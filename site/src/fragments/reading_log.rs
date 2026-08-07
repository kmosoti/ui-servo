//! `reading-log` — what has been read lately, and what stuck.

use maud::{Markup, html};

pub fn render() -> Markup {
    super::frame(
        "reading-log",
        "Reading log",
        html! {
            p class="type-sm text-accent-2" { "Reading log" }
            ul class="gap-sm" {
                li {
                    span class="text-text" { "Feedback Systems" }
                    " — Åström & Murray. "
                    span class="text-muted" { "Read for the vocabulary; stayed for the reminder that a loop without a sensor is just an opinion with a schedule." }
                }
                li {
                    span class="text-text" { "The Elements of Typographic Style" }
                    " — Bringhurst. "
                    span class="text-muted" { "Every argument I have about measure and leading is downstream of chapter two." }
                }
                li {
                    span class="text-text" { "Notes on the Synthesis of Form" }
                    " — Alexander. "
                    span class="text-muted" { "Misfit variables. The whole gauntlet is a misfit counter wearing a nicer coat." }
                }
            }
        },
    )
}
