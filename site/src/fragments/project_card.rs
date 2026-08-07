//! `project-card` — one project, stated plainly.

use maud::{Markup, html};

pub fn render() -> Markup {
    super::frame(
        "project-card",
        "Project: ui-servo",
        html! {
            p class="type-sm text-accent" { "Active — since 2026" }
            h2 class="type-lg" { "ui-servo" }
            p class="text-muted" {
                "A servo loop for interface quality. A written contract is the reference "
                "signal, a browser probe is the sensor, and a generate-and-test gauntlet is "
                "the actuator. The interesting part is not the generation; it is refusing to "
                "accept output that the sensors say is off-contract."
            }
            dl class="type-sm" {
                dt class="text-muted" { "Stack" }
                dd { "Rust, axum, maud, htmx, wasm islands, Python for the loop" }
                dt class="text-muted" { "Open question" }
                dd { "How much taste survives being made machine-checkable" }
            }
        },
    )
}
