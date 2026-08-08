// loader.js — the JS half of the WASM islands.
//
// Hand-written, not generated: everything else in this directory is wasm-pack
// output. Its job is small and deliberate.
//
//   1. Define both custom elements the moment the module runs, so an element
//      already in the document upgrades without waiting on a network fetch.
//   2. Load the wasm *lazily*, on first connect. A page with no island pays
//      nothing for this file beyond its own bytes, and an island swapped in by
//      htmx later still works.
//   3. Report every way this can fail, per probe/README.md: a throw in a
//      lifecycle callback becomes `ui-servo:ce-error`, a rejected instantiate
//      becomes `ui-servo:ce-error` with phase `wasm-init` (the probe's own
//      WebAssembly wrapper reports the same rejection as `wasm-error`; both
//      land, from two independent witnesses), and a Rust panic becomes
//      `ui-servo:wasm-panic` from the hook installed below.
//
// Two elements, one module, one pattern. Both classes are defined here rather
// than from inside the wasm, and for <resume-sandbox> that is load-bearing
// rather than tidy: an element registered by the module cannot upgrade until
// the module is up, so its connectedCallback would be downstream of the fetch
// that callback exists to cause. Anything else that could notice the tag —
// a querySelector at startup, a MutationObserver — is a worse connectedCallback
// that misses either later swaps or shadow trees. Defining the class here costs
// one exported function, mount_resume_sandbox, and gets the platform's own
// answer to "when is this element on the page".
//
// Nothing here re-throws. One broken island must not take the rest of a swap
// with it, and re-throwing would double-report through window.onerror.

const TAG = 'ui-constellation';
const SANDBOX_TAG = 'resume-sandbox';
const WASM_URL = new URL('./ui_servo_islands.js', import.meta.url).href;
const STACK_LIMIT = 800;

function describe(error) {
  if (error instanceof Error) {
    return {
      message: `${error.name}: ${error.message}`,
      stack: error.stack ? String(error.stack).slice(0, STACK_LIMIT) : '',
    };
  }
  return { message: String(error), stack: '' };
}

// The convention from probe/README.md: bubbles, so the probe's document-level
// listener sees it and resolves the nearest [data-span-id] ancestor itself.
//
// `tag` is a parameter rather than the constant it used to be: this file now
// reports for two elements, and a sandbox's failed fetch labelled
// `ui-constellation` would send whoever reads the beacon to the wrong file.
function reportCustomElementError(element, error, phase, tag = TAG) {
  const { message, stack } = describe(error);
  try {
    element.dispatchEvent(
      new CustomEvent('ui-servo:ce-error', {
        bubbles: true,
        composed: true,
        detail: { message, phase, tag, stack },
      }),
    );
  } catch {
    // A failure to report is not worth a second failure.
  }
}

let wasmPromise = null;

// One instantiate for the whole page, shared by every island on it. A failure
// clears the cache so a later element can try again rather than inheriting a
// dead promise.
function loadWasm() {
  if (!wasmPromise) {
    wasmPromise = import(WASM_URL)
      .then(async (module) => {
        await module.default();
        // Must happen before any other call into wasm: the hook is the only
        // thing that can still see a panic message before the trap erases it.
        module.install_panic_hook();
        return module;
      })
      .catch((error) => {
        wasmPromise = null;
        throw error;
      });
  }
  return wasmPromise;
}

// ------------------------------------------------------- <resume-sandbox> ---
//
// The editor island. Everything it does is Rust: this class is the lifecycle
// and nothing else — fetch on connect, mount, free on disconnect.

class ResumeSandbox extends HTMLElement {
  #island = null;
  // The box a pending load's reaction reads this element out of, or null when
  // no load is in flight for it. See connectedCallback.
  #slot = null;

