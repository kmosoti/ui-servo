//! `constellation` — the site's one WASM island, given somewhere to live.
//!
//! The fragment is deliberately thin: a heading, the custom element, and a
//! caption. Everything visible inside `<ui-constellation>` is drawn by
//! `site/islands/` once `assets/islands/loader.js` upgrades the element, and
//! until then the element is an empty box that costs nothing.
//!
//! It is a fragment rather than a lump of page markup so that the island
//! inherits the same `data-span-id` join key every other measured thing has —
//! a `ce-error` or a `wasm-panic` raised in here files under the same span as
//! the swap that put it on screen.

use maud::{Markup, html};

pub fn render() -> Markup {
    super::frame(
        "constellation",
        "Project constellation",
        html! {
            h2 class="type-md" { "Constellation" }
            ui-constellation
                role="img"
                aria-label="Nine linked nodes drifting slowly, reacting to the pointer."
            {}
            p class="type-sm text-muted" {
                "A WASM island. It reads its colours from the same custom properties "
                "the rest of the page does, holds still when the system asks for reduced "
                "motion, and reports its own failures — a panic in there arrives at the "
                "beacon as "
                code { "wasm-panic" }
                ", attributed to this span."
            }
        },
    )
}
