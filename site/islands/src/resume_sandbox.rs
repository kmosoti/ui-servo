//! `<resume-sandbox>` — the browser half of [`crate::resume`].
//!
//! A page embeds it with one line and no attributes:
//!
//! ```html
//! <resume-sandbox></resume-sandbox>
//! ```
//!
//! It fills itself in: an editor pre-loaded with the sample résumé, a preview
//! that re-renders on every keystroke, a status pill, the list of violations,
//! and three buttons — Reset sample, Copy JSON, Download HTML. Nothing has to
//! be passed to it, because everything it needs is in [`crate::resume`], which
//! is also where all of it is tested.
//!
//! This file is deliberately the thin half. It reads a string out of a
//! `<textarea>`, hands it to [`SandboxState::evaluate`], and writes the answer
//! into four places. There is no validation logic here and no HTML built by
//! hand, so nothing here needs a browser to be sure of.
//!
//! ## `set_inner_html` on visitor-supplied data
//!
//! The preview pane is filled with `set_inner_html`, which is the one call in
//! this crate that would be a hole if the string came from anywhere else. It
//! comes from [`crate::resume::render`], a `maud` template, so every value
//! that reaches it has already been escaped by construction — a `<script>` in
//! the editor arrives as `&lt;script&gt;` and there is no code path that could
//! have skipped it. The alternative, building the preview node by node, would
//! move that guarantee from the type system into a reviewer's attention span.
//!
//! ## What defines the element
//!
//! `assets/islands/loader.js` does, exactly as it does for
//! `<ui-constellation>`, and this crate exports [`mount_resume_sandbox`] for it
//! to call. That split is not incidental — it is what makes the element's
//! lifecycle work at all.
//!
//! The alternative was tried and abandoned: registering the element from inside
//! the module, via a `wasm_bindgen(inline_js)` snippet run by the start hook.
//! It works, but it inverts the trigger. An element defined from inside the
//! module cannot upgrade until the module is up, so its `connectedCallback` is
//! downstream of the fetch that callback exists to cause, and something outside
//! has to notice the tag some other way. Every "other way" is a worse version
//! of `connectedCallback`: a `document.querySelector` misses what htmx swaps in
//! later, and a `MutationObserver` catches that but cannot see into a shadow
//! root, which `connectedCallback` gets for free.
//!
//! Defining the class in `loader.js` and calling into wasm from it costs one
//! exported function and gives back the platform's own answer to "when is this
//! element on the page".

use std::cell::{Cell, RefCell};
use std::rc::Rc;

use js_sys::{Function, Object, Promise, Reflect};
use maud::{Markup, html};
use wasm_bindgen::JsCast;
use wasm_bindgen::prelude::*;
use wasm_bindgen_futures::{JsFuture, spawn_local};
use web_sys::{
    Blob, Document, Element, EventTarget, HtmlAnchorElement, HtmlElement, HtmlTextAreaElement, Url,
};

use crate::Source;
use crate::resume::{SandboxState, sample_json};

/// The tag, in one place, because the shim and the error reports must agree.
const TAG: &str = "resume-sandbox";
/// What the download is called. The old sandbox's filename, kept.
const FILENAME: &str = "resume-preview.html";
/// The Copy button's resting label.
const COPY_LABEL: &str = "Copy JSON";
/// How long "Copied" stays up, in ms. Long enough to read, short enough that
/// the button is not lying about its own affordance by the time you look back.
const COPY_FEEDBACK_MS: i32 = 1400;
/// Said for every way a clipboard write can fail, because from where the
/// visitor sits they are one thing: the text is not on the clipboard, and the
/// fallback is the same either way.
const CLIPBOARD_FAILED: &str = "Clipboard write failed. Select the JSON manually.";

/// Build the island inside `host`, and hand JS the handle that owns it.
///
/// The one entry point. `loader.js` calls this from `<resume-sandbox>`'s
/// `connectedCallback`; an `Err` arrives there as a thrown exception, which it
/// turns into the `ui-servo:ce-error` the probe listens for. The returned
/// handle is what keeps the island alive, and calling `free()` on it is what
/// ends it — see [`ResumeSandbox`].
#[wasm_bindgen]
pub fn mount_resume_sandbox(host: &HtmlElement) -> Result<ResumeSandbox, JsValue> {
    mount(host)
}

// ------------------------------------------------------------------ markup ---

