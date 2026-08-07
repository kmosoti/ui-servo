//! `/` — the index. A statement of position, then the current dispatch.

use crate::fragments;
use crate::layout::{PageMeta, shell};
use crate::state::AppState;
use maud::{Markup, html};

pub fn render(state: &AppState) -> Markup {
    shell(
        state,
        PageMeta {
            title: "Index",
            description: "Kennedy Mosoti — control systems, interfaces, and the argument that taste can be measured.",
            route: "/",
        },
        html! {
            // The hero is the gauntlet's own output when a pick has been
            // promoted, and the hand-written placeholder until then. Same frame,
            // same span id, same sensors either way — a promoted fragment is not
            // a special case of a page, it is just the page.
            @if let Some(promoted) = fragments::promoted::render_or_placeholder(state.assets_dir(), "hero") {
                (promoted)
            } @else {
            article class="my-xl" {
                p class="type-sm text-accent-2" { "Nairobi · building in the open" }
                h1 class="type-display" { "Kennedy Mosoti" }
                p class="type-md" {
                    "I build systems that hold a standard when nobody is watching them. "
                    "Lately that means control loops for interface quality: write the taste "
                    "down, instrument the browser, and let the evidence — not the argument — "
                    "decide what ships."
                }
                p class="text-muted" {
                    "This page is the specimen. It is rendered by the same server the loop "
                    "steers, from tokens the contract derives, and it is watched by the same "
                    "probe that judges every candidate the gauntlet produces. If the site "
                    "drifts, the site is the first thing to say so."
                }
            }
            }

            section class="my-xl" {
                h2 class="type-md text-muted" { "Now" }
                (super::slot("slot-dispatch", fragments::render("dispatch").unwrap_or_default()))
                (super::reswap_button("dispatch", "slot-dispatch", "Re-render this dispatch"))
                p class="type-sm text-muted my-sm" {
                    "That button swaps a server-rendered fragment. Watch the span id change: "
                    "it is the join key every sensor reading is filed under."
                }
            }

            section class="my-xl" {
                h2 class="type-md text-muted" { "The island" }
                (super::slot("slot-constellation", fragments::render("constellation").unwrap_or_default()))
                p class="type-sm text-muted my-sm" {
                    "One WASM component, mounted in a fragment like everything else here, "
                    "so the sensors have a span to file its evidence under."
                }
            }

            section class="my-xl" {
                h2 class="type-md text-muted" { "Colophon" }
                (super::slot("slot-colophon", fragments::render("colophon").unwrap_or_default()))
            }
        },
    )
}
