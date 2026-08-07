//! `/about` — the position, at length, plus how to reach a human.

use crate::fragments;
use crate::layout::{PageMeta, shell};
use crate::state::AppState;
use maud::{Markup, html};

pub fn render(state: &AppState) -> Markup {
    shell(
        state,
        PageMeta {
            title: "About",
            description: "Who is writing this, what they think interfaces are for, and how to get in touch.",
            route: "/about",
        },
        html! {
            article class="my-xl" {
                h1 class="type-2xl" { "About" }
                p class="type-md" {
                    "I am Kennedy. I write control systems and I write interfaces, and I have "
                    "stopped pretending those are different jobs."
                }
                p class="text-muted" {
                    "The through-line: a system that cannot observe its own output cannot be "
                    "held to a standard. Most interface tooling stops at the generator — a "
                    "component library, a theme, a prompt — and then relies on a human to "
                    "notice the drift. Humans notice drift late and inconsistently. Sensors "
                    "notice it on every render."
                }
                p class="text-muted" {
                    "So the work is unglamorous: write the taste down in one file, derive "
                    "every value from it, put a probe in the browser that reports what the "
                    "page actually did, and make the failing case loud. What is left over "
                    "after all of that — the part no probe can score — is the part worth "
                    "spending a Saturday on."
                }
            }

            section class="my-xl" {
                h2 class="type-md text-muted" { "Elsewhere" }
                ul class="gap-sm" {
                    li { "Email — " code { "kennedy.rmosoti@gmail.com" } }
                    li { "Code — " a href="/projects" { "the projects page" } ", mostly" }
                    li { "Talks — " span class="text-muted" { "none scheduled, which is the correct number" } }
                }
            }

            section class="my-xl" {
                h2 class="type-md text-muted" { "How this page is built" }
                (super::slot("slot-about-colophon", fragments::render("colophon").unwrap_or_default()))
            }
        },
    )
}
