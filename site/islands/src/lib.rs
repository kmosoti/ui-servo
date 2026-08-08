//! The site's WASM islands, and the panic plumbing they share.
//!
//! Two elements ship from this crate. `<resume-sandbox>` lives in
//! [`resume_sandbox`], with everything worth testing about it in [`resume`];
//! this file is `<ui-constellation>` and the machinery both of them report
//! failures through.
//!
//! # `<ui-constellation>`
//!
//! A canvas of nine nodes on fixed orbits, linked when they drift close enough,
//! reacting to the pointer. It is a placeholder for a real project graph, and it
//! exists mainly to prove three things end to end:
//!
//! 1. A wasm island can take its colours from the *contract*: every stroke here
//!    is a `--color-*` custom property read off the live element with
//!    `getComputedStyle`. No hex code is typed in this file, so a change to
//!    `direction/direction.toml` recolours the island without touching it.
//! 2. Motion respects `prefers-reduced-motion`. Under it the island renders one
//!    static frame and never schedules a `requestAnimationFrame`, so the probe's
//!    reduced-motion rule has nothing to report.
//! 3. A Rust panic is *observable*. [`install_panic_hook`] turns one into the
//!    `ui-servo:wasm-panic` CustomEvent the probe listens for, and
//!    [`provoke_panic`] fires one on demand so the sensor chain can be tested
//!    rather than assumed.
//!
//! The element itself is defined in `assets/islands/loader.js`; this crate is
//! the drawing and the panic plumbing. Nothing here touches the DOM outside the
//! canvas it is handed.
//!
//! `<resume-sandbox>` follows the same division: `loader.js` defines that class
//! too and calls [`resume_sandbox::mount_resume_sandbox`] from its
//! `connectedCallback`. That module explains why an element defined from
//! *inside* the wasm cannot work — its lifecycle callbacks would be downstream
//! of the fetch that has to produce them.

pub mod resume;
pub mod resume_sandbox;

use std::cell::{Cell, RefCell};
use std::rc::Rc;

use js_sys::Reflect;
use wasm_bindgen::JsCast;
use wasm_bindgen::prelude::*;
use web_sys::{
    CanvasRenderingContext2d, CssStyleDeclaration, CustomEvent, Element, HtmlCanvasElement,
};

thread_local! {
    /// The element whose call is currently on the stack, and therefore the
    /// element a panic right now belongs to.
    ///
    /// The README's convention dispatches on `document`, which is correct and
    /// unattributable: the probe resolves an event's span from its target's
    /// ancestors, and `document` has none. Naming the element instead lets the
    /// same event bubble to the same document-level listener while arriving
    /// with the fragment's `data-span-id` on it — the join key that makes the
    /// evidence worth storing.
    ///
    /// Set by [`Source`] on entry to every call that can panic and cleared on
    /// exit, so two islands on one page cannot be confused for each other and
    /// nothing holds a strong reference to a detached element between calls.
    static PANIC_SOURCE: RefCell<Option<Element>> = const { RefCell::new(None) };

    /// One panic has already happened. Everything after it is silence.
    ///
    /// A Rust panic traps the current call; the module's memory is left in
    /// whatever state the panic found it. Re-entering it would be undefined at
    /// best, and a still-running rAF loop would re-enter it sixty times a
    /// second — one real panic buried under a flood of `js-error`s. So the loop
    /// checks this and simply stops rescheduling.
    static PANICKED: Cell<bool> = const { Cell::new(false) };
}

/// Scopes [`PANIC_SOURCE`] to one call, restoring whatever it displaced.
pub(crate) struct Source(Option<Element>);

impl Source {
    pub(crate) fn enter(element: &Element) -> Self {
        Self(PANIC_SOURCE.with(|cell| cell.replace(Some(element.clone()))))
    }
}

impl Drop for Source {
    fn drop(&mut self) {
        PANIC_SOURCE.with(|cell| *cell.borrow_mut() = self.0.take());
    }
}

fn panicked() -> bool {
    PANICKED.with(Cell::get)
}

/// Reported as `detail.module` on a panic beacon.
const MODULE: &str = "ui_servo_islands_bg.wasm";
/// Stacks are evidence, not heap dumps — the probe truncates at 800 too.
const STACK_LIMIT: usize = 800;