  connectedCallback() {
    // Connect is not first connect: htmx moves elements, which disconnects and
    // reconnects them, and #teardown freed the last handle. Mounting again is
    // cheap — the Rust side finds the furniture still in place and adopts it,
    // so whatever was typed into the editor survives the move.
    if (this.#island) {
      return;
    }
    // A move is a disconnect and a connect, and disconnect wrote `detached`.
    // The element is here, and `loading` is what that is called.
    this.dataset.island = 'loading';
    if (this.#slot) {
      if (!this.#slot.settled) {
        // A load is already in flight for this element and its reaction is
        // still coming; a disconnect only emptied the box. Putting this element
        // back in it reuses that reaction instead of registering a second one,
        // so a stalled import plus a hundred htmx swaps is still one reaction.
        this.#slot.element = this;
        return;
      }
      // The load settled while this element sat detached. The reaction found
      // the box empty and returned — it could not clear this element's #slot,
      // because emptying the box was exactly what severed its reference to
      // this element. A box that is both settled and still held here is that
      // phantom, and waiting on it would leave the element `loading` forever;
      // dropping it lets a fresh reaction register below, against a wasm
      // promise that is already cached on the success path.
      this.#slot = null;
    }

    // Through a box, not directly. A promise reaction cannot be unregistered,
    // so a `.then` on the shared import holds whatever it closes over until
    // that import settles — and capturing `this` would let a removed sandbox
    // and its whole subtree outlive its own disconnect, past free() and past
    // the live count meant to notice. The reaction closes over the box instead,
    // and disconnect empties it. What a stalled import can still retain is one
    // emptied box per swap: bytes, deterministically, with no DOM attached and
    // no dependence on WeakRef or on when a collector feels like running.
    const slot = { element: this, settled: false };
    this.#slot = slot;
    loadWasm().then(
      (module) => {
        slot.settled = true;
        const element = slot.element;
        slot.element = null;
        if (!element) {
          return;
        }
        element.#slot = null;
        element.#boot(module);
      },
      (error) => {
        slot.settled = true;
        const element = slot.element;
        slot.element = null;
        if (!element) {
          return;
        }
        element.#slot = null;
        // A sandbox removed while the load was in flight is already reporting
        // `detached`, and that is the truer state: nothing is broken about it,
        // it is simply not here. Overwriting it with `error` would invent a
        // failure for an element nobody is waiting on.
        if (!element.isConnected) {
          return;
        }
        element.dataset.island = 'error';
        element.#report(error, 'wasm-init');
      },
    );
  }

  // Freeing is not optional. The handle owns the only strong reference to the
  // island's wasm-side state, its four DOM listeners, a pending clipboard
  // write and an object URL; dropping the JS wrapper alone would leave all of
  // them alive in linear memory, and an htmx page that swaps this fragment in
  // and out would accumulate one whole sandbox per swap. free() is the only
  // thing that runs the destructor.
  disconnectedCallback() {
    // Empty the box, but keep it. A load still in flight has a reaction holding
    // this element through that box, and emptying it is the only way to let go
    // — the reaction cannot be taken off the promise. Keeping the box is what
    // lets a reconnect refill it rather than attach a second reaction.
    if (this.#slot) {
      this.#slot.element = null;
    }
    const island = this.#island;
    this.#island = null;
    this.dataset.island = 'detached';
    if (!island) {
      return;
    }
    try {
      island.free();
    } catch (error) {
      this.#report(error, 'disconnect');
    }
  }

