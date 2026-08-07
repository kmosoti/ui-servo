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
 * Route Rust panics to the probe.
 *
 * The loader calls this immediately after `init()`. A panic in wasm is a trap:
 * by the time JS sees it, the message is gone and all that is left is
 * `RuntimeError: unreachable`. The hook runs *before* the trap, so this is the
 * only place the panic message still exists.
 */
export function install_panic_hook(): void;

export type InitInput = RequestInfo | URL | Response | BufferSource | WebAssembly.Module;

export interface InitOutput {
    readonly memory: WebAssembly.Memory;
    readonly __wbg_constellation_free: (a: number, b: number) => void;
    readonly constellation_draw_still: (a: number) => void;
    readonly constellation_new: (a: any, b: number) => [number, number, number];
    readonly constellation_resize: (a: number) => void;
    readonly constellation_start: (a: number) => void;
    readonly constellation_stop: (a: number) => void;
    readonly install_panic_hook: () => void;
    readonly constellation_provoke_panic: (a: number) => void;
    readonly wasm_bindgen_a2217870e4debde5___convert__closures_____invoke___f64______true_: (a: number, b: number, c: number) => void;
    readonly wasm_bindgen_a2217870e4debde5___convert__closures_____invoke___web_sys_834c9b80139a7d34___features__gen_PointerEvent__PointerEvent______true_: (a: number, b: number, c: any) => void;
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