/// Nodes, and their resting positions in a [-1, 1] box around the centre.
/// Fixed rather than random: a screenshot baseline is worthless if the subject
/// reshuffles itself every load.
const SEEDS: [(f64, f64, f64); 9] = [
    // (u, v, phase)
    (-0.88, -0.28, 0.0),
    (-0.61, 0.44, 1.9),
    (-0.31, -0.66, 3.4),
    (-0.05, 0.12, 5.1),
    (0.21, -0.47, 0.8),
    (0.35, 0.68, 2.6),
    (0.59, -0.14, 4.2),
    (0.81, 0.41, 5.8),
    (0.95, -0.60, 1.2),
];

/// How far apart two nodes can be and still be drawn as related, in CSS px.
const LINK_DISTANCE: f64 = 128.0;
/// How close the pointer must come before a node notices it, in CSS px.
const POINTER_REACH: f64 = 130.0;

/// The four contract colours the island is allowed to use.
struct Palette {
    accent: String,
    accent_2: String,
    border: String,
    muted: String,
}

/// The tokens that must resolve before anything is drawn.
const REQUIRED_TOKENS: [&str; 4] = [
    "--color-accent",
    "--color-accent-2",
    "--color-border",
    "--color-muted",
];

impl Palette {
    /// Read straight off the element. Custom properties inherit, so the canvas
    /// sees whatever `:root` (or any theming ancestor) currently resolves to.
    ///
    /// There is deliberately no fallback colour. A hard-coded hex here would be
    /// a value no contract authorised, drawn confidently, in a repository whose
    /// entire argument is that such values are the thing being prevented. If a
    /// token is missing the island refuses to draw and says which one.
    fn read(style: Option<&CssStyleDeclaration>) -> Result<Self, String> {
        let mut values = Vec::with_capacity(REQUIRED_TOKENS.len());
        let mut missing = Vec::new();
        for name in REQUIRED_TOKENS {
            match token(style, name) {
                Some(value) => values.push(value),
                None => missing.push(name),
            }
        }
        if !missing.is_empty() {
            return Err(format!(
                "<ui-constellation> will not render off-contract: unresolved {}",
                missing.join(", ")
            ));
        }
        let mut values = values.into_iter();
        Ok(Self {
            accent: values.next().unwrap_or_default(),
            accent_2: values.next().unwrap_or_default(),
            border: values.next().unwrap_or_default(),
            muted: values.next().unwrap_or_default(),
        })
    }
}

fn token(style: Option<&CssStyleDeclaration>, name: &str) -> Option<String> {
    let value = style?.get_property_value(name).ok()?;
    let value = value.trim();
    (!value.is_empty()).then(|| value.to_owned())
}

/// Everything a frame needs. `Cell` throughout so drawing takes `&self` and can
/// live behind an `Rc` shared with the pointer listeners and the rAF closure.
struct Renderer {
    ctx: CanvasRenderingContext2d,
    canvas: HtmlCanvasElement,
    /// The element this island was mounted on: who a panic in here belongs to.
    host: Element,
    palette: Palette,
    /// (css width, css height, device pixel ratio)
    size: Cell<(f64, f64, f64)>,
    pointer: Cell<Option<(f64, f64)>>,
}

impl Renderer {
    /// Match the backing store to the element's CSS box. Called on mount, on
    /// resize, and before the first frame — a canvas that skips this is blurry
    /// on every display that is not exactly 1×.
    fn resize(&self) {
        let dpr = web_sys::window()
            .map(|window| window.device_pixel_ratio())
            .filter(|ratio| *ratio > 0.0)
            .unwrap_or(1.0);
        let width = f64::from(self.canvas.client_width());
        let height = f64::from(self.canvas.client_height());
        if width <= 0.0 || height <= 0.0 {
            return;
        }
        let backing_w = (width * dpr).round().max(1.0) as u32;
        let backing_h = (height * dpr).round().max(1.0) as u32;
        if self.canvas.width() != backing_w {
            self.canvas.set_width(backing_w);
        }
        if self.canvas.height() != backing_h {
            self.canvas.set_height(backing_h);
        }
        self.size.set((width, height, dpr));
    }

