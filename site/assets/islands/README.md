# site/islands/ — the WASM islands

Two custom elements ship from this crate. `<ui-constellation>` is the original
and the rest of this file is mostly about it; `<resume-sandbox>` is described in
[its own section](#resume-sandbox) below.

## `<ui-constellation>`

A canvas of nine nodes on fixed
orbits, linked when they drift close, leaning toward the pointer. As a feature
it is a placeholder for a real project graph. As a *unit* it exists to prove
three claims about islands in this repository:

1. **Colour comes from the contract.** Every stroke reads a `--color-*` custom
   property off the live element with `getComputedStyle`. No hex value is typed
   in `src/lib.rs`; re-emitting `tokens.css` from `direction/direction.toml`
   recolours the island without touching it.
2. **Motion is opt-out.** Under `prefers-reduced-motion: reduce` the island
   draws exactly one frame and never schedules a `requestAnimationFrame`. There
   is no loop to slow down, so there is nothing for the probe's reduced-motion
   rule to find.
3. **Failure is observable.** A panic in wasm is a trap: by the time JS sees it,
   all that is left is `RuntimeError: unreachable`. `install_panic_hook()` runs
   *before* the trap and turns the panic into the `ui-servo:wasm-panic`
   CustomEvent `probe/README.md` defines, with the real message still attached.

```
site/islands/
  Cargo.toml              wasm-bindgen(-futures) · js-sys · web-sys · maud · serde, cdylib
  build.sh                wasm-pack into ../assets/islands (see "The .gitignore wrinkle")
  src/lib.rs              the constellation, the panic hook, the ?panic=1 test hook
  src/resume.rs           <resume-sandbox>'s schema, validator, renderer — no browser in it
  src/resume_sandbox.rs   the element: events in, four DOM writes out
```

## Build

```sh
rustup target add wasm32-unknown-unknown   # once
cargo install wasm-pack --locked           # or a release binary from GitHub
sh site/islands/build.sh                   # wasm-pack build --target web --out-dir ../assets/islands
```

The crate is its own workspace root, and `site/Cargo.toml` excludes it, so
building the server never pulls a wasm target into its graph and building the
island never needs axum.

Output lands in `site/assets/islands/` next to the hand-written `loader.js`, and
is **committed**, for the same reason `tokens.css` and `htmx.min.js` are: the
server serves `assets/` as-is and must not need a wasm toolchain to boot.

### The .gitignore wrinkle

`wasm-pack` writes a blanket `*` `.gitignore` into its out-dir on every build.
The out-dir here also holds `loader.js`, the one file in there a human wrote, so
`build.sh` deletes that `.gitignore` after each build. Building with a bare
`wasm-pack build …` instead will silently untrack the loader.

## The two halves

**`src/lib.rs`** draws and panics. It never touches the DOM outside the canvas
it is handed. `Constellation::start()` runs the rAF loop, `draw_still()` renders
a single frame, `stop()` cancels the loop *and* drops the callback, which is
what breaks the `Rc` cycle a self-scheduling rAF would otherwise leak.

**`../assets/islands/loader.js`** defines the element. It is loaded on every
page by `layout.rs` and fetches the wasm lazily, on the first `connectedCallback`
— a page with no island pays only for the loader, and an island htmx swaps in
later still upgrades. Nothing in it re-throws: a broken island must not cascade
through the rest of a swap.

## Reporting, per `probe/README.md`

| what failed | event | phase |
| --- | --- | --- |
| `connectedCallback` threw | `ui-servo:ce-error` | `connect` |
| the wasm module would not load or instantiate | `ui-servo:ce-error` | `wasm-init` |
| construction or first draw threw | `ui-servo:ce-error` | `render` |
| a `ResizeObserver` repaint threw | `ui-servo:ce-error` | `resize` |
| a Rust panic | `ui-servo:wasm-panic` | — |
| the trap left behind by that panic | `ui-servo:ce-error` | `panic` |

Panics are dispatched **from the mounted element**, not from `document` as the
README's snippet shows. Both reach the probe's document-level listener; only the
first arrives with a `data-span-id` ancestor, which is what lets the beacon be
attributed to the fragment the island was mounted in instead of to the page at
large.

## The `?panic=1` hook

`/?panic=1` (or a `panic` attribute on the element) makes the island draw one
still frame and then call `provoke_panic()`. It is deliberate, and deliberately
reachable from a URL: a sensor that has never fired is not a sensor.

The loop is *not* started in this path. A trapped wasm instance cannot be
re-entered, so a running rAF would turn one panic into a `js-error` every frame
— evidence spam that buries the one event worth reading.

**Nothing drives this in a browser today.** `tests/test_island.py` used to: it
asserted the CustomEvent fires with the Rust message intact and that `probe.js`
turns it into a beacon carrying this turn's id and the constellation fragment's
span id. Those tests went when the site stopped mounting a constellation on any
page — `/fragments/constellation` still serves one, but a browser test has to
open a URL that has the element on it, and there is none. The file now drives
`<resume-sandbox>` on `/about`, and covers the sensor chain through that
element's own failure path (a refused module fetch → `ce-error` → beacon)
instead. The `?panic=1` path is now exercised by nothing at all — `provoke_panic`
has no native test either, and could not have a useful one: a panic in a host
`cargo test` unwinds, which is precisely not the trap the hook exists for.

## `<resume-sandbox>`

```html
<resume-sandbox></resume-sandbox>
```

No attributes, no slot, no configuration: an editor pre-loaded with a real
résumé, a preview that re-renders on every keystroke, a status pill reading
`not rendered` / `rendered` / `blocked`, the full list of what is wrong, and
three buttons — Reset sample, Copy JSON, Download HTML.

It is a port of `setupResumeSandbox` from the owner's old vanilla-JS portfolio,
and the port is the point. Two things the JS could not do:

1. **Escaping stopped being a thing to remember.** The JS interpolated
   `escapeHtml(value)` at every hole and was correct for exactly as long as
   nobody added a hole. `src/resume.rs` renders with `maud`, so every
   interpolation escapes because that is the only thing `maud` can do with a
   `&str`. `<script>` typed into the editor arrives in the preview as text, and
   `cargo test` asserts it on the rendered string — thirteen hostile holes in
   one résumé, all thirteen escaped. The claim a browser adds is that
   `set_inner_html` does not undo the escaping, and `tests/test_island.py` now
   makes it: a `<script>` and an `<img onerror>` typed into the editor on
   `/about` build no element and set no global.
2. **The validator went where JS's coercion used to be.** `skills: [1, 2, 3]`
   passed the old checks and the renderer printed the numbers; a `projects`
   entry with no `name` printed the word `undefined` into a résumé. Rust has no
   `String(undefined)` to fall back on, so `validate` names each of those
   instead. Every check the JS had is here with its wording reproduced
   verbatim, plus the ones it needed and lacked. It is not a *strict* superset
   of its output: one input, `"experience": [[]]`, made the JS report four
   missing fields (because `"role" in []` is false four times) and reports one
   violation here naming the cause — that the entry is not an object. The
   divergence is deliberate and a test pins it, so it cannot drift into an
   accident.

The split is deliberate and it is the reason this unit has tests at all:

**`src/resume.rs`** is the schema, the validator, the renderer, the sample and
the three states. No `wasm-bindgen`, no `web-sys`, nothing that needs a DOM —
so `cargo test` on the *host* target runs all of it, in milliseconds, with no
browser and no harness.

**`src/resume_sandbox.rs`** reads a string out of a `<textarea>`, hands it to
`SandboxState::evaluate`, and writes the answer into four places. There is no
logic in it worth a browser to be sure of.

### How it gets defined

Exactly the way `<ui-constellation>` does: `loader.js` defines the class, and
the class calls into wasm. The crate exports one function for it,
`mount_resume_sandbox(host)`, which builds the island and returns the handle
that owns it.

The obvious alternative — registering the element from inside the module, with
a `wasm_bindgen(inline_js)` snippet run by `#[wasm_bindgen(start)]` — was
written, reviewed and removed. It works, but it inverts the trigger: an element
the module defines cannot upgrade until the module is up, so its
`connectedCallback` is downstream of the fetch that callback exists to cause,
and something outside has to notice the tag instead. Everything that can is a
worse `connectedCallback`:

| instead of `connectedCallback` | what it misses |
| --- | --- |
| `document.querySelector` at startup | anything htmx swaps in later |
| a `MutationObserver` on `documentElement` | anything inside a shadow root |
| "put a constellation on the page too" | not a mechanism |

Defining the class in `loader.js` costs one exported function and gets the
platform's own answer to "when is this element on the page", including inside a
shadow tree, for both elements, with one lifecycle to review rather than two.

Both elements therefore behave alike while the module is in flight:
`data-island` reads `loading`, then `ready`, `error`, or `detached`. The
sandbox has nothing to show during `loading` — its skeleton is built by the
Rust that has not arrived — so a page that wants a placeholder puts one in the
fragment.

### What it costs

The island went from 21 KB to 69 KB gzipped when `serde_json`, `serde_derive`
and `maud` joined it. That is a real regression for every page, because every
page loads one wasm module — `opt-level = "z"` recovers about 4 % of it and
nothing else in this crate's reach recovers more. The honest fix is a second
`cdylib` so a page with only a constellation on it does not pay for a JSON
parser, and that is an integration decision, not this crate's.

### Styling, and what the token sheet could not say

Every class the island puts in the DOM is one `site.css` declares — the probe
reads the CSSOM and reports any class it cannot account for, so an island is
held to the same vocabulary a fragment is. That vocabulary is colour, type
scale and spacing. It has no `display`, so:

| what it wanted | what it got instead |
| --- | --- |
| `resume-sandbox { display: block }` | nothing; an unknown element is `display: inline`, and the block children inside it lay out anyway |
| editor and preview side by side | stacked `<section>`s |
| a full-width `<textarea>` | `rows` and `cols`, which are content attributes rather than presentation |
| a `<pre>` that wraps instead of overflowing | `site.css` now says `pre { white-space: pre-wrap; overflow-wrap: anywhere }` (scrolling locally would trip the probe's overflow sensor) — closed |
| a pill-shaped status chip | `<code>`, which `site.css` already styles, and which is the right element for a machine state |

None of these is inlined as a `style` attribute — a test asserts the skeleton
contains no `style=` at all.

Two of the five are closed now, by the unit that mounted this island on
`/about`: `pre { white-space: pre-wrap; overflow-wrap: anywhere }` and `textarea { max-width: 100% }` are in
`site.css`, written on the elements rather than on this tag. They were not
cosmetic — a `<textarea cols="72">` and an unwrapped `<pre>` are each wider than
a phone, and an element wider than the viewport scrolls the whole document
sideways. The `textarea` rule caps it; it does not make it full width, so that
row of the table stands. What is still missing is
`resume-sandbox { display: block }` and anything that would put the editor and
the preview side by side, and `site.css` belongs to another unit.

### Teardown

`disconnectedCallback` calls `free()`, and `free()` is the only thing that runs
the destructor: the handle owns the island's whole object graph, and dropping
the JS wrapper alone would leave it in linear memory. An htmx page that swaps
this fragment in and out would then accumulate one entire sandbox per swap.

Freeing has to actually free, which means nothing in the island may hold a
strong reference back to it and nothing may outlive it:

- **every callback holds a `Weak`** — the four DOM listeners and the
  label-restore timer. An `Rc` in any of them would be a cycle through the host
  element and its whole subtree, which is a permanent leak rather than a slow
  one. A callback that fires after the free finds nothing to upgrade.
- **listeners unhook themselves.** Dropping a `Closure` invalidates the
  wasm-side slot but leaves the DOM registration pointing at it, so a detached
  node someone still holds would throw on its next event instead of doing
  nothing.
- **the flash timer is cancelled** and the Copy button's label put back, or a
  moved element would arrive reading "Copied" forever.
- **the clipboard write is `await`ed, not `then2`'d, and raced against
  teardown.** A `Closure` pair handed to a promise has to stay valid until it
  settles, and nothing here decides when that is; a future instead owns itself
  and holds one `Weak`. Because a future is retained by the promise it awaits,
  there are two bounds on it: one write in flight at a time, and a
  teardown-resolved promise in the race so that a `writeText` the browser never
  settles cannot pin a task for the life of the document.
- **the object URL is revoked** — by the next download or by teardown, never in
  the same task as the click that consumes it.

`live_resume_sandboxes()` is exported as the sensor for all of this: mount and
free are the only two things that move it, so cycling sandboxes through the DOM
must leave it where it started. Nothing on screen changes when a detached
island stays in memory, which is exactly why the count is worth exporting.

### Tests

`cargo test` in this directory, host target, no browser:

- the sample validates, round-trips through JSON, and renders every section;
- removing each of the eight required fields produces exactly its named
  violation, and so does making each one the wrong type;
- `experience` items are checked for all four keys, for `bullets` being an
  array, and for its entries being strings, with the index in the message;
- violations accumulate rather than stopping at the first;
- a clean validation guarantees deserialisation — the promise `Resume::parse`
  is built on;
- thirteen hostile holes in one résumé all escape, and an attribute breakout
  stays text;
- the skeleton's hooks match the queries `mount` makes, and its classes are all
  in the contract's allowlist.