/// The furniture, built once at mount.
///
/// Every class here is one `site.css` declares — the probe reads the CSSOM and
/// reports any class in the DOM that no stylesheet can account for, so the
/// island is held to the same vocabulary a fragment is. That vocabulary is
/// colour, type scale and spacing; it has nothing that says "two columns" and
/// nothing that says "this textarea is full width", and no inline `style` is
/// used to invent them. What is left is semantic structure, which is what the
/// rest of the site lays out with anyway. The gap is recorded in `README.md`.
///
/// `rows` and `cols` are doing the work a `textarea` rule would: they are
/// content attributes rather than presentation, and they are the only way to
/// size a `<textarea>` without either a class or a `style`.
fn skeleton() -> Markup {
    html! {
        p class="type-xs text-muted" { "Validation status" }
        h2 class="type-md" data-validation-title { "Waiting for input" }

        p {
            button type="button" data-reset-resume { "Reset sample" }
            " "
            button type="button" data-copy-resume-json { (COPY_LABEL) }
            " "
            button type="button" data-download-resume-html { "Download HTML" }
        }

        section {
            h3 class="type-sm text-muted" { "Structured data" }
            textarea data-resume-json spellcheck="false" rows="18" cols="72"
                aria-label="Resume JSON" {}
        }

        section {
            h3 class="type-sm text-muted" {
                "Rendered preview "
                code data-render-state { "not rendered" }
            }
            div data-resume-preview {}
        }

        section {
            h3 class="type-sm text-muted" { "Validation output" }
            // Polite rather than assertive: the output changes on every
            // keystroke, and a screen reader interrupting mid-word for each one
            // would make the editor unusable.
            pre data-validation-output aria-live="polite" {}
        }
    }
}

// ------------------------------------------------------------------- island ---

thread_local! {
    /// How many sandboxes are mounted right now.
    ///
    /// A leak is the failure mode this island is most likely to have and least
    /// likely to show: nothing on screen changes when a detached instance stays
    /// in linear memory, and an htmx page that swaps a fragment in and out
    /// accumulates one per swap. So the count is a sensor, exported and
    /// asserted on, for the same reason `?panic=1` exists — a claim about
    /// teardown that nothing measures is a claim, not a property.
    static LIVE: Cell<usize> = const { Cell::new(0) };
}

/// How many `<resume-sandbox>` instances are currently mounted.
///
/// Mount and free are the only two things that move it, so after a detach it
/// must be back where it started. It is exported so a browser test can cycle
/// sandboxes through the DOM and assert exactly that; no such test is committed
/// yet, and the export is what makes writing one a page of Playwright rather
/// than a memory profiler.
#[wasm_bindgen]
pub fn live_resume_sandboxes() -> usize {
    LIVE.with(Cell::get)
}

/// The parts of the DOM this island writes to, resolved once.
///
/// Every reference *into* this struct is a `Weak` — the listeners', the
/// label-restore callback's, the in-flight clipboard write's — so
/// [`ResumeSandbox`] holds the only strong one and dropping it is what ends the
/// island. An `Rc` in any of them would be a cycle, and a cycle here is not a
/// slow leak but a permanent one: the host element and every descendant are
/// reachable from this struct.
struct Sandbox {
    host: HtmlElement,
    editor: HtmlTextAreaElement,
    preview: Element,
    title: Element,
    output: Element,
    pill: Element,
    copy: HtmlElement,
    state: RefCell<SandboxState>,
    /// Puts [`COPY_LABEL`] back on the Copy button when the flash expires.
    ///
    /// Built once at mount rather than per press: a `Closure::once_into_js` per
    /// press is one JS function and one captured element per press, and it is
    /// never freed at all if the timer that would have called it is cancelled.
    restore: RefCell<Option<Closure<dyn Fn()>>>,
    /// The pending flash timer, if any. At most one, so a second press restarts
    /// the window instead of racing the first one's expiry.
    flash: Cell<Option<i32>>,
    /// A clipboard write is in flight. At most one is, ever: see
    /// [`Sandbox::copy`].
    writing: Cell<bool>,
    /// Settles the promise the in-flight clipboard write is racing against.
    ///
    /// Held only while that write is in flight, and dropped the moment it
    /// settles: keeping one signal for the island's whole life would leave one
    /// dead reaction on it per completed copy, which is the unbounded growth
    /// the race was there to prevent.
    cancel: RefCell<Option<Function>>,
    /// A download's click is on the stack. See [`Sandbox::download`].
    downloading: Cell<bool>,
    /// The object URL the last download handed out, still live.
    ///
    /// Kept rather than revoked on the spot: revoking inside the same task as
    /// the click can cancel the download in browsers that resolve the URL after
    /// the task ends. At most one is outstanding — the next download revokes it
    /// and so does teardown — so the blob it pins is bounded by one file.
    exported: RefCell<Option<String>>,
}