    /// One frame. `elapsed_ms` is the rAF timestamp; 0 draws the resting pose,
    /// which is exactly what reduced motion gets.
    fn draw(&self, elapsed_ms: f64) {
        // Every path into the drawing code goes through here, so this is the
        // one place that has to name the element a panic belongs to.
        let _source = Source::enter(&self.host);
        let (width, height, dpr) = self.size.get();
        if width <= 0.0 || height <= 0.0 {
            return;
        }
        let ctx = &self.ctx;
        let _ = ctx.set_transform(dpr, 0.0, 0.0, dpr, 0.0, 0.0);
        ctx.clear_rect(0.0, 0.0, width, height);

        let seconds = elapsed_ms / 1000.0;
        let centre = (width / 2.0, height / 2.0);
        let spread = (width * 0.42, height * 0.36);
        let pointer = self.pointer.get();

        let mut points = [(0.0_f64, 0.0_f64, 0.0_f64); SEEDS.len()];
        for (index, (u, v, phase)) in SEEDS.iter().enumerate() {
            // A slow lissajous drift, amplitude a few px — enough to make the
            // links breathe, not enough to be a distraction.
            let wobble_x = (seconds * 0.21 + phase).sin() * spread.0 * 0.05;
            let wobble_y = (seconds * 0.17 + phase * 1.3).cos() * spread.1 * 0.07;
            let mut x = centre.0 + u * spread.0 + wobble_x;
            let mut y = centre.1 + v * spread.1 + wobble_y;

            let mut pull = 0.0;
            if let Some((px, py)) = pointer {
                let distance = ((px - x).powi(2) + (py - y).powi(2)).sqrt();
                if distance < POINTER_REACH && distance > 0.001 {
                    pull = 1.0 - distance / POINTER_REACH;
                    x += (px - x) / distance * pull * 9.0;
                    y += (py - y) / distance * pull * 9.0;
                }
            }
            let radius = 2.1 + (index % 3) as f64 * 0.45 + pull * 2.6;
            points[index] = (x, y, radius);
        }

        // Links first: the graph reads as a shape, the nodes as its joints.
        ctx.set_line_width(1.0);
        for (i, a) in points.iter().enumerate() {
            for b in points.iter().skip(i + 1) {
                let distance = ((a.0 - b.0).powi(2) + (a.1 - b.1).powi(2)).sqrt();
                if distance >= LINK_DISTANCE {
                    continue;
                }
                let closeness = 1.0 - distance / LINK_DISTANCE;
                let lively = a.2 > 3.6 || b.2 > 3.6;
                ctx.set_global_alpha(if lively {
                    0.30 + closeness * 0.5
                } else {
                    0.12 + closeness * 0.38
                });
                ctx.set_stroke_style_str(if lively {
                    &self.palette.accent_2
                } else {
                    &self.palette.border
                });
                ctx.begin_path();
                ctx.move_to(a.0, a.1);
                ctx.line_to(b.0, b.1);
                ctx.stroke();
            }
        }

        for (index, (x, y, radius)) in points.iter().enumerate() {
            let lively = *radius > 3.6;
            ctx.set_global_alpha(if lively { 0.95 } else { 0.7 });
            ctx.set_fill_style_str(if lively {
                &self.palette.accent
            } else if index % 4 == 0 {
                &self.palette.accent_2
            } else {
                &self.palette.muted
            });
            ctx.begin_path();
            let _ = ctx.arc(*x, *y, *radius, 0.0, std::f64::consts::TAU);
            ctx.fill();
        }
        ctx.set_global_alpha(1.0);
    }
}

/// The rAF callback, held in a cell it also captures: that self-reference is
/// how the loop re-schedules itself, and clearing the cell is how [`Constellation::stop`]
/// breaks the cycle instead of leaking it.
type FrameCallback = Rc<RefCell<Option<Closure<dyn FnMut(f64)>>>>;

/// A mounted island. The loader owns one of these per element and drops it in
/// `disconnectedCallback`.
#[wasm_bindgen]
pub struct Constellation {
    renderer: Rc<Renderer>,
    frame: FrameCallback,
    handle: Rc<Cell<Option<i32>>>,
    reduced_motion: bool,
    // Pointer listeners live as long as the island does.
    _listeners: Vec<Closure<dyn FnMut(web_sys::PointerEvent)>>,
}