  #boot(module) {
    // The element may have been removed while the wasm was in flight, or
    // removed and put back — in which case a second connect already booted it.
    if (!this.isConnected || this.#island) {
      return;
    }
    let island;
    try {
      island = module.mount_resume_sandbox(this);
    } catch (error) {
      this.dataset.island = 'error';
      this.#report(error, 'connect');
      return;
    }
    // Checked again, on the far side of the mount. Mounting writes the
    // skeleton into this element, and a DOM write is a place other code runs:
    // any custom element it removes gets a synchronous disconnectedCallback,
    // and a page-level handler in that callback can remove this host. That
    // disconnect already ran and found no handle to free, so storing one here
    // unchecked would leave a detached element holding a live island — the
    // exact cycle free() exists to break, made permanent.
    if (!this.isConnected) {
      this.dataset.island = 'detached';
      try {
        island.free();
      } catch (error) {
        this.#report(error, 'disconnect');
      }
      return;
    }
    this.#island = island;
    this.dataset.island = 'ready';
  }

  // An event dispatched on a detached node bubbles through a detached tree and
  // reaches no document-level listener, so a report from a disconnected sandbox
  // would be silent. The root is the fallback: a beacon with no span beats no
  // beacon. Both failure paths here can arrive after a removal — a rejected
  // import, and free() itself.
  #report(error, phase) {
    const host = this.isConnected ? this : document.documentElement;
    reportCustomElementError(host, error, phase, SANDBOX_TAG);
  }
}

const REDUCED_MOTION = '(prefers-reduced-motion: reduce)';

function prefersReducedMotion() {
  return typeof matchMedia === 'function' && matchMedia(REDUCED_MOTION).matches;
}

// `?panic=1` on the page, or `panic` on the element. The deliberate test hook:
// it exists so the wasm-panic sensor can be proven rather than assumed.
function panicRequested(element) {
  if (element.hasAttribute('panic')) {
    return true;
  }
  try {
    return new URLSearchParams(location.search).get('panic') === '1';
  } catch {
    return false;
  }
}

class UiConstellation extends HTMLElement {
  #canvas = null;
  #scene = null;
  // Which answer to `prefers-reduced-motion` the current scene was built for.
  #reduced = null;
  #observer = null;
  #motion = null;
  #onMotionChange = null;

  connectedCallback() {
    // The probe shims customElements.define with exactly this try/catch as a
    // backstop; doing it here too means the report carries a real phase and
    // does not depend on the probe having loaded first.
    try {
      this.#mount();
    } catch (error) {
      this.dataset.island = 'error';
      reportCustomElementError(this, error, 'connect');
    }
  }

  disconnectedCallback() {
    try {
      this.#teardown();
    } catch (error) {
      reportCustomElementError(this, error, 'disconnect');
    }
  }

  // Connect is not "first connect": moving an element in the DOM disconnects
  // and reconnects it, and htmx does exactly that. Whatever #teardown() removes
  // must be re-registered here, or a moved island comes back deaf to resizes
  // and to the reduced-motion preference.
  #mount() {
    this.#listen();

    if (this.#scene) {
      // The preference can change while an element sits detached, and a scene
      // is built around the answer it got.
      if (this.#reduced !== prefersReducedMotion()) {
        this.#rebuildScene();
        return;
      }
      this.#scene.start();
      this.dataset.island = 'ready';
      return;
    }
    if (this.#canvas) {
      // Reconnected while the wasm was still loading, or after a failure: the
      // pending #boot picks it up, and a failed load left a state worth keeping.
      return;
    }

    // Styling is applied from here rather than from site.css: this element and
    // its canvas are the island's own furniture, and the site's utility
    // vocabulary has no class that would describe them.
    this.style.display = 'block';
    this.style.lineHeight = '0';

    const canvas = document.createElement('canvas');
    canvas.setAttribute('aria-hidden', 'true');
    canvas.style.cssText = 'display:block;width:100%;height:180px;touch-action:none;';
    this.#canvas = canvas;
    this.replaceChildren(canvas);
    this.dataset.island = 'loading';

