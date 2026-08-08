/* tslint:disable */
/* eslint-disable */

/**
 * A mounted island. The loader owns one of these per element and drops it in
 * `disconnectedCallback`.
 */
export class Constellation {
    free(): void;
    [Symbol.dispose](): void;
    /**
     * One frame, nothing scheduled. What reduced motion gets, and what the
     * `?panic=1` hook leaves on screen: a trapped wasm instance cannot be
     * re-entered, so an island that is about to panic must not own a running
     * rAF loop — every frame after the trap would be a fresh `js-error`.
     */
    draw_still(): void;
    /**
     * Attach to a canvas. Fails loudly (a JS exception the loader turns into a
     * `ui-servo:ce-error`) rather than drawing nothing quietly.
     */
    constructor(canvas: HTMLCanvasElement, reduced_motion: boolean);
    /**
     * The deliberate test hook behind `?panic=1`, raised *by this island* so
     * the beacon carries this island's span.
     *
     * A sensor that has never fired is not a sensor. This is the one call in
     * the crate whose entire purpose is to make the wasm-panic path
     * observable in an acceptance test rather than assumed.
     */
    provoke_panic(): void;
    /**
     * The element's box changed; re-measure and, if nothing is animating,
     * repaint so a reduced-motion island is not left stretched.
     */
    resize(): void;
    /**
     * Draw. Under reduced motion this is one static frame and nothing is
     * scheduled — the contract says no animation, so there is no loop to stop.
     */
    start(): void;
    /**
     * Cancel the loop and drop the closure, which is what breaks the `Rc` cycle
     * the rAF chain would otherwise leak.
     */
    stop(): void;
}

/**
 * The handle JS holds, and the island's only strong reference.
 *
 * `free()` from `disconnectedCallback` is what runs its destructor: the
 * listeners unhook themselves as they drop, and the last `Rc` on [`Sandbox`]
 * goes with them. Nothing else in the island holds a strong reference, so
 * there is no cycle for this to fail to break.
 *
 * It has no `Drop` of its own, deliberately, and no assertion that it held the
 * last `Rc`. "The handle holds the only strong reference" is true between
 * calls and not during one: a listener has an upgraded `Rc` on the stack while
 * it runs, and `free()` can legally arrive inside that window — a page-level
 * listener on the click this island just dispatched can remove the host. A
 * count of 2 there is the `Rc` doing its job, and [`Sandbox`] correctly
 * outlives this handle by exactly the rest of that call. Everything that has
 * to happen on teardown therefore happens in [`Sandbox::drop`], which runs
 * when the island is *actually* gone rather than when JS let go of it.
 */
export class ResumeSandbox {
    private constructor();
    free(): void;
    [Symbol.dispose](): void;
}

/**
 * Route Rust panics to the probe.
 *
 * The loader calls this immediately after `init()`. A panic in wasm is a trap:
 * by the time JS sees it, the message is gone and all that is left is
 * `RuntimeError: unreachable`. The hook runs *before* the trap, so this is the
 * only place the panic message still exists.
 */
export function install_panic_hook(): void;

/**
 * How many `<resume-sandbox>` instances are currently mounted.
 *
 * Mount and free are the only two things that move it, so after a detach it
 * must be back where it started. It is exported so a browser test can cycle
 * sandboxes through the DOM and assert exactly that; no such test is committed
 * yet, and the export is what makes writing one a page of Playwright rather
 * than a memory profiler.
 */
export function live_resume_sandboxes(): number;

/**
 * Build the island inside `host`, and hand JS the handle that owns it.
 *
 * The one entry point. `loader.js` calls this from `<resume-sandbox>`'s
 * `connectedCallback`; an `Err` arrives there as a thrown exception, which it
 * turns into the `ui-servo:ce-error` the probe listens for. The returned
 * handle is what keeps the island alive, and calling `free()` on it is what
 * ends it — see [`ResumeSandbox`].
 */
export function mount_resume_sandbox(host: HTMLElement): ResumeSandbox;

