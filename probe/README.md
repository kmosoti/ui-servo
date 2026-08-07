# probe/ — the browser-side sensor

`probe.js` is the control loop's first-hand witness of what a browser actually
did with generated markup. It is a **sensor**: it observes and reports, never
corrects. Judgement lives in `direction/direction.toml` and the server-side
gates; the probe only compares against the motion table the server hands it.

Vanilla JS, no dependencies, no build step: servable raw. The rationale that
would normally sit in comments lives here instead, because the file is on a
gzip budget (see [Size](#size)).

## Wiring it up

The server injects configuration *before* the script tag:

```html
<script>
  window.__UI_SERVO__ = {
    beacon: "/beacon",
    turnId: "t-0007",
    motion: {
      durations: [90, 140, 220, 320],
      easings: ["cubic-bezier(0.2, 0, 0, 1)", "cubic-bezier(0.16, 1, 0.3, 1)",
                "cubic-bezier(0.4, 0, 1, 1)"],
      properties: ["transform", "opacity"],
      reducedMotionRequired: true
    }
  };
</script>
<script src="/static/probe.js"></script>
```

`motion` is `DirectionContract.motion_table()` flattened to JSON. Its fields are
`frozenset`s, which `json.dumps` cannot serialise and `dataclasses.asdict` does
not convert — sort them into lists explicitly:

```python
table = contract.motion_table()
motion = {
    "durations": sorted(table.durations_ms),
    "easings": sorted(table.easings),
    "properties": sorted(table.animatable_properties),
    "reducedMotionRequired": table.reduced_motion_required,
}
```

The dataclass's own names (`durations_ms`, `animatable_properties`,
`reduced_motion_required`) are accepted as aliases. **A missing or empty list
disables that one comparison and emits `probe-config-incomplete` once**, so a
half-wired server shows up as a signal instead of silently permissive gates.

Anything the server wants attributed should carry `data-span-id`: each event's
`spanId` resolves to the nearest `[data-span-id]` ancestor, else the element's
`id`, else `cfg.spanId`, else a synthetic `anon-N`.

## Beacon protocol

Events queue and flush with `navigator.sendBeacon(cfg.beacon, JSON.stringify(batch))`
— a **string**, so the browser sends `Content-Type: text/plain;charset=UTF-8`.
The server must parse the body as JSON regardless of the declared content type.
The body is a JSON array of events:

```json
[{"spanId": "span-hero", "turnId": "t-0007", "kind": "motion-violation",
  "ts": 341, "node": "div #hero card", "payload": {}}]
```

- `ts` — `performance.now()` rounded to ms (page-relative, monotonic).
- `node` — compact descriptor of the element the event is about
  (`tag #id class-list`), or `null` for page-level events.
- Absent payload fields are omitted rather than sent as `null`; treat missing
  as unknown.

Flush triggers: every 2 s when the queue is non-empty; on `visibilitychange →
hidden`; and immediately for kinds matching `/error|reject|csp|panic/` — if the
page is about to die, the evidence that killed it is what matters most.

Back-pressure, so a wedged beacon can never wedge the page:

- **Chunking** — a batch is split so no single `sendBeacon` call exceeds 32 KB
  (measured as UTF-8 bytes).
- **Queue cap** — 500 events. Beyond that the oldest are dropped and the next
  flush leads with `probe-drop` carrying the count.
- **Retry** — a failed chunk re-queues and is re-chunked on the next flush, so a
  batch never grows monotonically into something that can never be sent.

## Event kinds

| kind | source | payload |
| --- | --- | --- |
| `swap-ok` | `htmx:afterSwap`, plus one at boot for `<body>` | `children` |
| `swap-error` | `htmx:responseError`, `htmx:sendError`, `htmx:timeout` | `event`, `status`, `path` |
| `motion-violation` | `getAnimations({subtree:true})` on the swapped root | `name`, `duration`, `easing`, `easings[]`, `properties[]`, `reasons[]` |
| `animation` | same scan, conforming | same minus `reasons` — evidence, not a fault |
| `unknown-class` | CSSOM vocabulary vs. swapped subtree | `vocabulary` (size), `classes: [{name, count}]` (max 40) |
| `overflow` | rAF-deferred measurement | `viewport`, `offenders: [{node, scrollWidth, clientWidth, root}]` |
| `longtask` | `PerformanceObserver` | `duration` |
| `layout-shift` | `PerformanceObserver`, ignores `hadRecentInput` | `value`, `sources[]` (node descriptors, max 5) |
| `interaction` | `PerformanceObserver` `event`, `durationThreshold: 100` | `name`, `duration` |
| `element-timing` | `PerformanceObserver` `element` (server adds `elementtiming`) | `identifier`, `renderTime` |
| `js-error` | `window` `error` (capture), incl. resource load failures | `message`, `source`, `line`, `stack` / `message`, `src` |
| `rejection` | `unhandledrejection` | `message`, `stack` |
| `csp` | `securitypolicyviolation` | `blocked`, `directive`, `source`, `line` |
| `ce-error` | `ui-servo:ce-error` convention + the `customElements.define` shim | `message`, `phase`, `tag`, `stack` |
| `wasm-error` | rejected `WebAssembly.instantiate` / `instantiateStreaming` | `method`, `message` |
| `wasm-panic` | `ui-servo:wasm-panic` convention | `message`, `module`, `stack` |
| `probe-drop` | queue cap reached | `dropped` |
| `probe-config-incomplete` | boot, once | `missing[]` |

Stacks truncate at 800 chars, overflow offenders at 12 descendants (the
scrolling root is always included and never counted against the cap), unknown
classes at 40. A beacon is evidence, not a heap dump.

## Conventions components must follow

Two failure modes are unobservable from outside a component, so the probe
defines events instead of monkeypatching internals:

```js
// Custom element failure (bubbles; the probe listens at the document)
this.dispatchEvent(new CustomEvent('ui-servo:ce-error', {
  bubbles: true, detail: { message: String(err), phase: 'render' }
}));

// WASM panic hook
document.dispatchEvent(new CustomEvent('ui-servo:wasm-panic', {
  bubbles: true, detail: { message: panicMessage, module: 'app.wasm', stack }
}));
```

As a backstop, `customElements.define` is shimmed to wrap `connectedCallback`,
`attributeChangedCallback` and `adoptedCallback` in try/catch: those run outside
the swap's call stack, so a throw there is otherwise invisible to the swap that
caused it. **The shim reports and swallows rather than re-throwing** — one
broken component must not cascade through the rest of a swap, and re-throwing
would double-report through `window.onerror`. Elements defined before `probe.js`
loads are not shimmed; inject the probe first.

## How each check earns its result

**CSSOM vocabulary.** Built at boot, rebuilt on `htmx:afterSettle` and whenever
a swapped fragment carries CSS. A class with no rule behind it is a typo or an
invented utility; both are drift. Details that matter:

- Quoted strings and `[attr]` blocks are stripped from `selectorText` before
  class extraction, so `a[href$=".pdf"]` does **not** whitelist a class `pdf`.
- A hex escape consumes one trailing whitespace character (CSS Syntax 4.3.7), so
  `.\31 0xl` is the single class `10xl`, not `1`. Variant escapes (`.hover\:x`,
  `.md\:p-4`) normalise back to the class the HTML carries.
- Cross-origin sheets throw on `.cssRules` and are skipped — unreadable by
  design, not a violation. Unknown-class findings are only as good as the
  same-origin stylesheets.
- A fragment may ship its own `<style>` or `<link>`. Judging its classes against
  the pre-swap vocabulary would report the fragment's own new rules as unknown,
  so the vocabulary is rebuilt first for inline `<style>` (live the moment it is
  inserted), and the class check is deferred until any not-yet-loaded `<link>`
  fires `load`/`error`.

**Motion.** Compared against the injected table, exactly:

- Durations are compared with a 0.001 ms epsilon that covers float
  representation only. `140.4 ms` is *not* the `140 ms` token — rounding an
  observed duration onto the nearest token is how drift launders itself past a
  gate.
- Easing is per segment. A CSS animation carries its timing function on the
  *keyframes* (`getComputedTiming().easing` reads `"linear"` even for a contract
  bezier); `element.animate(kfs, {easing})` is the reverse. So: keyframes
  uniformly at the API default `"linear"` mean the effect easing is the real
  one; otherwise every keyframe easing is a real segment and each is judged on
  its own — an explicit `linear` segment mixed among contract beziers is a
  violation, and reporting only the "interesting" easing would hide it.
- Properties come from keyframe keys (de-camelised), falling back to
  `transitionProperty` for CSS transitions.
- With `reducedMotionRequired`, any animation running while
  `prefers-reduced-motion: reduce` matches is itself a violation.

**Overflow.** Measured after two `requestAnimationFrame` ticks so layout has
settled. Any `scrollWidth > clientWidth` at all counts — a 1 px horizontal
scrollbar is still a horizontal scrollbar, so there is no slack threshold.
`document.scrollingElement` is probed on every check and reported first.

## Escape hatch

`window.__UI_SERVO_PROBE__` exposes `{emit, flush, onSwap, vocab, known,
queue()}` for fixtures and for components that swap without htmx (call
`onSwap(rootElement)` yourself).

## Size

The unit budget is `gzip -n -9 -c probe/probe.js | wc -c` ≤ 4096. Measured
2026-08-06:

| form | raw | gzip -n -9 |
| --- | ---: | ---: |
| this file as committed (comments + indentation) | 12791 | **5196** |
| same code, comments and indentation stripped | 9827 | **3995** |

The budget is met by the served form and missed by the readable one by ~1100
bytes, all of which is comment and indentation — no sensor was traded for it.
The floor for the full sensor set is ~4.1 kB gzipped even with *zero* comments
and normal indentation, so the readable source cannot fit 4096 while keeping
every check; a minifier (unavailable offline here) plus identifier mangling
would land the served asset near 3.2 kB. Either serve a minified copy or read
the budget as applying to the served artifact.

## Fixture

`tests/fixtures/probe.html` is a standalone, htmx-free exercise rig: it
synthesises htmx's CustomEvents and deterministically provokes `swap-ok`,
`unknown-class`, `motion-violation`, `animation`, `overflow`, `js-error`,
`swap-error`, `ce-error`, `element-timing` and `wasm-panic`. It also pins the
regression cases: a 140.4 ms animation (off-token by 0.4 ms), a contract-bezier
animation with one `linear` segment, a class that appears only inside an
attribute selector (`pdf` — must be reported), a hex-escaped class (`10xl` —
must not), a class defined by the fragment's own `<style>` (`fragment-only` —
must not), and an element overflowing by exactly 1 px.

It tees `navigator.sendBeacon` into `window.__PROBE_EVENTS__` (with
`window.__PROBE_KINDS__()`) so assertions work under `file://` without a server;
the native beacon is still attempted and its result ignored. U4's Playwright
acceptance drives it.
