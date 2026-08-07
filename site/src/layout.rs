//! The document shell every full-page response is poured into.
//!
//! Fragments never go through here — an htmx swap must return exactly the
//! fragment and nothing else — so this file is the one place that decides what
//! a *page* is: semantic landmarks, a skip link, the token sheet, and (in dev)
//! the sensor runtime.

use crate::state::AppState;
use maud::{DOCTYPE, Markup, PreEscaped, html};

/// The four places this site goes. Order is the nav order.
pub const NAV: [(&str, &str); 4] = [
    ("/", "Index"),
    ("/projects", "Projects"),
    ("/writing", "Writing"),
    ("/about", "About"),
];

/// Per-page metadata the shell needs and the page body cannot supply itself.
pub struct PageMeta<'a> {
    /// Goes in `<title>`, before the site name.
    pub title: &'a str,
    /// `<meta name="description">`.
    pub description: &'a str,
    /// Which nav entry is `aria-current`.
    pub route: &'a str,
}

/// Wrap a page body in the document shell.
pub fn shell(state: &AppState, meta: PageMeta<'_>, body: Markup) -> Markup {
    html! {
        (DOCTYPE)
        html lang="en" {
            head {
                meta charset="utf-8";
                meta name="viewport" content="width=device-width, initial-scale=1";
                meta name="color-scheme" content=(state.color_scheme());
                meta name="description" content=(meta.description);
                title { (meta.title) " — kmosoti" }
                link rel="stylesheet" href="/assets/tokens.css";
                link rel="stylesheet" href="/assets/site.css";
                script src="/assets/htmx.min.js" defer {}
                // The island loader defines <ui-constellation> and nothing
                // else; the wasm is fetched lazily, on first upgrade. Carrying
                // it on every page is what lets htmx swap an island into a page
                // that did not start with one.
                script type="module" src="/assets/islands/loader.js" {}
                @if state.dev() {
                    (probe_boot(state))
                }
            }
            body {
                a href="#main" { "Skip to content" }
                header {
                    p { a href="/" { "kmosoti" } }
                    nav aria-label="Primary" {
                        ul {
                            @for (href, label) in NAV {
                                li {
                                    @if href == meta.route {
                                        a href=(href) aria-current="page" { (label) }
                                    } @else {
                                        a href=(href) { (label) }
                                    }
                                }
                            }
                        }
                    }
                }
                main id="main" { (body) }
                footer {
                    p class="text-muted type-sm" {
                        "Set in the ember-terminal direction. Every value on this page comes from "
                        code { "direction/direction.toml" } "."
                    }
                }
            }
        }
    }
}

/// The dev-only sensor boot: config first, then the probe that reads it.
fn probe_boot(state: &AppState) -> Markup {
    html! {
        script {
            (PreEscaped(format!("window.__UI_SERVO__={};", state.probe_config())))
        }
        script src="/assets/probe.js" defer {}
    }
}