/// A promise and the function that settles it, and nothing else ever will.
///
/// `Promise::race` is the only cancellation a `JsFuture` has: a future awaiting
/// a promise is retained by that promise's reaction list, so a promise nothing
/// ever settles is a task nothing ever frees. Racing against one of these turns
/// "the island was dropped" into a settlement, which is the event the await was
/// missing.
///
/// One is built per write rather than one per island. `race` cannot detach the
/// reaction it registers when the *other* promise wins, so a shared signal
/// would collect one dead reaction per completed copy and hold them all until
/// teardown. A signal nothing references once its write has settled is
/// collectable in one piece, reaction list included — which is why [`Sandbox`]
/// keeps `fire` exactly as long as the write it belongs to.
struct Signal {
    promise: Promise,
    fire: Function,
}

impl Signal {
    fn new() -> Self {
        let mut fire = None;
        // The executor runs synchronously, inside `Promise::new`, so `fire` is
        // always `Some` by the time this returns. `unwrap_or_else` rather than
        // `expect` because a panic in wasm is a trap that takes the island with
        // it, and a no-op cancel is a far smaller failure than that.
        let promise = Promise::new(&mut |resolve, _reject| fire = Some(resolve));
        let fire = fire.unwrap_or_else(|| Function::new_no_args(""));
        Self { promise, fire }
    }
}

impl Drop for Sandbox {
    /// Three things outlive an island if nobody stops them.
    ///
    /// The flash timer: cancelling it is what makes [`Sandbox::restore`] safe to
    /// drop a moment later — a timer left armed would fire against an
    /// invalidated closure and throw inside a callback nothing is watching. The
    /// label goes back at the same time, because htmx moves an element by
    /// disconnecting and reconnecting it, the subtree travels with it, and a
    /// button still reading "Copied" when it lands would keep that label for
    /// the rest of the page's life.
    ///
    /// The clipboard await: settled here, because a promise the browser never
    /// settles would otherwise retain its task for the life of the page.
    ///
    /// The object URL: an unrevoked one pins its blob to the document, and the
    /// document outlives this element.
    fn drop(&mut self) {
        if let Some(fire) = self.cancel.borrow_mut().take() {
            let _ = fire.call0(&JsValue::NULL);
        }
        if let Some(url) = self.exported.borrow_mut().take() {
            revoke_next_task(url);
        }
        // Unconditionally, not only when a timer is pending: `flash_copied`
        // writes "Copied" before it schedules anything, and the schedule can
        // fail. The label is the visible state, so it is what gets reset.
        self.copy.set_text_content(Some(COPY_LABEL));
        if let Some((window, timer)) = web_sys::window().zip(self.flash.take()) {
            window.clear_timeout_with_handle(timer);
        }

        // Last, and that ordering is the claim. Every line above is a call into
        // JS, and a JS call that throws leaves wasm by trapping rather than by
        // unwinding — no destructor after it runs. Decrementing first would
        // mean a teardown that died halfway still reported itself complete;
        // decrementing last means the sensor stays high, which is exactly what
        // a half-freed island is. Freeing the *handle* is not this event
        // either: if something else still held an `Rc`, this would not be
        // running at all.
        LIVE.with(|live| live.set(live.get().saturating_sub(1)));
    }
}

/// Clears a flag however its scope ends.
///
/// The flags this island guards are re-entrancy flags, and a re-entrancy flag
/// that is only cleared on the happy path is a button that stops working.
struct Guard<'a>(&'a Cell<bool>);

impl Drop for Guard<'_> {
    fn drop(&mut self) {
        self.0.set(false);
    }
}

impl Sandbox {
    /// Re-read the editor and write the answer to all four surfaces.
    ///
    /// One function for every input, because "the panel reflects the editor" is
    /// one invariant, and splitting it across per-event handlers is how a
    /// status pill ends up disagreeing with the preview beside it.
    fn update(&self) {
        let _source = Source::enter(self.host.as_ref());
        let source = self.editor.value();
        // What the editor holds is now also what it reverts to. A `<textarea>`
        // in a `<form>` is reset to its *default* value, an event that fires no
        // `input` and would otherwise leave this panel describing text that is
        // no longer on screen. Keeping the two equal makes a form reset a no-op
        // for this field, which is the only sensible answer for an editor that
        // is not part of anybody's submission.
        let _ = self.editor.set_default_value(&source);

        let state = SandboxState::evaluate(&source);
        self.title.set_text_content(Some(state.title()));
        self.pill.set_text_content(Some(state.pill()));
        self.output.set_text_content(Some(&state.detail()));
        // Safe by construction, not by review — see the module doc.
        if set_html(&self.preview, &state.preview().into_string()).is_err() {
            // Trusted Types, almost certainly. The preview is the one thing
            // here that needs markup; everything else still works, so say what
            // is missing rather than leaving a pane that looks like it is still
            // loading. `set_text_content` is not an injection sink and is not
            // policed, so this cannot fail the same way.
            self.preview.set_text_content(Some(
                "This page's security policy blocks the rendered preview.",
            ));
        }

        *self.state.borrow_mut() = state;
    }

    fn reset(&self) {
        self.editor.set_value(&sample_json());
        self.update();
    }

