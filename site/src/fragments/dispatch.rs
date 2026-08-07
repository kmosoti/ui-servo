//! `dispatch` — what is on the bench right now.

use maud::{Markup, html};

pub fn render() -> Markup {
    super::frame(
        "dispatch",
        "Current dispatch",
        html! {
            p class="type-sm text-accent-2" { "Dispatch — no. 041" }
            h2 class="type-lg" { "Teaching a control loop to have taste" }
            p class="text-muted" {
                "The premise: aesthetic judgement is a setpoint, not a vibe. Write the "
                "direction down once, derive every token from it, then let a probe in the "
                "browser tell you — in milliseconds and easing curves — where the rendered "
                "page has drifted off it."
            }
            p class="type-sm text-muted" {
                "Bench notes, mostly wrong, occasionally load-bearing."
            }
        },
    )
}
