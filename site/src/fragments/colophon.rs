//! `colophon` — how the page you are looking at was made.

use maud::{Markup, html};

pub fn render() -> Markup {
    super::frame(
        "colophon",
        "Colophon",
        html! {
            p class="type-sm text-accent" { "Colophon" }
            p class="text-muted" {
                "Server-rendered from Rust: "
                code { "maud" } " compiles this paragraph into the binary. "
                code { "htmx" } " swaps the pieces that change. No client framework, no "
                "hydration step, no bundle to apologise for."
            }
            p class="text-muted" {
                "Type is set in a serif for display and a plain text face for prose. "
                "Colour is warm dark with an amber primary and one cyan counterweight. "
                "The exact values are not choices made here — they are read out of the "
                "direction contract at build time."
            }
            p class="type-sm text-muted" {
                "Motion: four durations, three easings, transform and opacity only. "
                "If your system asks for less, you get none."
            }
        },
    )
}