    /// Copy the editor's text, and say so on the button that did it.
    ///
    /// The clipboard is reached reflectively rather than through `web-sys`:
    /// `web_sys::Clipboard` is gated behind `web_sys_unstable_apis`, which
    /// would put a `RUSTFLAGS` requirement into `build.sh` for one method call.
    ///
    /// The settlement is `await`ed rather than handled with a `Closure` pair,
    /// and that is the whole memory-safety argument for this island's one
    /// promise. A closure handed to `then2` must stay valid until a promise
    /// settles, and nothing here controls when that is — the visitor can detach
    /// the element mid-write. Dropping a `Closure` JS still holds makes it
    /// throw inside a reaction; keeping it alive to avoid that is a leak per
    /// press. A future has neither problem: it owns itself and holds one
    /// `Weak`, so a torn-down island is not there to upgrade.
    ///
    /// A future is retained by the promise it awaits, though, so "it is freed
    /// by its settlement" is only as good as the settlement. Two bounds make it
    /// one:
    ///
    /// - **One at a time.** A second press while a write is in flight is
    ///   ignored, so an island can hold at most one of these however hard the
    ///   button is pressed. The press is not silently dropped — the answer to
    ///   the first one is already on its way to the same button.
    /// - **Cancelled at teardown.** The clipboard is raced against a [`Signal`]
    ///   this island fires when it drops. A `writeText` the browser never
    ///   settles — a permission prompt nobody answers, a page put to sleep
    ///   mid-write — would otherwise retain its task for the life of the
    ///   document.
    ///
    /// Together: at most one task per live island, and none per dead one.
    fn copy(sandbox: &Rc<Self>) {
        // Claimed before anything else, because everything else is a call into
        // JS and a call into JS is a place other code can run. A press that
        // arrives while this one is still setting up must lose here, or two
        // writes end up in flight and the second one's cancellation overwrites
        // the first's — leaving a task with no way to be cancelled at all.
        // The answer to the earlier press is on its way to the same button, so
        // losing costs the visitor nothing.
        if sandbox.writing.replace(true) {
            return;
        }
        let Some(promise) = write_clipboard(&sandbox.editor.value()) else {
            sandbox.writing.set(false);
            sandbox.warn(CLIPBOARD_FAILED);
            return;
        };
        let cancel = Signal::new();
        let race = Promise::race(&js_sys::Array::of2(&promise, &cancel.promise));
        *sandbox.cancel.borrow_mut() = Some(cancel.fire);

        let weak = Rc::downgrade(sandbox);
        spawn_local(async move {
            let settled = JsFuture::from(race).await;
            // A cancelled write lands here too, and lands with the `Rc` already
            // gone — so "was this island torn down?" is answered by the upgrade
            // rather than by inspecting which promise got there first.
            let Some(sandbox) = weak.upgrade() else {
                return;
            };
            sandbox.writing.set(false);
            // Dropping the last handle on the signal is what makes it, and the
            // reaction `race` left on it, collectable.
            sandbox.cancel.borrow_mut().take();
            let _source = Source::enter(sandbox.host.as_ref());
            match settled {
                Ok(_) => sandbox.flash_copied(),
                Err(_) => sandbox.warn(CLIPBOARD_FAILED),
            }
        });
    }

    /// "Copied", then back to the real label. The label is restored rather than
    /// left, because a button reading "Copied" is a button that no longer says
    /// what pressing it does.
    ///
    /// The callback is the one built at mount, and the timer replaces whatever
    /// was pending rather than joining it: one press, one timer, and nothing
    /// scheduled once the island is gone.
    fn flash_copied(&self) {
        // The write worked, so whatever `warn` last put in the validation pane
        // is over: put the state's own account back before saying so.
        self.show_state();
        if self.schedule_restore().is_none() {
            // Nothing will put the label back, so it never goes up. A button
            // reading "Copied" for the rest of the page's life is worse than
            // one that says nothing about a copy that did in fact happen.
            return;
        }
        self.copy.set_text_content(Some("Copied"));
    }

    /// Arm the timer that restores [`COPY_LABEL`], replacing any pending one.
    ///
    /// `None` when nothing could be armed, which is the caller's cue not to
    /// change the label in the first place.
    fn schedule_restore(&self) -> Option<()> {
        let window = web_sys::window()?;
        if let Some(timer) = self.flash.take() {
            window.clear_timeout_with_handle(timer);
        }
        let restore = self.restore.borrow();
        let restore = restore.as_ref()?;
        let timer = window
            .set_timeout_with_callback_and_timeout_and_arguments_0(
                restore.as_ref().unchecked_ref(),
                COPY_FEEDBACK_MS,
            )
            .ok()?;
        self.flash.set(Some(timer));
        Some(())
    }

