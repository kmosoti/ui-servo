//! `/writing` — essays and bench notes, newest first.

use crate::fragments;
use crate::layout::{PageMeta, shell};
use crate::state::AppState;
use maud::{Markup, html};

/// Placeholder index. Real posts arrive as content, not as code.
const POSTS: [(&str, &str, &str); 4] = [
    (
        "2026-07-28",
        "A setpoint you can argue with",
        "Design systems fail at the point where the token stops being enforceable. Here is where that point usually is.",
    ),
    (
        "2026-06-11",
        "The sensor is the hard part",
        "Everyone builds the generator first. The generator is the easy half and it is the half that lies to you.",
    ),
    (
        "2026-04-02",
        "Motion as a set-membership test",
        "Four durations and three easings turn 'does this feel right' into a question a machine can answer in one frame.",
    ),
    (
        "2026-02-19",
        "Against the median",
        "Every model trained on the web converges on the same grey card with the same rounded corner. Naming the anti-reference is half the fix.",
    ),
];

pub fn render(state: &AppState) -> Markup {
    shell(
        state,
        PageMeta {
            title: "Writing",
            description: "Essays on control loops, measurable taste, and the parts of interface work that resist measurement.",
            route: "/writing",
        },
        html! {
            article class="my-xl" {
                h1 class="type-2xl" { "Writing" }
                p class="text-muted" {
                    "Long-form when the idea earns it, bench notes when it does not. "
                    "Nothing here is a listicle and nothing here has a newsletter signup."
                }
            }

            section class="my-xl" {
                ul class="gap-md" {
                    @for (date, title, blurb) in POSTS {
                        li class="py-sm" {
                            p class="type-sm text-accent-2" { time datetime=(date) { (date) } }
                            h2 class="type-md" { a href="/writing" { (title) } }
                            p class="text-muted type-sm" { (blurb) }
                        }
                    }
                }
            }

            section class="my-xl" {
                h2 class="type-md text-muted" { "Reading" }
                (super::slot("slot-reading", fragments::render("reading-log").unwrap_or_default()))
                (super::reswap_button("reading-log", "slot-reading", "Re-render the reading log"))
            }
        },
    )
}
