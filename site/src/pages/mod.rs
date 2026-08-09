//! Pages: the two server-rendered documents. Each one is a shell plus prose,
//! plus whatever fragments have somewhere real to be.
//!
//! Right now that is one: the promoted hero on `/`. The named fragments in
//! `crate::fragments` are demo content left from the placeholder copy — a
//! dispatch numbered 041, a reading log, a project card — and mounting them
//! under real work-history prose would put invented material back on the page
//! the moment a visitor read past the first section. [`slot`] and
//! [`reswap_button`] stay because they are how the *next* fragment gets
//! mounted, not because something is mounted through them today.
//!
//! Pages are *not* the gauntlet's unit of work — fragments are. A page exists
//! to give a fragment somewhere real to live, with real neighbours, so that a
//! measurement taken on it is a measurement of the site and not of a test
//! harness.
//!
//! There were four. `projects` and `writing` were placeholder lists with
//! invented titles and invented dates, which is the one thing a site whose
//! argument is "measure what you actually shipped" cannot have on it. They are
//! redirects in the route manifest now. A page comes back when there is
//! content for it.

use crate::span::new_span_id;
use maud::{Markup, html};

pub mod about;
pub mod blackcell;
pub mod deepdive;
pub mod home;
pub mod splunk_studio;

/// A swap target. Carries its own span id so that an htmx swap always resolves
/// to *some* join key, even in the instant between removing the old fragment
/// root and inserting the new one.
pub fn slot(id: &str, body: Markup) -> Markup {
    html! {
        div id=(id) data-span-id=(new_span_id()) class="my-md" { (body) }
    }
}

/// The "swap this fragment again" control, in the one shape the whole site uses.
///
/// `innerHTML` into a slot rather than `outerHTML` on the fragment root: the
/// slot survives the swap, so the target of `htmx:afterSwap` is a stable
/// element that already has a span id.
pub fn reswap_button(fragment: &str, target_id: &str, label: &str) -> Markup {
    html! {
        button
            type="button"
            hx-get=(format!("/fragments/{fragment}"))
            hx-target=(format!("#{target_id}"))
            hx-swap="innerHTML"
            class="type-sm px-sm py-xs border-border text-accent"
        {
            (label)
        }
    }
}