#[wasm_bindgen]
impl Constellation {
    /// Attach to a canvas. Fails loudly (a JS exception the loader turns into a
    /// `ui-servo:ce-error`) rather than drawing nothing quietly.
    #[wasm_bindgen(constructor)]
    pub fn new(canvas: HtmlCanvasElement, reduced_motion: bool) -> Result<Constellation, JsValue> {
        let ctx = canvas
            .get_context("2d")?
            .ok_or_else(|| JsValue::from_str("2d canvas context unavailable"))?
            .dyn_into::<CanvasRenderingContext2d>()?;
        let style =
            web_sys::window().and_then(|window| window.get_computed_style(&canvas).ok().flatten());

        // The host element, so a panic raised while this island is running is
        // attributed to the fragment it was mounted in — not to the document,
        // and not to whichever island happened to be constructed last.
        let host = canvas
            .parent_element()
            .unwrap_or_else(|| canvas.clone().into());
        let _source = Source::enter(&host);

        let palette = Palette::read(style.as_ref()).map_err(|message| {
            // A missing token is a contract failure, not a rendering hiccup: it
            // is reported and nothing is drawn.
            JsValue::from(js_sys::Error::new(&message))
        })?;

        let renderer = Rc::new(Renderer {
            ctx,
            canvas: canvas.clone(),
            host,
            palette,
            size: Cell::new((0.0, 0.0, 1.0)),
            pointer: Cell::new(None),
        });
        renderer.resize();

        let mut listeners: Vec<Closure<dyn FnMut(web_sys::PointerEvent)>> = Vec::new();
        for (event, tracks) in [("pointermove", true), ("pointerleave", false)] {
            let renderer = renderer.clone();
            let listener = Closure::wrap(Box::new(move |event: web_sys::PointerEvent| {
                if !tracks {
                    renderer.pointer.set(None);
                    return;
                }
                let rect = renderer.canvas.get_bounding_client_rect();
                renderer.pointer.set(Some((
                    event.client_x() as f64 - rect.left(),
                    event.client_y() as f64 - rect.top(),
                )));
            }) as Box<dyn FnMut(web_sys::PointerEvent)>);
            canvas.add_event_listener_with_callback(event, listener.as_ref().unchecked_ref())?;
            listeners.push(listener);
        }

        Ok(Constellation {
            renderer,
            frame: Rc::new(RefCell::new(None)),
            handle: Rc::new(Cell::new(None)),
            reduced_motion,
            _listeners: listeners,
        })
    }

    /// Draw. Under reduced motion this is one static frame and nothing is
    /// scheduled — the contract says no animation, so there is no loop to stop.
    pub fn start(&self) {
        if panicked() {
            return;
        }
        if self.reduced_motion || self.handle.get().is_some() {
            self.draw_still();
            return;
        }
        self.renderer.resize();

        let renderer = self.renderer.clone();
        let handle = self.handle.clone();
        let frame = self.frame.clone();
        let next = frame.clone();
        *frame.borrow_mut() = Some(Closure::wrap(Box::new(move |timestamp: f64| {
            // Some *other* island may have panicked since the last frame. This
            // instance is fine; the module underneath it is not.
            if panicked() {
                handle.set(None);
                return;
            }
            renderer.draw(timestamp);
            let scheduled = next.borrow().as_ref().and_then(request_frame);
            handle.set(scheduled);
        }) as Box<dyn FnMut(f64)>));

        let scheduled = self.frame.borrow().as_ref().and_then(request_frame);
        self.handle.set(scheduled);
    }

    /// One frame, nothing scheduled. What reduced motion gets, and what the
    /// `?panic=1` hook leaves on screen: a trapped wasm instance cannot be
    /// re-entered, so an island that is about to panic must not own a running
    /// rAF loop — every frame after the trap would be a fresh `js-error`.
    pub fn draw_still(&self) {
        if panicked() {
            return;
        }
        self.renderer.resize();
        self.renderer.draw(0.0);
    }

    /// The deliberate test hook behind `?panic=1`, raised *by this island* so
    /// the beacon carries this island's span.
    ///
    /// A sensor that has never fired is not a sensor. This is the one call in
    /// the crate whose entire purpose is to make the wasm-panic path
    /// observable in an acceptance test rather than assumed.
    pub fn provoke_panic(&self) {
        let _source = Source::enter(&self.renderer.host);
        panic!("ui-constellation: deliberate panic from the ?panic=1 test hook");
    }