/**
 * Run on module init, before anything else in here can be called.
 *
 * One job: install the panic hook. `loader.js` also calls
 * [`install_panic_hook`] immediately after `init()` resolves, and doing it
 * twice is harmless — `set_hook` replaces. Doing it here as well closes the
 * window between the module coming up and that call, which costs nothing and
 * means no panic in this crate can ever reach JS as a bare
 * `RuntimeError: unreachable` with its message already gone.
 */
export function start(): void;

export type InitInput = RequestInfo | URL | Response | BufferSource | WebAssembly.Module;

export interface InitOutput {
    readonly memory: WebAssembly.Memory;
    readonly __wbg_constellation_free: (a: number, b: number) => void;
    readonly __wbg_resumesandbox_free: (a: number, b: number) => void;
    readonly constellation_draw_still: (a: number) => void;
    readonly constellation_new: (a: any, b: number) => [number, number, number];
    readonly constellation_resize: (a: number) => void;
    readonly constellation_start: (a: number) => void;
    readonly constellation_stop: (a: number) => void;
    readonly install_panic_hook: () => void;
    readonly live_resume_sandboxes: () => number;
    readonly mount_resume_sandbox: (a: any) => [number, number, number];
    readonly start: () => void;
    readonly constellation_provoke_panic: (a: number) => void;
    readonly wasm_bindgen_a2217870e4debde5___convert__closures_____invoke___f64______true_: (a: number, b: number, c: number) => void;
    readonly wasm_bindgen_a2217870e4debde5___convert__closures_____invoke___wasm_bindgen_a2217870e4debde5___JsValue__core_9b3796e30d99ddb7___result__Result_____wasm_bindgen_a2217870e4debde5___JsError___true_: (a: number, b: number, c: any) => [number, number];
    readonly wasm_bindgen_a2217870e4debde5___convert__closures_____invoke___js_sys_4c4b8cf065ecbf05___Function_fn_wasm_bindgen_a2217870e4debde5___JsValue_____wasm_bindgen_a2217870e4debde5___sys__Undefined___js_sys_4c4b8cf065ecbf05___Function_fn_wasm_bindgen_a2217870e4debde5___JsValue_____wasm_bindgen_a2217870e4debde5___sys__Undefined_______true_: (a: number, b: number, c: any, d: any) => void;
    readonly wasm_bindgen_a2217870e4debde5___convert__closures_____invoke___web_sys_504172b18353fa11___features__gen_PointerEvent__PointerEvent______true_: (a: number, b: number, c: any) => void;
    readonly wasm_bindgen_a2217870e4debde5___convert__closures_____invoke_______true_: (a: number, b: number) => void;
    readonly wasm_bindgen_a2217870e4debde5___convert__closures_____invoke_______true__1_: (a: number, b: number) => void;
    readonly __wbindgen_malloc: (a: number, b: number) => number;
    readonly __wbindgen_realloc: (a: number, b: number, c: number, d: number) => number;
    readonly __wbindgen_exn_store: (a: number) => void;
    readonly __externref_table_alloc: () => number;
    readonly __wbindgen_externrefs: WebAssembly.Table;
    readonly __wbindgen_destroy_closure: (a: number, b: number) => void;
    readonly __externref_table_dealloc: (a: number) => void;
    readonly __wbindgen_start: () => void;
}

export type SyncInitInput = BufferSource | WebAssembly.Module;

/**
 * Instantiates the given `module`, which can either be bytes or
 * a precompiled `WebAssembly.Module`.
 *
 * @param {{ module: SyncInitInput }} module - Passing `SyncInitInput` directly is deprecated.
 *
 * @returns {InitOutput}
 */
export function initSync(module: { module: SyncInitInput } | SyncInitInput): InitOutput;

/**
 * If `module_or_path` is {RequestInfo} or {URL}, makes a request and
 * for everything else, calls `WebAssembly.instantiate` directly.
 *
 * @param {{ module_or_path: InitInput | Promise<InitInput> }} module_or_path - Passing `InitInput` directly is deprecated.
 *
 * @returns {Promise<InitOutput>}
 */
export default function __wbg_init (module_or_path?: { module_or_path: InitInput | Promise<InitInput> } | InitInput | Promise<InitInput>): Promise<InitOutput>;