    /// Download the preview as a standalone file.
    ///
    /// The object URL from the *previous* download is revoked here rather than
    /// at the end of the one that made it: a URL revoked in the same task as
    /// the click can be gone before the browser resolves it, and a cancelled
    /// download is a worse failure than a blob held one press too long. One is
    /// outstanding at a time, and [`Sandbox::drop`] revokes the last.
    ///
    /// Not re-entrant, and guarded rather than assumed: `click()` dispatches a
    /// real event synchronously, so a page-level click listener can call
    /// straight back in here from inside it. The inner call would revoke the
    /// URL whose click is still on the stack, cancelling the download it was
    /// nested inside.
    fn download(&self) {
        // Claimed before `update`, not after. `update` writes the preview, and
        // a DOM write runs custom-element reactions synchronously; one of them
        // clicking Download would re-enter with the flag still unset. `Guard`
        // clears it on every exit, including the early ones below.
        if self.downloading.replace(true) {
            return;
        }
        let _downloading = Guard(&self.downloading);
        let _source = Source::enter(self.host.as_ref());
        // Re-read before exporting rather than trusting the last `input`. The
        // editor is an ordinary light-DOM `<textarea>`, so a surrounding
        // `<form>` being reset changes its value without firing one, and this
        // is the one action that would otherwise write a stale résumé to disk.
        // It also puts the panel back in agreement after a `warn`.
        self.update();
        let Some(document) = self.state.borrow().document() else {
            // The old sandbox said "Nothing to export" here and left the
            // validation output showing it until the next keystroke. Same
            // wording; the next `update()` replaces it with the real reason.
            self.warn("Nothing to export. Fix validation errors first.");
            return;
        };
        if let Some(url) = self.exported.borrow_mut().take() {
            // Deferred for the same reason teardown defers: two downloads in
            // one task would otherwise revoke the first URL while the browser
            // is still resolving it.
            revoke_next_task(url);
        }
        let saved = save(&document.into_string());
        match saved {
            Ok(url) => *self.exported.borrow_mut() = Some(url),
            Err(_) => self.warn("Download failed. The browser refused the object URL."),
        }
    }

    /// Put the state's own account back in the validation pane.
    ///
    /// The counterpart to [`Sandbox::warn`]: an action's failure message is
    /// transient, and leaving one sitting under a heading that says the résumé
    /// is valid would be a panel disagreeing with itself.
    fn show_state(&self) {
        // The borrow ends on this line, before the DOM write. A `RefCell` held
        // across one is held across a custom-element reaction: a descendant's
        // `disconnectedCallback` runs synchronously inside it and can re-enter
        // this island through any of its own listeners, and `update`'s
        // `borrow_mut` would then panic — which in wasm is a trap, not an
        // unwind, so nothing on the stack gets cleaned up.
        let detail = self.state.borrow().detail();
        self.output.set_text_content(Some(&detail));
    }

    /// Put a message where the violations go.
    ///
    /// Deliberately not a `console.warn` or an `alert`: the validation pane is
    /// the place this UI already tells you things, and a second channel would
    /// be a second place to look.
    fn warn(&self, message: &str) {
        let detail = self.state.borrow().detail();
        // Above the violations, not instead of them. "Nothing to export" is the
        // answer to the button; the list underneath is the answer to "why", and
        // replacing it would take away the only thing that says what to fix.
        let text = if detail.is_empty() {
            message.to_owned()
        } else {
            format!("{message}\n\n{detail}")
        };
        self.output.set_text_content(Some(&text));
    }
}

