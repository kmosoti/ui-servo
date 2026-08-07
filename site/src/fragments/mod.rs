//! Fragments: the gauntlet's unit of work.
//!
//! A fragment is a pure function from nothing (for now) to `Markup`. It is what
//! the explorer mutates, what the preview server serves standalone, what the
//! probe measures, and what htmx swaps into a live page. Three rules hold for
//! every one of them, and [`frame`] is where they are enforced rather than
//! remembered:
//!
//! 1. **One root element**, a `<section>`, carrying a fresh `data-span-id`.
//!    A swap with two roots has no join key and no measurable element timing.
//! 2. **Allowlisted classes only** — the utility vocabulary that
//!    `DirectionContract.class_allowlist_seed()` derives from the tokens, all of
//!    which exist in `site.css`. A class no token can justify is an unreviewed
//!    escape hatch, and the probe's CSSOM check will report it as one.
//! 3. **Motion through tokens only** — `var(--motion-duration-*)` and
//!    `var(--motion-ease-*)`, on `transform`/`opacity`. Hand-typed durations are
//!    off-contract by construction.

use crate::span::new_span_id;
use maud::{Markup, html};

mod colophon;
mod constellation;
mod dispatch;
pub mod promoted;
mod project_card;
mod reading_log;

/// Every fragment the site can serve, in the order they appear in the docs.
pub const NAMES: [&str; 5] = [
    "dispatch",
    "project-card",
    "reading-log",
    "colophon",
    "constellation",
];

/// Render a fragment by name. `None` is a 404, not a panic: the name arrives
/// from a URL.
pub fn render(name: &str) -> Option<Markup> {
    match name {
        "dispatch" => Some(dispatch::render()),
        "project-card" => Some(project_card::render()),
        "reading-log" => Some(reading_log::render()),
        "colophon" => Some(colophon::render()),
        "constellation" => Some(constellation::render()),
        _ => None,
    }
}

/// The fragment root. Nothing in `src/fragments/` builds its own outer element.
///
/// `elementtiming` is what lets the probe attribute a `PerformanceElementTiming`
/// entry to this exact swap; `data-fragment` is what lets a human reading the
/// evidence file know which function produced the span.
///
/// **The frame carries no visual identity, only rhythm.** It used to add
/// `p-md border-border`, and that was a quiet violation of the loop's whole
/// premise in two ways. First, the preview shell a candidate is judged in does
/// not apply the frame, so every candidate was assessed as bare markup and then
/// served inside chrome nobody had judged — the measured artefact and the
/// shipped artefact were not the same object. Second, the chrome it added was a
/// bordered panel, which is precisely what `direction.toml` names as an
/// anti-reference and what both critic families cited, by class, when they threw
/// out the card-shaped hero in round 4. The site was serving the thing it had
/// just rejected, on every fragment.
///
/// So padding, borders and background belong to the fragment body, where the
/// class-0 gate can see them and the panel can judge them. What stays here is
/// the vertical rhythm between fragments, which is the page's business rather
/// than any one fragment's, and the four measurement attributes.
pub fn frame(name: &str, label: &str, body: Markup) -> Markup {
    html! {
        section
            data-span-id=(new_span_id())
            data-fragment=(name)
            elementtiming=(name)
            aria-label=(label)
            class="my-lg"
        {
            (body)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_named_fragment_renders() {
        for name in NAMES {
            assert!(render(name).is_some(), "{name} is listed but not wired");
        }
    }

    #[test]
    fn unknown_fragments_are_none() {
        assert!(render("../etc/passwd").is_none());
    }

    #[test]
    fn every_fragment_root_carries_a_span_id() {
        for name in NAMES {
            let markup = render(name).unwrap().into_string();
            assert!(
                markup.starts_with("<section data-span-id=\""),
                "{name} does not open with a span-id-bearing section: {markup:.80}"
            );
            assert_eq!(
                markup.matches("<section").count(),
                markup.matches("data-span-id").count(),
                "{name} has a section without a span id"
            );
        }
    }

    /// The frame is a measurement device, not a design decision. If it grows
    /// visual classes again, candidates go back to being judged as one thing and
    /// served as another — and the specific classes below are the ones the
    /// direction contract names as an anti-reference.
    #[test]
    fn the_frame_imposes_no_visual_chrome() {
        let framed = frame("t", "t", html! { "body" }).into_string();
        let root = framed.split_once('>').unwrap().0;
        let classes = root
            .split_once("class=\"")
            .map(|(_, rest)| rest.split_once('"').unwrap().0)
            .unwrap_or("");
        // An allowlist. A denylist of seven class names only stops the seven
        // somebody thought of, and the whole failure being guarded against is
        // chrome nobody noticed arriving.
        for class in classes.split_whitespace() {
            assert!(
                class.starts_with("my-"),
                "the frame applies {class:?} to every fragment, including ones judged without \
                 it. Only vertical rhythm (my-*) belongs here: padding, borders, backgrounds \
                 and type are the fragment's own business, where the class-0 gate can read \
                 them and the panel can judge them. Got: {classes:?}"
            );
        }
    }

    #[test]
    fn span_ids_are_fresh_per_render() {
        let first = render("dispatch").unwrap().into_string();
        let second = render("dispatch").unwrap().into_string();
        assert_ne!(first, second);
    }
}