    /// Cancel the loop and drop the closure, which is what breaks the `Rc` cycle
    /// the rAF chain would otherwise leak.
    pub fn stop(&self) {
        if let (Some(window), Some(handle)) = (web_sys::window(), self.handle.take()) {
            window.cancel_animation_frame(handle).ok();
        }
        *self.frame.borrow_mut() = None;
    }

    /// The element's box changed; re-measure and, if nothing is animating,
    /// repaint so a reduced-motion island is not left stretched.
    pub fn resize(&self) {
        if panicked() {
            return;
        }
        self.renderer.resize();
        if self.handle.get().is_none() {
            self.renderer.draw(0.0);
        }
    }
}

fn request_frame(closure: &Closure<dyn FnMut(f64)>) -> Option<i32> {
    web_sys::window()?
        .request_animation_frame(closure.as_ref().unchecked_ref())
        .ok()
}

/// Run on module init, before anything else in here can be called.
///
/// One job: install the panic hook. `loader.js` also calls
/// [`install_panic_hook`] immediately after `init()` resolves, and doing it
/// twice is harmless — `set_hook` replaces. Doing it here as well closes the
/// window between the module coming up and that call, which costs nothing and
/// means no panic in this crate can ever reach JS as a bare
/// `RuntimeError: unreachable` with its message already gone.
#[wasm_bindgen(start)]
fn start() {
    install_panic_hook();
}

/// Route Rust panics to the probe.
///
/// The loader calls this immediately after `init()`. A panic in wasm is a trap:
/// by the time JS sees it, the message is gone and all that is left is
/// `RuntimeError: unreachable`. The hook runs *before* the trap, so this is the
/// only place the panic message still exists.
#[wasm_bindgen]
pub fn install_panic_hook() {
    std::panic::set_hook(Box::new(|info| {
        // Latched before dispatching: the listener that receives this event may
        // call back into the module, and everything from here on must find the
        // door closed.
        PANICKED.with(|flag| flag.set(true));
        dispatch_panic(&info.to_string());
    }));
}

/// `ui-servo:wasm-panic` as `probe/README.md` specifies it — bubbling, with
/// `{message, module, stack}` — dispatched from the mounted element when there
/// is one so the probe can resolve a span, and from `document` otherwise.
fn dispatch_panic(message: &str) {
    let Some(document) = web_sys::window().and_then(|window| window.document()) else {
        return;
    };
    let source = PANIC_SOURCE.with(|cell| {
        cell.borrow()
            .as_ref()
            .filter(|element| element.is_connected())
            .cloned()
    });

    let detail = js_sys::Object::new();
    set(&detail, "message", message);
    set(&detail, "module", MODULE);
    set(&detail, "stack", &truncate(&js_stack()));

    // Built as a plain object and cast: `CustomEventInit`'s builder API has
    // changed shape across web-sys releases, the object literal has not.
    let init = js_sys::Object::new();
    let _ = Reflect::set(&init, &JsValue::from_str("bubbles"), &JsValue::TRUE);
    let _ = Reflect::set(&init, &JsValue::from_str("composed"), &JsValue::TRUE);
    let _ = Reflect::set(&init, &JsValue::from_str("detail"), &detail);

    if let Ok(event) =
        CustomEvent::new_with_event_init_dict("ui-servo:wasm-panic", init.unchecked_ref())
    {
        let target: &web_sys::EventTarget = match source.as_ref() {
            Some(element) => element.as_ref(),
            None => document.as_ref(),
        };
        let _ = target.dispatch_event(&event);
    }
}

fn set(object: &js_sys::Object, key: &str, value: &str) {
    let _ = Reflect::set(object, &JsValue::from_str(key), &JsValue::from_str(value));
}

/// The JS-side stack at the moment of the panic. Rust's own backtrace is not
/// available on `wasm32-unknown-unknown`; this at least names the call path.
///
/// `stack` is a non-standard property with no js-sys getter, so it is read
/// reflectively and an engine that does not provide it yields an empty string.
fn js_stack() -> String {
    let error = js_sys::Error::new("ui-servo island panic");
    Reflect::get(&error, &JsValue::from_str("stack"))
        .ok()
        .and_then(|stack| stack.as_string())
        .unwrap_or_default()
}

fn truncate(value: &str) -> String {
    value.chars().take(STACK_LIMIT).collect()
}