/// Build the island inside `host`.
fn mount(host: &HtmlElement) -> Result<ResumeSandbox, JsValue> {
    let _source = Source::enter(host.as_ref());

    // A reconnect finds the furniture still standing: htmx moves elements, and
    // a move is a disconnect immediately followed by a connect. The subtree
    // travels with the element, so rebuilding it here would do nothing except
    // throw away whatever had been typed into the editor.
    let fresh = host.query_selector("[data-resume-json]")?.is_none();
    if fresh {
        // `?`, and it matters that it is reachable: a failure here happens
        // before anything is allocated, so the mount refuses cleanly and JS
        // gets a `ce-error` naming the reason. See [`set_html`].
        set_html(host.as_ref(), &skeleton().into_string())?;
    }

    let sandbox = Rc::new(Sandbox {
        host: host.clone(),
        editor: find(host, "[data-resume-json]")?,
        preview: find(host, "[data-resume-preview]")?,
        title: find(host, "[data-validation-title]")?,
        output: find(host, "[data-validation-output]")?,
        pill: find(host, "[data-render-state]")?,
        copy: find(host, "[data-copy-resume-json]")?,
        state: RefCell::new(SandboxState::NotRendered),
        restore: RefCell::new(None),
        flash: Cell::new(None),
        writing: Cell::new(false),
        cancel: RefCell::new(None),
        downloading: Cell::new(false),
        exported: RefCell::new(None),
    });
    // Paired with the decrement in `Sandbox::drop`, and placed here rather than
    // at the end of `mount` so that the pair brackets the struct's life exactly.
    // A `find` two lines below can still fail, and the count has to come back
    // down for a mount that got this far and then gave up.
    LIVE.with(|live| live.set(live.get() + 1));

    // Every long-lived callback is built here, once, and every one of them
    // holds a `Weak` — so the island's whole callback surface is a fixed cost
    // paid at mount rather than something that grows with use.
    *sandbox.restore.borrow_mut() = Some(weakly(&sandbox, |it| {
        it.copy.set_text_content(Some(COPY_LABEL));
        // The timer that called this has expired, and an expired handle is not
        // an inert one: browsers reuse timer ids, so clearing a stale one later
        // could cancel somebody else's timeout.
        it.flash.set(None);
    }));

    let reset: HtmlElement = find(host, "[data-reset-resume]")?;
    let download: HtmlElement = find(host, "[data-download-resume-html]")?;
    let listeners = vec![
        listen(sandbox.editor.as_ref(), "input", &sandbox, |it| it.update())?,
        listen(reset.as_ref(), "click", &sandbox, |it| it.reset())?,
        listen(sandbox.copy.as_ref(), "click", &sandbox, Sandbox::copy)?,
        listen(download.as_ref(), "click", &sandbox, |it| it.download())?,
    ];

    if fresh {
        // The editor opens on the sample and the panel opens agreeing with it,
        // so the first thing a visitor sees is a worked example rather than a
        // blank box and the word "blocked".
        sandbox.reset();
    } else {
        // Adopted furniture: whatever is in the editor is what the panel has to
        // agree with, and it is not necessarily the sample any more.
        sandbox.update();
    }

    Ok(ResumeSandbox {
        _sandbox: sandbox,
        _listeners: listeners,
    })
}

/// The handle JS holds, and the island's only strong reference.
///
/// `free()` from `disconnectedCallback` is what runs its destructor: the
/// listeners unhook themselves as they drop, and the last `Rc` on [`Sandbox`]
/// goes with them. Nothing else in the island holds a strong reference, so
/// there is no cycle for this to fail to break.
///
/// It has no `Drop` of its own, deliberately, and no assertion that it held the
/// last `Rc`. "The handle holds the only strong reference" is true between
/// calls and not during one: a listener has an upgraded `Rc` on the stack while
/// it runs, and `free()` can legally arrive inside that window — a page-level
/// listener on the click this island just dispatched can remove the host. A
/// count of 2 there is the `Rc` doing its job, and [`Sandbox`] correctly
/// outlives this handle by exactly the rest of that call. Everything that has
/// to happen on teardown therefore happens in [`Sandbox::drop`], which runs
/// when the island is *actually* gone rather than when JS let go of it.
#[wasm_bindgen]
pub struct ResumeSandbox {
    // Declaration order is drop order. The listeners hold only `Weak`s, so
    // either order is correct; this one frees the DOM handles first. Both are
    // held and never read: together they *are* the island, and dropping them is
    // this type's whole API.
    _sandbox: Rc<Sandbox>,
    _listeners: Vec<Listener>,
}

/// An event listener that unhooks itself when the island is dropped.
///
/// Holding the `Closure` alone is not enough. Dropping it invalidates the
/// wasm-side slot but leaves the DOM node's registration pointing at it, so a
/// node that outlives its island — a detached subtree JS still has a handle on
/// — would throw on the next event instead of doing nothing.
struct Listener {
    target: EventTarget,
    event: &'static str,
    /// `Option` only so [`Drop`] can take ownership and decide.
    closure: Option<Closure<dyn Fn()>>,
}

impl Drop for Listener {
    /// Remove first, free second, and only if the removal worked.
    ///
    /// The order is the whole point. Dropping a `Closure` invalidates the
    /// wasm-side slot behind it, so a callback the DOM still holds becomes one
    /// that traps when it is next called. `removeEventListener` can throw —
    /// through a proxied target, or a `useCapture` mismatch on an exotic host —
    /// and a registration that survived its closure is a landmine. So a failed
    /// removal leaks the closure on purpose: an unreachable `Fn` holding one
    /// dead `Weak` is a few bytes, and the alternative is a trap.
    fn drop(&mut self) {
        let Some(closure) = self.closure.take() else {
            return;
        };
        if self
            .target
            .remove_event_listener_with_callback(self.event, closure.as_ref().unchecked_ref())
            .is_err()
        {
            closure.forget();
        }
    }
}

// ------------------------------------------------------------------ helpers ---