    loadWasm().then(
      (module) => this.#boot(module),
      (error) => {
        this.dataset.island = 'error';
        // A rejected instantiate/import. The probe reports the rejection as
        // `wasm-error` on its own; this names the component that wanted it.
        reportCustomElementError(this, error, 'wasm-init');
      },
    );
  }

  // Exactly one observer and one media-query listener per connected element,
  // owned by connect/disconnect rather than by the scene. Rebuilding the scene
  // on a preference change must not accumulate either.
  #listen() {
    if (!this.#observer && typeof ResizeObserver === 'function') {
      this.#observer = new ResizeObserver(() => {
        try {
          this.#scene?.resize();
        } catch (error) {
          reportCustomElementError(this, error, 'resize');
        }
      });
      this.#observer.observe(this);
    }

    // Reduced motion is a live preference, not a boot-time constant.
    if (!this.#motion && typeof matchMedia === 'function') {
      this.#motion = matchMedia(REDUCED_MOTION);
      this.#onMotionChange = () => this.#rebuildScene();
      this.#motion.addEventListener('change', this.#onMotionChange);
    }
  }

  #boot(module) {
    // The element may have been removed while the wasm was in flight.
    if (!this.isConnected || !this.#canvas) {
      return;
    }
    try {
      // Two preference changes in quick succession would otherwise leave the
      // first scene running its own rAF loop with nothing holding it.
      this.#dropScene();

      const reduced = prefersReducedMotion();
      this.#scene = new module.Constellation(this.#canvas, reduced);
      this.#reduced = reduced;

      if (panicRequested(this)) {
        // Draw one frame, then trap. Deliberately *without* starting the loop:
        // a trapped module cannot be trusted to be re-entered, so a running rAF
        // would turn one panic into a js-error every 16 ms.
        this.#scene.draw_still();
        this.#provokePanic();
        return;
      }

      this.#scene.start();
      this.dataset.island = 'ready';
    } catch (error) {
      this.dataset.island = 'error';
      reportCustomElementError(this, error, 'render');
    }
  }

  // The `?panic=1` test hook, raised through this element's own instance so the
  // panic beacon carries this element's span. The Rust hook has already
  // dispatched `ui-servo:wasm-panic` with the real message by the time this
  // catch runs; what JS sees is only the trap it left behind, reported as the
  // second, independent witness that this element is now broken.
  #provokePanic() {
    try {
      this.#scene.provoke_panic();
      this.dataset.island = 'error';
    } catch (error) {
      // Dropped rather than kept, and dropped by hand: calling free() on a
      // trapped instance would re-enter the module that just died.
      this.#scene = null;
      this.#reduced = null;
      this.dataset.island = 'panicked';
      reportCustomElementError(this, error, 'panic');
    }
  }

  // A live change to `prefers-reduced-motion`. Only the scene is rebuilt: the
  // observer and the media-query listener belong to the connection, not to the
  // scene, so toggling the preference a hundred times still leaves one of each.
  #rebuildScene() {
    this.#dropScene();
    if (!this.isConnected || !wasmPromise) {
      return;
    }
    wasmPromise.then(
      (module) => this.#boot(module),
      () => {
        // The load already failed and was already reported; a preference change
        // is not a second failure.
      },
    );
  }

  // Stop and release the wasm-side instance. Freeing is not optional: the
  // renderer owns pointer listeners and a self-scheduling rAF callback, and
  // dropping the JS handle alone would leave both running.
  #dropScene() {
    if (!this.#scene) {
      return;
    }
    const scene = this.#scene;
    this.#scene = null;
    this.#reduced = null;
    try {
      scene.stop();
      scene.free();
    } catch (error) {
      reportCustomElementError(this, error, 'rebuild');
    }
  }

  #teardown() {
    this.#observer?.disconnect();
    this.#observer = null;
    if (this.#motion && this.#onMotionChange) {
      this.#motion.removeEventListener('change', this.#onMotionChange);
    }
    this.#motion = null;
    this.#onMotionChange = null;
    // The scene survives a disconnect: the canvas and its wasm state are still
    // valid, and #mount() restarts them if the element comes back.
    this.#scene?.stop();
  }
}

// Idempotent: htmx can re-execute this module's script tag, and a second
// define() would throw where nothing is watching.
if (!customElements.get(TAG)) {
  customElements.define(TAG, UiConstellation);
}
if (!customElements.get(SANDBOX_TAG)) {
  customElements.define(SANDBOX_TAG, ResumeSandbox);
}
