//! The document shell every full-page response is poured into.
//!
//! Fragments never go through here — an htmx swap must return exactly the
//! fragment and nothing else — so this file is the one place that decides what
//! a *page* is: semantic landmarks, a skip link, the token sheet, and (in dev)
//! the sensor runtime.

use crate::state::AppState;
use maud::{DOCTYPE, Markup, PreEscaped, html};

/// The two places this site goes. Order is the nav order.
///
/// It was four. `/projects` and `/writing` held placeholder lists — invented
/// titles, invented dates — and a nav entry is a promise that there is
/// something behind it. Both are permanent redirects to `/` now, and the entries
/// are gone rather than pointing at a redirect: a nav link that bounces is a
/// second request and a lie about where you are.
pub const NAV: [(&str, &str); 2] = [("/", "Index"), ("/about", "About")];

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
                // SVG only, no PNG fallback: every browser that runs the wasm
                // island on this site also reads an SVG favicon.
                link rel="icon" type="image/svg+xml" href="/assets/favicon.svg";
                (pwa_head())
                // Owner-allowed exception (batch, 2026-08-09): tokens.css has
                // always declared "JetBrains Mono" for this shell's monospace
                // stack, but nothing loaded the webfont — /about rendered the
                // system fallback instead of the font its own tokens ask for.
                // Loading it here, before tokens.css, makes /about render what
                // it already claims to.
                link rel="stylesheet" href="/assets/fonts/fonts.css";
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

/// The golden-path shell: the document chrome of
/// `experiments/Kennedy Mosoti - Portfolio.dc.html`, ported verbatim.
///
/// The classic [`shell`] stays for `/about` (the resume-sandbox island lives
/// there); every page of the portfolio proper goes through this one. Values
/// are the golden file's own — the sticky header, the fixed margin canvases,
/// the résumé menu, the print-only résumé document — and behaviour comes from
/// `/assets/portfolio.js`, the vanilla port of the golden component.
pub fn portfolio_shell(state: &AppState, meta: PageMeta<'_>, body: Markup) -> Markup {
    html! {
        (DOCTYPE)
        html lang="en" {
            head {
                meta charset="utf-8";
                meta name="viewport" content="width=device-width, initial-scale=1";
                meta name="color-scheme" content="dark";
                meta name="description" content=(meta.description);
                title { (meta.title) " — kennedy mosoti" }
                link rel="icon" type="image/svg+xml" href="/assets/favicon.svg";
                (pwa_head())
                link rel="stylesheet" href="/assets/fonts/fonts.css";
                link rel="stylesheet" href="/assets/portfolio.css";
                script src="/assets/portfolio.js" defer {}
                @if state.dev() {
                    (probe_boot(state))
                }
            }
            body {
                div data-print-reset="1" style="position:relative; min-height:100vh; background:#08090b; color:#ece7dd; font-family:'Public Sans',system-ui,sans-serif; font-size:16px; line-height:1.6; overflow-x:clip;" {
                    a class="pf-skip" href="#main" { "Skip to content" }

                    // Owner tuning (2026-08-09): brighter than the golden .6,
                    // and the strips FILL the margin — from the viewport edge
                    // to 24px shy of the 1080px content column — so their
                    // width scales with the viewport (golden: fixed 190px at
                    // the edges). portfolio.js re-seeds density on resize.
                    // Owner ruling (2026-08-09, later the same day): the scene
                    // in the strips is now the warp starfield, chosen over the
                    // ancestor's node graph + glyph rain and two other
                    // candidates — see makeWarp in portfolio.js.
                    canvas class="pf-canvas" id="pf-canvas-l" data-print-hide="1" style="position:fixed; top:0; left:0; width:max(76px, calc((100vw - 1128px) / 2)); height:100vh; pointer-events:none; z-index:0; opacity:.85; display:none;" {}
                    canvas class="pf-canvas" id="pf-canvas-r" data-print-hide="1" style="position:fixed; top:0; right:0; width:max(76px, calc((100vw - 1128px) / 2)); height:100vh; pointer-events:none; z-index:0; opacity:.85; display:none;" {}

                    (portfolio_header(meta.route))

                    main id="main" data-print-hide="1" style="position:relative; z-index:1;" {
                        div data-span-id=(crate::span::new_span_id()) {
                            (body)
                        }
                    }

                    (portfolio_footer())
                    (print_resume_doc())
                }
            }
        }
    }
}

