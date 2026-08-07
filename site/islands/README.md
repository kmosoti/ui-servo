# site/islands/ — the WASM island

One custom element, `<ui-constellation>`: a canvas of nine nodes on fixed
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
  Cargo.toml     wasm-bindgen · js-sys · web-sys, crate-type = cdylib
  build.sh       wasm-pack into ../assets/islands (see "The .gitignore wrinkle")
  src/lib.rs     the renderer, the panic hook, the ?panic=1 test hook
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

`tests/test_island.py` drives exactly this: it asserts the CustomEvent fires
with the Rust message intact, and that `probe.js` turns it into a beacon
carrying this turn's id and the constellation fragment's span id.
