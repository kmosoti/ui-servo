//! `/about` — the longer version of the home page's Work section: where the
//! work has actually been, what I build when nobody is paying for it, and the
//! same three links `/` carries.
//!
//! The contact block is [`super::home::contact_list`] rather than a second copy
//! of the same three list items. Two hand-maintained contact blocks stay in
//! agreement right up until one of them doesn't.

use crate::layout::{PageMeta, shell};
use crate::state::AppState;
use maud::{Markup, html};

pub fn render(state: &AppState) -> Markup {
    shell(
        state,
        PageMeta {
            title: "About",
            description: "Kennedy Mosoti — the observability platform work, what came before it, and what I build outside it.",
            route: "/about",
        },
        html! {
            // This page's join key, on a wrapper rather than on the first
            // article: the probe resolves an element-targeted reading by
            // walking up to the nearest `[data-span-id]`, so an id that covered
            // only the opening paragraphs would leave three quarters of the
            // page resolving to the probe's fallback instead of to `/about`.
            // No fragment is mounted here to inherit one from, and a slot
            // nothing ever swaps into would be a lie about what this is for.
            div data-span-id=(crate::span::new_span_id()) {
                article class="my-xl" {
                    h1 class="type-2xl" { "About" }
                    p class="type-md" {
                        "I'm Kennedy Mosoti. I work on observability platforms — the logging "
                        "systems everything else reports into — and most of what I do there is "
                        "automation: making a change safe to roll out across a few hundred "
                        "machines instead of typing it a few hundred times."
                    }
                    p class="text-muted" {
                        "I'm in Dallas–Fort Worth, Texas. I studied software engineering at the "
                        "University of Texas at Arlington, and I'm certified on Splunk (Admin, "
                        "Power User) and Cribl, plus AWS Solutions Architect – Associate."
                    }
                }

                section class="my-xl" {
                    h2 class="type-md text-muted" { "Where I've worked" }
                    p {
                        "Since August 2024 I've been an associate observability engineer at "
                        "Netbuilder, contracting on the log platform at JPMorgan Chase in Plano, "
                        "Texas. It's a centralised platform for multi-tenant logging — Splunk, "
                        "Logstash, SaltStack, and a lot of Linux underneath."
                    }
                    ul class="gap-sm" {
                        li {
                            "I automated search-filter updates across 3,000+ Splunk roles in "
                            "Python, which replaced a manual path that was easy to get wrong "
                            "with a rollout you can repeat."
                        }
                        li {
                            "I work on the Salt-based Splunk automation: login-banner "
                            "orchestration across 100+ search heads, repave design support for "
                            "15 on-prem search head cluster nodes, and duplicate detection in "
                            "the onboarding and Salt configuration data so the platform drifts "
                            "less between deployments."
                        }
                        li {
                            "On ingestion I work the Kafka-to-Logstash-to-Splunk path — "
                            "onboarding and sync analysis, stale Kafka topic cleanup, and "
                            "remediating Logstash consumers when they stop keeping up."
                        }
                        li {
                            "I investigated the disaster-recovery readiness gaps around the "
                            "cluster manager and the deployer: app parity bottlenecked on NAS, "
                            "a cluster manager with no failover, search head cluster readiness "
                            "with no clear signal. I also did the root cause and remediation "
                            "on farm breaks, including a 40-host reboot."
                        }
                    }
                    p class="text-muted" {
                        "Before that I was an associate solutions architect at Amazon Web "
                        "Services in 2023, working with enterprise customers on availability, "
                        "security, scalability and performance, and I built a cost estimation "
                        "tool for cloud migrations. Earlier: a software engineering internship at "
                        "NCR Voyix and help desk support at ServiceLink — Python automation for "
                        "mobile testing workflows, plus web application, account access, SQL "
                        "reporting and production support requests."
                    }
                    p class="text-muted" {
                        "Since January 2024 I've also evaluated AI-generated code and reasoning "
                        "for Data Annotation Tech, across Python, JavaScript and SQL."
                    }
                }

                section class="my-xl" {
                    h2 class="type-md text-muted" { "Outside work" }
                    p {
                        "Small tools, mostly, and mostly about getting a system to say what it "
                        "is actually doing. I've been running local models with Python, uv and "
                        "Hugging Face Transformers, but I spend more of that time on the "
                        "unglamorous half: explicit tool contracts, context retrieval, and "
                        "boundaries that keep a coding agent inside what it was handed. "
                        "Infrastructure work fails sooner or later, and I want to be able to see "
                        "where."
                    }
                    p class="text-muted" {
                        "This site is one of those tools. The pages are rendered from Rust, and "
                        "the part I care about is the build around them: the colours, the type "
                        "scale and the motion are derived from one file rather than typed into "
                        "the markup, and a change has to get past that before anyone spends "
                        "time judging how it looks."
                    }
                    p class="text-muted" {
                        "The rest is smaller: Terraform and Python experiments for provisioning "
                        "AWS the same way twice, and a notes repo where the patterns I keep "
                        "reusing live."
                    }
                }

                section class="my-xl" {
                    h2 class="type-md text-muted" { "Contact" }
                    p { "Email is the surest way to reach me." }
                    (super::home::contact_list())
                }
            }
        },
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::pages::home;

    fn page() -> String {
        render(&AppState::for_tests(false)).into_string()
    }

    /// The page renders as a document, under the right nav entry. `route` is the
    /// one `PageMeta` field nothing else would catch: get it wrong and the page
    /// is fine except that the nav says you are somewhere else.
    #[test]
    fn the_about_page_renders_under_its_own_nav_entry() {
        let body = page();
        assert!(body.starts_with("<!DOCTYPE html>"));
        assert!(body.contains("<h1 class=\"type-2xl\">About</h1>"));
        assert!(
            body.contains("<a href=\"/about\" aria-current=\"page\">About</a>"),
            "the nav does not mark /about as current"
        );
    }

    /// One join key, and it encloses the whole page rather than merely opening
    /// first.
    ///
    /// "A span id exists somewhere in the document" is the assertion that lets
    /// the id drift onto the opening article and quietly orphan every section
    /// after it; "it opens before the first heading" is the assertion that lets
    /// the wrapper close early and do the same thing. Both ends are checked.
    #[test]
    fn one_join_key_encloses_the_whole_page() {
        let body = page();
        assert_eq!(
            body.matches("data-span-id=\"").count(),
            1,
            "the about page should carry exactly one span id"
        );
        let span = body
            .find("data-span-id=\"")
            .expect("the page carries a span id");
        assert!(
            span < body.find("<h1").expect("the page has a heading"),
            "the span id opens after the content it is meant to cover"
        );

        // No fragment is mounted here, so the wrapper is this page's only div
        // and its close is the last one in the document.
        let wrapper_closes = body.rfind("</div>").expect("the wrapper closes");
        let last_section = body.rfind("</section>").expect("the page has sections");
        assert!(
            last_section < wrapper_closes,
            "the page's span closes before the sections it is supposed to cover"
        );
        assert_ne!(page(), page(), "span ids must be fresh per render");
    }

    /// The page still has a page on it: three sections, each with a body.
    ///
    /// Headings rather than sentences, because copy gets rewritten and a test
    /// that pins a paragraph is a test that gets deleted — but a heading alone
    /// would pass over a section gutted down to its `<h2>`, so each one is
    /// checked for prose between its heading and its close.
    #[test]
    fn all_three_sections_render_with_a_body() {
        let body = page();
        for heading in ["Where I've worked", "Outside work", "Contact"] {
            let opening = format!("<h2 class=\"type-md text-muted\">{heading}</h2>");
            let at = body
                .find(&opening)
                .unwrap_or_else(|| panic!("the about page has lost its {heading} section"));
            let rest = &body[at + opening.len()..];
            let end = rest.find("</section>").expect("the section closes");
            assert!(
                rest[..end].contains("<p"),
                "the {heading} section is a heading with nothing under it"
            );
        }
    }

    /// Not "a contact block is present" — *the* contact block, byte for byte the
    /// one `/` renders. This is the assertion that makes `contact_list` worth
    /// sharing: if the two pages ever drift, one of them stops containing the
    /// other's markup and this fails.
    #[test]
    fn the_contact_block_is_the_one_the_home_page_renders() {
        let shared = home::contact_list().into_string();
        assert!(
            page().contains(&shared),
            "the about page's contact block has drifted from the home page's"
        );
    }

    /// The résumé is the only long-form thing the site points at, and it is
    /// linked from both pages — this is the assertion that `/about` keeps its
    /// half.
    #[test]
    fn the_resume_pdf_is_linked_from_the_about_page() {
        assert!(page().contains(home::RESUME));
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

    /// `/projects` and `/writing` are permanent redirects. A link to either one
    /// still "works" — it just costs a second request and lands somewhere the
    /// visitor did not ask for, which is exactly why nothing catches it by hand.
    #[test]
    fn nothing_links_to_a_retired_route() {
        let body = page();
        for (index, _) in body.match_indices("href=\"") {
            let value = &body[index + 6..];
            let value = &value[..value.find('"').expect("href is quoted")];
            // Prefix, not equality: `/projects/`, `/projects?ref=x` and
            // `/writing#top` are the same retired page wearing a hat, and the
            // trailing-slash form does not even reach the redirect.
            for retired in ["/projects", "/writing"] {
                assert!(
                    value != retired && !value.starts_with(&format!("{retired}/")),
                    "the about page links to {value}, which is the retired {retired}"
                );
                assert!(
                    !value.starts_with(&format!("{retired}?"))
                        && !value.starts_with(&format!("{retired}#")),
                    "the about page links to {value}, which is the retired {retired}"
                );
            }
        }
    }
}