/// The sticky header: brand dot, route nav, direction toggle, motion toggle,
/// résumé menu. Active-state colours are server-rendered for the current
/// route; portfolio.js only touches the toggles.
fn portfolio_header(route: &str) -> Markup {
    let nav_style = |here: bool| {
        if here {
            "padding:6px 11px; border-radius:4px; font-family:'JetBrains Mono',monospace; font-size:12px; letter-spacing:.04em; text-decoration:none; color:#ff7a45; background:rgba(255,122,69,.1);"
        } else {
            "padding:6px 11px; border-radius:4px; font-family:'JetBrains Mono',monospace; font-size:12px; letter-spacing:.04em; text-decoration:none; color:#9aa0a7; background:transparent;"
        }
    };
    html! {
        header data-print-hide="1" style="position:sticky; top:0; z-index:60; display:flex; align-items:center; justify-content:space-between; gap:24px; padding:14px clamp(20px,4vw,44px); background:rgba(8,9,11,.86); backdrop-filter:blur(16px); border-bottom:1px solid #1b1f24;" {
            a href="/" style="display:flex; align-items:center; gap:10px; color:#ece7dd; font-family:'JetBrains Mono',monospace; font-size:13px; font-weight:700; letter-spacing:.06em; text-decoration:none;" {
                span style="width:7px; height:7px; border-radius:50%; background:#ff7a45; box-shadow:0 0 10px 2px rgba(255,122,69,.55);" {}
                "KM"
            }
            nav aria-label="Primary" style="display:flex; align-items:center; gap:4px; flex-wrap:wrap;" {
                a class="pf-hover-text" href="/" style=(nav_style(route == "/")) { "profile" }
                a class="pf-hover-text" href="/projects/blackcell" style=(nav_style(route == "/projects/blackcell")) { "blackcell" }
                a class="pf-hover-text" href="/projects/splunk-dashboard-studio" style=(nav_style(route == "/projects/splunk-dashboard-studio")) { "dashboard-studio" }
            }
            div style="display:flex; align-items:center; gap:10px;" {
                button id="pf-motion" title="reduced motion" style="padding:6px 10px; border:1px solid #24282e; border-radius:5px; cursor:pointer; background:transparent; font-family:'JetBrains Mono',monospace; font-size:11px; color:#ff7a45;" { "motion on" }
                div style="position:relative;" {
                    button id="pf-resume-btn" style="padding:6px 12px; border:1px solid #ff7a45; border-radius:5px; cursor:pointer; background:rgba(255,122,69,.1); font-family:'JetBrains Mono',monospace; font-size:11px; font-weight:700; color:#ff7a45;" { "résumé ↓" }
                    div id="pf-resume-menu" style="position:absolute; top:38px; right:0; width:230px; padding:6px; border:1px solid #24282e; border-radius:6px; background:#101215; box-shadow:0 18px 40px rgba(0,0,0,.6); display:none;" {
                        button class="pf-print pf-hover-row" style="display:block; width:100%; text-align:left; padding:9px 10px; border:0; border-radius:4px; cursor:pointer; background:transparent; color:#ece7dd; font-family:'JetBrains Mono',monospace; font-size:12px;" { "print / save as PDF" }
                        button class="pf-json pf-hover-row" style="display:block; width:100%; text-align:left; padding:9px 10px; border:0; border-radius:4px; cursor:pointer; background:transparent; color:#ece7dd; font-family:'JetBrains Mono',monospace; font-size:12px;" { "download résumé.json" }
                        div style="padding:6px 10px 4px; color:#6e747b; font-family:'JetBrains Mono',monospace; font-size:10.5px; line-height:1.5;" { "both generated from the same source data as this page" }
                    }
                }
            }
        }
    }
}

