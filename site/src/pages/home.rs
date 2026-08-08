//! `/` — the index: the promoted hero, then what the work is and how to reach a
//! human. Two sections below the hero, deliberately. A portfolio that lists
//! everything is a portfolio nobody finishes reading.

use crate::fragments;
use crate::layout::{PageMeta, shell};
use crate::state::AppState;
use maud::{Markup, html};

/// The three places a visitor can go from here. They live in one place because
/// `/about` links to the same three, and a contact address that is right on one
/// page and stale on the other is worse than one that is missing from both.
pub const EMAIL: &str = "kennedy.rmosoti@gmail.com";
pub const GITHUB: &str = "https://github.com/kmosoti";
/// Served as `application/pdf` (`server::static_asset`, and copied byte for
/// byte by the static export), so it opens rather than prompting a download.
pub const RESUME: &str = "/assets/kennedy-mosoti-resume.pdf";

/// `Email — <a>`, `GitHub — <a>`, `Résumé — <a>`. Shared with `/about` so the
/// two pages cannot disagree about where to find the same person.
pub fn contact_list() -> Markup {
    html! {
        ul class="gap-sm" {
            li { "Email — " a href=(format!("mailto:{EMAIL}")) { (EMAIL) } }
            li { "GitHub — " a href=(GITHUB) { "github.com/kmosoti" } }
            li { "Résumé — " a href=(RESUME) { "PDF" } }
        }
    }
}

