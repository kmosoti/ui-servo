// loader.js — the JS half of the <ui-constellation> island.
//
// Hand-written, not generated: everything else in this directory is wasm-pack
// output. Its job is small and deliberate.
//
//   1. Define the custom element the moment the module runs, so an element
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
// Nothing here re-throws. One broken island must not take the rest of a swap
// with it, and re-throwing would double-report through window.onerror.

const TAG = 'ui-constellation';
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
function reportCustomElementError(element, error, phase) {
  const { message, stack } = describe(error);
  try {
    element.dispatchEvent(
      new CustomEvent('ui-servo:ce-error', {
        bubbles: true,
        composed: true,
        detail: { message, phase, tag: TAG, stack },
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