/// Resolve one of the skeleton's parts, or say which one is missing.
///
/// The skeleton is built three functions up, so a failure here means the
/// markup and the queries have drifted apart — a bug, reported as one, rather
/// than an island that silently does nothing when you type in it.
fn find<T: JsCast>(host: &HtmlElement, selector: &str) -> Result<T, JsValue> {
    host.query_selector(selector)?
        .ok_or_else(|| error(&format!("<{TAG}> has no {selector}")))?
        .dyn_into::<T>()
        .map_err(|_| {
            error(&format!(
                "<{TAG}>: {selector} is not the element it should be"
            ))
        })
}

/// Attach a [`weakly`]-bound handler, and keep what it takes to detach it.
///
/// The `Weak` is the point. A handler holding an `Rc` would keep the island —
/// and through it the host element and its whole subtree — alive for as long
/// as the DOM node held the listener, which is exactly the cycle
/// [`ResumeSandbox::drop`] exists to not have.
fn listen(
    target: &EventTarget,
    event: &'static str,
    sandbox: &Rc<Sandbox>,
    handler: impl Fn(&Rc<Sandbox>) + 'static,
) -> Result<Listener, JsValue> {
    let closure = weakly(sandbox, handler);
    target.add_event_listener_with_callback(event, closure.as_ref().unchecked_ref())?;
    Ok(Listener {
        target: target.clone(),
        event,
        closure: Some(closure),
    })
}

/// A no-argument callback that borrows the shared [`Sandbox`] weakly.
///
/// The one place a `Weak` is turned into a callback, so "no long-lived closure
/// holds an `Rc`" is a property of this function rather than a habit spread
/// over five call sites. A callback that fires after the handle was freed finds
/// nothing to upgrade and does nothing.
///
/// The handler is handed the `Rc`, not a `&Sandbox`: [`Sandbox::copy`] needs to
/// downgrade it again for the future it spawns, and a handler that had only a
/// reference would have to reach for a second `Weak` from somewhere.
///
/// `Fn`, not `FnMut`, and that is a correctness requirement rather than a
/// preference. `wasm-bindgen` guards an `FnMut` closure with a borrow flag and
/// throws at the boundary if it is entered while already running — before any
/// Rust in it gets a turn. These handlers *can* be entered that way:
/// [`Sandbox::download`] dispatches a synthetic `click`, which runs page-level
/// listeners synchronously, and one of them clicking the Download button again
/// would re-enter this closure. An `Fn` closure is re-entrant, so the guard
/// that actually handles that case is Rust's own, inside the handler, where it
/// can decline quietly instead of throwing.
fn weakly(sandbox: &Rc<Sandbox>, handler: impl Fn(&Rc<Sandbox>) + 'static) -> Closure<dyn Fn()> {
    let weak = Rc::downgrade(sandbox);
    Closure::wrap(Box::new(move || {
        if let Some(sandbox) = weak.upgrade() {
            handler(&sandbox);
        }
    }) as Box<dyn Fn()>)
}

/// Revoke an object URL, but not in this task.
///
/// Teardown can happen *inside* the click that started a download: a page-level
/// listener on that click can remove the host, and the island is freed as soon
/// as the handler returns — still in the task the browser is going to resolve
/// the URL in. Revoking there is the one case [`Sandbox::download`]'s whole
/// revoke-the-previous-one policy exists to avoid, so the last URL is handed to
/// a zero-delay timeout instead.
///
/// `once_into_js` rather than a stored `Closure`, because the island doing the
/// storing is in the middle of ceasing to exist: this hands the callback to JS,
/// which frees it when the timer fires. Nothing outlives the timeout.
fn revoke_next_task(url: String) {
    let Some(window) = web_sys::window() else {
        let _ = Url::revoke_object_url(&url);
        return;
    };
    let revoke: Function = Closure::once_into_js(move || {
        let _ = Url::revoke_object_url(&url);
    })
    .unchecked_into();
    if window
        .set_timeout_with_callback_and_timeout_and_arguments_0(&revoke, 0)
        .is_err()
    {
        // No timer to defer to. Calling it now risks the download; not calling
        // it at all pins one blob for the life of the document *and* strands
        // the closure. Now, then — the branch is unreachable in a browser that
        // has a `window` at all.
        let _ = revoke.call0(&JsValue::NULL);
    }
}

/// `element.innerHTML = html`, as a call that can fail.
///
/// `web_sys`'s setter is infallible and its generated import does not catch, so
/// a throw from the assignment does not become an `Err` — it unwinds out of
/// wasm past every Rust destructor on the stack, stranding whatever they were
/// going to clean up. The realistic way to make that setter throw is a Trusted
/// Types CSP (`require-trusted-types-for 'script'`) with no default policy,
/// which is a deployment decision this crate does not get to make.
///
/// `Reflect::set` performs the same assignment through a binding that *is*
/// declared `catch`, so the same failure arrives as a value instead.
fn set_html(element: &Element, html: &str) -> Result<(), JsValue> {
    // `Reflect.set` answers `false` for a refusal that did not throw — a
    // read-only or non-writable property. Discarding that would report a
    // preview that was never written as one that was.
    Reflect::set(
        element.as_ref(),
        &JsValue::from_str("innerHTML"),
        &JsValue::from_str(html),
    )?
    .then_some(())
    .ok_or_else(|| error("innerHTML refused the assignment"))
}

