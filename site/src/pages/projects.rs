//! `/projects` — a short list, honestly annotated.

use crate::fragments;
use crate::layout::{PageMeta, shell};
use crate::state::AppState;
use maud::{Markup, html};

pub fn render(state: &AppState) -> Markup {
    shell(
        state,
        PageMeta {
            title: "Projects",
            description: "Things built and still running, with the parts that did not work left in.",
            route: "/projects",
        },
        html! {
            article class="my-xl" {
                h1 class="type-2xl" { "Projects" }
                p class="text-muted" {
                    "Short list on purpose. A project earns a line here once it has survived "
                    "contact with somebody who was not me, and it keeps the line only while "
                    "it still runs."
                }
            }

            section class="my-xl" {
                (super::slot("slot-project", fragments::render("project-card").unwrap_or_default()))
                (super::reswap_button("project-card", "slot-project", "Re-render this card"))
            }

            section class="my-xl" {
                h2 class="type-md text-muted" { "Archive" }
                ul class="gap-sm" {
                    li {
                        span class="text-text" { "jsonl-evidence" }
                        " — "
                        span class="text-muted" { "append-only store for machine observations. Boring by design; the boringness is the feature." }
                    }
                    li {
                        span class="text-text" { "beacon-ingest" }
                        " — "
                        span class="text-muted" { "a 204-and-shut-up endpoint. Everything interesting happens after the response." }
                    }
                    li {
                        span class="text-text" { "the-first-three-attempts" }
                        " — "
                        span class="text-muted" { "retired. Tried to score taste with a model and no sensor; scored confidence instead." }
                    }
                }
            }
        },
    )
}