fn portfolio_footer() -> Markup {
    html! {
        footer data-print-hide="1" style="position:relative; z-index:1; max-width:1080px; margin:0 auto; padding:28px clamp(20px,4vw,44px) 56px; border-top:1px solid #1b1f24; display:flex; flex-wrap:wrap; gap:16px; justify-content:space-between; align-items:center; font-family:'JetBrains Mono',monospace; font-size:12px; color:#6e747b;" {
            span { "kennedy mosoti — observability platform engineer" }
            span style="display:flex; gap:18px;" {
                a href="https://github.com/kmosoti" target="_blank" rel="noopener" { "github.com/kmosoti" }
                a class="pf-print" href="#" { "résumé (pdf)" }
                a class="pf-json" href="#" { "résumé.json" }
            }
        }
    }
}

/// The print-only résumé document: hidden on screen, the only thing visible
/// under `@media print`. Generated from the same facts as the page, verbatim
/// from the golden file.
fn print_resume_doc() -> Markup {
    let h = |text: &'static str| {
        html! { div style="font-size:9pt; font-weight:700; letter-spacing:.1em; text-transform:uppercase; color:#111; margin:0 0 5pt;" { (text) } }
    };
    html! {
        div data-print-doc="1" style="display:none; background:#fff; color:#111; font-family:'Public Sans',system-ui,sans-serif; font-size:10.5pt; line-height:1.45; padding:0;" {
            div style="border-bottom:1.5pt solid #111; padding-bottom:8pt; margin-bottom:12pt;" {
                div style="font-size:22pt; font-weight:800; letter-spacing:-.02em;" { "Kennedy Mosoti" }
                div style="font-size:10.5pt; color:#444; margin-top:2pt;" { "Observability Platform Engineer — branching into agentic engineering" }
                div style="font-family:'JetBrains Mono',monospace; font-size:8.5pt; color:#555; margin-top:5pt;" { "github.com/kmosoti" }
            }

            (h("Summary"))
            p style="margin:0 0 12pt; color:#333;" { "Observability platform engineer supporting a centralized, multi-tenant Splunk/Logstash/Kafka platform. Automation in Python, Salt, and Terraform; reliability work across HA/DR readiness, configuration-drift detection, and root-cause analysis. Currently extending the same instrumentation discipline to agent-driven systems." }

            (h("Experience"))
            div style="margin-bottom:9pt;" {
                div style="display:flex; justify-content:space-between; gap:12pt;" { strong { "Netbuilder (JPMC LogA Platform)" } span style="color:#555; font-size:9pt;" { "Aug 2024 — present" } }
                div style="color:#444; font-style:italic; margin-bottom:2pt;" { "Associate Observability Engineer" }
                div style="color:#333;" { "Support a centralized, multi-tenant observability platform — Splunk, Logstash, SaltStack, Linux. Work the Kafka → Logstash → Splunk ingestion path: onboarding and sync analysis, stale-topic cleanup, consumer remediation. Automated search-filter updates across 3,000+ Splunk roles in Python, and login-banner orchestration across 100+ search heads in Salt — plus SHC repave design for 15 on-prem nodes. Duplicate-detection to catch config drift before it ships, DR-readiness investigation into cluster-manager failover and NAS bottlenecks, and RCA on farm-break findings down to SELinux-context drift." }
            }
            div style="margin-bottom:9pt;" {
                div style="display:flex; justify-content:space-between; gap:12pt;" { strong { "Data Annotation Tech" } span style="color:#555; font-size:9pt;" { "Jan 2024 — present" } }
                div style="color:#444; font-style:italic; margin-bottom:2pt;" { "AI Code Evaluation Contractor" }
                div style="color:#333;" { "Evaluate AI-generated code and reasoning — Python, JavaScript, SQL — for correctness, efficiency, maintainability, edge-case handling, and whether it actually did what it was asked." }
            }
            div style="margin-bottom:9pt;" {
                div style="display:flex; justify-content:space-between; gap:12pt;" { strong { "Amazon Web Services" } span style="color:#555; font-size:9pt;" { "Jan 2023 — Nov 2023" } }
                div style="color:#444; font-style:italic; margin-bottom:2pt;" { "Associate Solutions Architect" }
                div style="color:#333;" { "Advised enterprise customers on AWS architecture — availability, security, scalability, performance. Built a cloud-migration cost-estimation tool and several cloud-native prototypes." }
            }
            div style="margin-bottom:12pt;" {
                div style="display:flex; justify-content:space-between; gap:12pt;" { strong { "NCR Voyix / ServiceLink" } span style="color:#555; font-size:9pt;" { "early career" } }
                div style="color:#444; font-style:italic; margin-bottom:2pt;" { "Software Engineering Intern & IT Help Desk Support" }
                div style="color:#333;" { "Python automation scripts for mobile-testing workflows at NCR Voyix; web-application, account-access, and SQL-reporting support at ServiceLink." }
            }

            (h("Selected Projects"))
            div style="margin-bottom:12pt; color:#333;" {
                div style="margin-bottom:3pt;" { strong { "BlackCell" } " (pre-alpha) — Local-first, evidence-gated control runtime for coding agents. Bounded retries, isolated worktrees, append-only evidence in SQLite." }
                div style="margin-bottom:3pt;" { strong { "splunk-dashboard-studio" } " (alpha) — Pydantic 2 compiler/validator for Splunk Dashboard Studio, version-aware 9.4–10.4, differential validation against Splunk's official validator." }
                div style="margin-bottom:3pt;" { strong { "Kernform" } " (alpha) — Deterministic project scaffolding and repo-conformance tool. Rust core, PyO3 bridge, Python SDK/CLI." }
                div style="margin-bottom:3pt;" { strong { "PraxisLedger" } " — Provenance and temporal knowledge graph. SQLite + Rust + Python." }
                div style="margin-bottom:3pt;" { strong { "SAI" } " (shipped) — Agent routing modeled on brain-network dynamics; episodic/semantic memory, async consolidation loop." }
                div { strong { "learning-os" } " — Adaptive personal-learning app. FastAPI + SQLAlchemy." }
            }

            (h("Skills"))
            div style="margin-bottom:12pt; color:#333;" {
                div { strong { "Observability" } " — Splunk, Logstash, Kafka, Cribl, RED, USE, SRE, RCA, metrics, alerts, traces, events, logging" }
                div { strong { "Programming" } " — Python, Rust, Bash" }
                div { strong { "Architecture" } " — Hexagonal architecture, event-driven architecture, self-healing systems" }
                div { strong { "Configuration" } " — SaltStack, Terraform, scaffolding & conformance" }
                div { strong { "Infrastructure" } " — Linux, AWS, VMware/vSphere, HA/DR readiness, configuration-drift detection" }
                div { strong { "AI-assisted engineering" } " — AI code evaluation, context engineering, OpenTelemetry & tracing, harness engineering" }
            }

            (h("Education & Certifications"))
            div style="color:#333;" { "B.S. Software Engineering, University of Texas at Arlington · Splunk Certified Admin · Splunk Power User · Cribl Certified User · AWS Certified Solutions Architect — Associate" }
        }
    }
}

/// The installable identity both shells share: manifest, apple touch icon,
/// theme colour, and the two apple-specific standalone-mode hints. One
/// function rather than one copy per shell, so the two cannot drift the way a
/// hand-copied block eventually does — see [`probe_boot`] for the same move
/// made for the sensor script.
fn pwa_head() -> Markup {
    html! {
        link rel="manifest" href="/assets/manifest.webmanifest";
        link rel="apple-touch-icon" href="/assets/icons/apple-touch-180.png";
        meta name="theme-color" content="#08090b";
        meta name="apple-mobile-web-app-capable" content="yes";
        meta name="apple-mobile-web-app-status-bar-style" content="black-translucent";
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