fn error(message: &str) -> JsValue {
    JsValue::from(js_sys::Error::new(message))
}

fn document() -> Option<Document> {
    web_sys::window()?.document()
}

/// `navigator.clipboard.writeText(text)`, or `None` where there is no
/// clipboard to write to — an insecure origin, or a browser without it.
fn write_clipboard(text: &str) -> Option<Promise> {
    let navigator = web_sys::window()?.navigator();
    let clipboard = Reflect::get(navigator.as_ref(), &JsValue::from_str("clipboard")).ok()?;
    if !clipboard.is_object() {
        return None;
    }
    let write = Reflect::get(&clipboard, &JsValue::from_str("writeText"))
        .ok()?
        .dyn_into::<Function>()
        .ok()?;
    write
        .call1(&clipboard, &JsValue::from_str(text))
        .ok()?
        .dyn_into::<Promise>()
        .ok()
}

/// Hand `html` to the browser as a file, the only way a page can: a Blob, an
/// object URL, and an `<a download>` clicked and thrown away.
///
/// Returns the object URL, still live. Revoking it is the caller's, because
/// only the caller knows when the download has stopped needing it — see
/// [`Sandbox::download`].
fn save(html: &str) -> Result<String, JsValue> {
    let document = document().ok_or_else(|| error("no document"))?;
    let body = document.body().ok_or_else(|| error("no body"))?;

    let parts = js_sys::Array::of1(&JsValue::from_str(html));
    // Built as a plain object and cast, for the reason `lib.rs` gives about
    // `CustomEventInit`: the typed builder has changed shape across web-sys
    // releases and the object literal has not.
    let options = Object::new();
    Reflect::set(
        &options,
        &JsValue::from_str("type"),
        &JsValue::from_str("text/html"),
    )?;
    let blob = Blob::new_with_str_sequence_and_options(&parts, options.unchecked_ref())?;

    let url = Url::create_object_url_with_blob(&blob)?;
    // Everything from here to the click can fail, and a failure that left the
    // anchor in the body or the URL unrevoked would be a visible one: an
    // invisible empty link in the page, and a blob pinned to the document.
    click_to_download(&document, &body, &url).inspect_err(|_| {
        let _ = Url::revoke_object_url(&url);
    })?;
    Ok(url)
}

/// The `<a download>` half of [`save`], separated so its cleanup has one exit.
fn click_to_download(document: &Document, body: &HtmlElement, url: &str) -> Result<(), JsValue> {
    let anchor: HtmlAnchorElement = document.create_element("a")?.unchecked_into();
    anchor.set_href(url);
    anchor.set_download(FILENAME);
    body.append_child(&anchor)?;
    anchor.click();
    anchor.remove();
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The queries in `mount` and the markup in `skeleton` are written in two
    /// places and have to agree. They cannot be checked against a real DOM on
    /// the host target, but they can be checked against each other.
    #[test]
    fn the_skeleton_carries_every_hook_mount_looks_for() {
        let markup = skeleton().into_string();
        for hook in [
            "data-resume-json",
            "data-resume-preview",
            "data-validation-title",
            "data-validation-output",
            "data-render-state",
            "data-reset-resume",
            "data-copy-resume-json",
            "data-download-resume-html",
        ] {
            assert!(markup.contains(hook), "the skeleton has no {hook}");
        }
    }

    /// Every class the island puts in the DOM has to be one `site.css`
    /// declares, or the probe's CSSOM check reports it on every page view.
    #[test]
    fn the_skeleton_uses_only_contract_classes() {
        let markup = skeleton().into_string();
        let allowed = ["type-xs", "type-sm", "type-md", "text-muted"];
        for class in markup
            .split("class=\"")
            .skip(1)
            .filter_map(|rest| rest.split('"').next())
            .flat_map(str::split_whitespace)
        {
            assert!(allowed.contains(&class), "{class} is not in the allowlist");
        }
    }

    /// No `style=` anywhere. The island's furniture is semantic or it is
    /// nothing; a hard-coded value here is a value no contract authorised.
    #[test]
    fn the_skeleton_inlines_no_styles() {
        assert!(!skeleton().into_string().contains("style="));
    }

    #[test]
    fn the_copy_button_opens_on_its_resting_label() {
        assert!(skeleton().into_string().contains(COPY_LABEL));
    }
}