pub fn render(state: &AppState) -> Result<Markup, crate::error::RouteError> {
    // A hero that exists but cannot prove it was gated fails the page rather
    // than quietly rendering the placeholder: "nobody has picked yet" and
    // "someone edited the pick" must not look the same to a visitor.
    let hero = fragments::promoted::render_or_placeholder(|part| state.promoted(part), "hero")
        .map_err(|error| crate::error::RouteError::UngatedPromotion(error.to_string()))?;
    Ok(shell(
        state,
        PageMeta {
            title: "Index",
            description: "Kennedy Mosoti — observability engineer in Dallas–Fort Worth. What I work on, and how to get in touch.",
            route: "/",
        },
        html! {
            // The page-level join key. The probe resolves an element-targeted
            // reading by walking up to the nearest `[data-span-id]`, so without
            // this wrapper the sections below the hero fall through to the
            // probe's last-resort attribution instead of to the page they are
            // on. The promoted fragment nests its own id inside this one, and
            // the nearer of the two wins, so hero readings still land on the
            // hero.
            div data-span-id=(crate::span::new_span_id()) {
                // The hero is the gauntlet's own output when a pick has been
                // promoted, and the hand-written placeholder until then. Same
                // frame, same sensors either way — a promoted fragment is not a
                // special case of a page, it is just the page.
                @if let Some(promoted) = hero {
                    (promoted)
                } @else {
                // Only ever seen before the first promotion. It says so, rather
                // than impersonating a pick nobody made.
                article class="my-xl" {
                    p class="type-sm text-accent-2" { "Dallas–Fort Worth, Texas" }
                    h1 class="type-display" { "Kennedy Mosoti" }
                    p class="type-md" {
                        "I'm an observability engineer. I work on the log platform a large "
                        "bank runs on, and on smaller tools the rest of the time."
                    }
                    p class="text-muted" {
                        "Nothing has been promoted into this spot yet, so this is the "
                        "fallback copy — the page still renders, and it tells you which one "
                        "you are looking at."
                    }
                }
                }

                section class="my-xl" {
                    h2 class="type-md text-muted" { "Work" }
                    p {
                        "I'm an observability engineer at Netbuilder, on the log platform at "
                        "JPMorgan Chase in Plano, Texas. It's a Splunk platform big enough "
                        "that most things have to be automated, and automating them is most "
                        "of what I do."
                    }
                    ul class="gap-sm" {
                        li {
                            "Access — search-filter updates across about 3,000 Splunk roles, "
                            "done in Python instead of by hand."
                        }
                        li {
                            "Rollout — Salt-driven automation across 100+ search heads, so a "
                            "platform-wide change is one job rather than a hundred."
                        }
                        li {
                            "Ingestion — Kafka into Logstash into Splunk: stale-topic "
                            "cleanup, and Logstash consumers when they stop keeping up."
                        }
                        li {
                            "Recovery — I wrote up the disaster-recovery readiness gaps "
                            "around the cluster manager and the deployer, and did the root "
                            "cause and remediation on farm breaks, including a 40-host "
                            "reboot."
                        }
                    }
                    p class="text-muted" {
                        "Outside work I build small tools — inspection utilities, and "
                        "boundaries that keep coding agents inside what they were handed. "
                        "This site's build system is one of them: every change goes through "
                        "automated checks before anyone judges how it looks."
                    }
                }

                section class="my-xl" {
                    h2 class="type-md text-muted" { "Contact" }
                    p { "I'm in Dallas–Fort Worth, Texas. Email is the surest way to reach me." }
                    (contact_list())
                }
            }
        },
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn page() -> String {
        render(&AppState::for_tests(false))
            .expect("the committed hero verifies")
            .into_string()
    }

    /// The page's whole job below the hero is to be reachable-from. A contact
    /// block that renders the address as text, or points at a résumé that
    /// moved, fails silently — it still looks like a contact block.
    #[test]
    fn the_contact_block_is_present_and_clickable() {
        let body = page();
        for href in [
            "href=\"mailto:kennedy.rmosoti@gmail.com\"",
            "href=\"https://github.com/kmosoti\"",
            "href=\"/assets/kennedy-mosoti-resume.pdf\"",
        ] {
            assert!(body.contains(href), "the home page is missing {href}");
        }
        // The address is also readable without a mouse over the link.
        assert!(body.contains(EMAIL));
    }

    /// Every `href` is one of the four *shapes* a link on this site may take:
    /// a site path, an in-page anchor, a mail address, or an https origin.
    /// Catches the empty `href=""`, a bare `mailto:` with no address, and a
    /// plaintext http link.
    ///
    /// Shape, not resolution — `/nowhere` passes here. Whether a site path
    /// exists is the export's link check (`export::tests`), which walks every
    /// internal reference against the files it actually wrote.
    #[test]
    fn every_href_is_well_formed() {
        let body = page();
        for (index, tail) in body.match_indices("href=\"") {
            let value = &body[index + 6..];
            let value = &value[..value.find('"').expect("href is quoted")];
            assert!(!value.is_empty(), "empty href at byte {index} ({tail})");
            let ok = value.starts_with('/')
                || value.starts_with('#')
                || value
                    .strip_prefix("mailto:")
                    .is_some_and(|to| to.contains('@'))
                || value.starts_with("https://");
            assert!(ok, "{value:?} is not a href this page can honour");
        }
    }

    /// The page's own join key opens before the hero's *and closes after the
    /// last section*, so every element on the page has a span-bearing ancestor.
    ///
    /// Opening order alone is not enclosure: a wrapper closed immediately after
    /// the hero would still open first, and Work and Contact would still
    /// render, and they would resolve to whatever the probe falls back to
    /// rather than to this page. Where it closes is the half worth asserting.
    #[test]
    fn the_page_join_key_encloses_the_hero_and_the_prose() {
        let body = page();
        // On the wrapper itself, not merely first in the document: strip the
        // attribute off the `div` and the hero's own id becomes the earliest
        // one, which is enough to satisfy every ordering assertion below while
        // leaving Work and Contact with no span-bearing ancestor at all.
        let wrapper = body.find("<div ").expect("the page opens a wrapper");
        let tag_end = body[wrapper..].find('>').expect("the tag closes") + wrapper;
        assert!(
            body[wrapper..tag_end].contains("data-span-id=\""),
            "the page wrapper does not carry the join key: {}",
            &body[wrapper..tag_end]
        );

        let page_span = body.find("data-span-id=\"").expect("the page carries one");
        let hero = body
            .find("data-fragment=\"hero\"")
            .expect("the committed hero renders");
        assert!(page_span < hero, "the hero's span id is the outer one");

        // The wrapper is the outermost `div` in the body, so its close is the
        // last one however many the promoted hero nests inside it.
        let wrapper_closes = body.rfind("</div>").expect("the wrapper closes");
        let last_section = body.rfind("</section>").expect("the page has sections");
        assert!(
            last_section < wrapper_closes,
            "the page's span closes before the sections it is supposed to cover"
        );
    }

    /// Work and Contact both render, each with a body under the heading.
    ///
    /// The hero comes from a promoted fragment, so without this every home-page
    /// assertion below the hero would survive the two sections being deleted:
    /// the wrapper would still enclose the hero, the contact block would be the
    /// only thing missing, and the link tests pass on a page with no links.
    #[test]
    fn both_sections_render_with_a_body() {
        let body = page();
        for heading in ["Work", "Contact"] {
            let opening = format!("<h2 class=\"type-md text-muted\">{heading}</h2>");
            let at = body
                .find(&opening)
                .unwrap_or_else(|| panic!("the home page has lost its {heading} section"));
            let rest = &body[at + opening.len()..];
            let end = rest.find("</section>").expect("the section closes");
            assert!(
                rest[..end].contains("<p"),
                "the {heading} section is a heading with nothing under it"
            );
        }
    }

    /// The résumé is the only long-form thing the site points at, and it is
    /// linked from here as well as from `/about` — this is the assertion that
    /// the home page keeps its half.
    #[test]
    fn the_resume_pdf_is_linked_from_the_home_page() {
        assert!(page().contains(RESUME));
    }
}
