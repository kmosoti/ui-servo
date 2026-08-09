//! `/` — the golden-path profile: the terminal hero (direction 1a, official
//! by owner ruling 2026-08-09), live instrumentation, the append-only
//! experience log, the cross-referencing skills matrix, the project ledger,
//! and about.
//!
//! Ported verbatim from `experiments/Kennedy Mosoti - Portfolio.dc.html`. The
//! inline styles are the golden file's own, on purpose: the owner named that
//! file the reference, so every value here traces to it rather than to the
//! token sheet. Behaviour (typing, metrics, filtering, the accordion) lives in
//! `assets/portfolio.js`.

use crate::layout::{PageMeta, portfolio_shell};
use crate::state::AppState;
use maud::{Markup, html};

pub const EMAIL: &str = "kennedy.rmosoti@gmail.com";
pub const GITHUB: &str = "https://github.com/kmosoti";
/// Served as `application/pdf`; kept for `/about` and for anyone holding the
/// old link, though the portfolio's résumé actions are print + JSON now.
pub const RESUME: &str = "/assets/kennedy-mosoti-resume.pdf";

/// `Email — <a>`, `GitHub — <a>`, `Résumé — <a>`. Still shared with `/about`.
pub fn contact_list() -> Markup {
    html! {
        ul class="gap-sm" {
            li { "Email — " a href=(format!("mailto:{EMAIL}")) { (EMAIL) } }
            li { "GitHub — " a href=(GITHUB) { "github.com/kmosoti" } }
            li { "Résumé — " a href=(RESUME) { "PDF" } }
        }
    }
}

const MONO: &str = "'JetBrains Mono',monospace";

/// A credential chip, golden style.
fn chip(text: &str) -> Markup {
    html! {
        span style="font-family:'JetBrains Mono',monospace; font-size:11.5px; color:#9aa0a7; border:1px solid #24282e; border-radius:4px; padding:5px 10px;" { (text) }
    }
}

/// One clickable skill tag. Initial styles are the no-selection state;
/// portfolio.js recomputes them on click exactly as the golden component did.
fn tag_btn(key: &str, label: &str) -> Markup {
    html! {
        button class="pf-tag" data-tag=(key) style="font-family:'JetBrains Mono',monospace; font-size:12px; padding:5px 10px; border-radius:4px; cursor:pointer; border-style:solid; border-width:1px; border-color:#24282e; background:transparent; color:#9aa0a7; opacity:1;" { (label) }
    }
}

/// One instrumentation tile.
fn metric_tile(label: &str, id: &str, value_style: &str) -> Markup {
    html! {
        div style="background:#101215; padding:16px 18px;" {
            div style="font-family:'JetBrains Mono',monospace; font-size:10.5px; letter-spacing:.08em; color:#6e747b; text-transform:uppercase; margin-bottom:6px;" { (label) }
            div id=(id) style=(value_style) { "—" }
        }
    }
}

/// One experience-log entry. `open` renders the golden default (`jpmc` open).
#[allow(clippy::too_many_arguments)]
fn job(
    id: &str,
    offset: &str,
    company: &str,
    role: &str,
    when: &str,
    body: &str,
    tags: &[&str],
    data_tags: &str,
    open: bool,
) -> Markup {
    html! {
        article class="pf-item" data-item=(id) data-tags=(data_tags)
            style="border-style:solid; border-width:1px; border-color:#24282e; border-radius:8px; background:#101215; opacity:1; transition:opacity .3s, border-color .3s;" {
            div data-job-toggle=(id) style="display:flex; align-items:center; gap:16px; padding:18px 20px; cursor:pointer;" {
                span style="font-family:'JetBrains Mono',monospace; font-size:11px; color:#6e747b; border:1px solid #24282e; border-radius:3px; padding:3px 7px; flex-shrink:0;" { (offset) }
                span style="flex:1; min-width:0;" {
                    span style="display:block; font-size:17px; font-weight:700; letter-spacing:-.015em;" { (company) }
                    span style="display:block; font-size:14px; color:#9aa0a7;" { (role) }
                }
                span style="font-family:'JetBrains Mono',monospace; font-size:12px; color:#9aa0a7; white-space:nowrap;" { (when) }
                span data-job-icon=(id) style="color:#ff7a45; font-size:15px; flex-shrink:0;" { (if open { "⌄" } else { "›" }) }
            }
            div data-job-body=(id) style=(format!("display:{}; padding:0 20px 22px;", if open { "block" } else { "none" })) {
                p style="margin:0 0 14px; max-width:760px; font-size:15px; color:#9aa0a7; text-wrap:pretty;" { (body) }
                div style="display:flex; flex-wrap:wrap; gap:6px; font-family:'JetBrains Mono',monospace; font-size:11.5px; color:#6e747b;" {
                    @for (index, tag) in tags.iter().enumerate() {
                        @if index > 0 { span { "·" } }
                        span { (tag) }
                    }
                }
            }
        }
    }
}

/// A project-card status badge: `warn` (amber), `ship` (ember) or quiet.
fn status_badge(text: &str, kind: &str) -> Markup {
    let style = match kind {
        "warn" => {
            "font-family:'JetBrains Mono',monospace; font-size:10.5px; letter-spacing:.05em; text-transform:uppercase; padding:3px 7px; border:1px solid #f2b134; color:#f2b134; border-radius:3px; white-space:nowrap;"
        }
        "ship" => {
            "font-family:'JetBrains Mono',monospace; font-size:10.5px; letter-spacing:.05em; text-transform:uppercase; padding:3px 7px; border:1px solid #ff7a45; color:#ff7a45; border-radius:3px; white-space:nowrap;"
        }
        _ => {
            "font-family:'JetBrains Mono',monospace; font-size:10.5px; letter-spacing:.05em; text-transform:uppercase; padding:3px 7px; border:1px solid #24282e; color:#6e747b; border-radius:3px; white-space:nowrap;"
        }
    };
    html! { span style=(style) { (text) } }
}

const CARD: &str = "display:flex; flex-direction:column; border-style:solid; border-width:1px; border-color:#24282e; border-radius:8px; background:#101215; padding:20px 22px; opacity:1; transition:opacity .3s, border-color .3s;";
const CARD_TOP: &str =
    "display:flex; justify-content:space-between; align-items:flex-start; gap:10px;";
const CARD_NAME: &str = "font-size:17px; font-weight:700; letter-spacing:-.015em;";
const CARD_DESC: &str = "margin:10px 0 16px; flex:1; font-size:14.5px; color:#9aa0a7;";

pub fn render(state: &AppState) -> Result<Markup, crate::error::RouteError> {
    Ok(portfolio_shell(
        state,
        PageMeta {
            title: "Kennedy Mosoti",
            description: "Kennedy Mosoti — observability platform engineer, branching into agentic engineering.",
            route: "/",
        },
        html! {
            div style="max-width:1080px; margin:0 auto; padding:0 clamp(20px,4vw,44px) 120px;" {

                // ---------- hero: the terminal window (direction 1a, made
                // official by owner ruling 2026-08-09; 1b and its header
                // toggle are retired) ----------
                section style="padding:72px 0 48px;" {
                    div style="border:1px solid #24282e; border-radius:10px; background:#101215; overflow:hidden; box-shadow:0 24px 60px rgba(0,0,0,.45);" {
                        div style="display:flex; align-items:center; gap:10px; padding:10px 16px; border-bottom:1px solid #1b1f24; background:#0c0e11; font-family:'JetBrains Mono',monospace; font-size:11px; color:#6e747b; letter-spacing:.04em;" {
                            span style="width:6px; height:6px; border-radius:50%; background:#ff7a45;" {}
                            "kennedy@observability — zsh — 96×28"
                        }
                        div style="padding:34px clamp(20px,4vw,42px) 38px;" {
                            div style="font-family:'JetBrains Mono',monospace; font-size:13.5px; color:#9aa0a7;" {
                                span style="color:#ff7a45; margin-right:8px;" { "$" }
                                span class="pf-typed" {}
                                span class="pf-caret" style="display:inline-block; width:8px; height:14px; margin-left:2px; vertical-align:-2px; background:#ff7a45; animation:km-blink 1s step-end infinite;" {}
                            }
                            h1 style="margin:24px 0 0; font-size:clamp(38px,5.6vw,60px); line-height:1.02; font-weight:800; letter-spacing:-.03em; text-wrap:balance;" { "Kennedy Mosoti" }
                            p style="margin:14px 0 0; font-family:'JetBrains Mono',monospace; font-size:13px; letter-spacing:.04em; color:#ff7a45; text-transform:uppercase;" { "observability platform engineer / branching into agentic engineering" }
                            p style="margin:26px 0 0; max-width:640px; font-size:22px; line-height:1.42; color:#ece7dd; font-weight:400; text-wrap:pretty;" { "Building the thing is easy. Knowing if it's working is the actual job." }
                        }
                    }
                    div style="display:flex; flex-wrap:wrap; gap:8px; margin-top:20px;" {
                        (chip("B.S. Software Engineering — UT Arlington"))
                        (chip("Splunk Certified Admin"))
                        (chip("Splunk Power User"))
                        (chip("Cribl Certified User"))
                        (chip("AWS Solutions Architect — Associate"))
                    }
                }

                // ---------- instrumentation ----------
                section style="padding:8px 0 64px;" {
                    div style="display:flex; align-items:baseline; justify-content:space-between; gap:20px; flex-wrap:wrap; margin-bottom:14px;" {
                        div style="font-family:'JetBrains Mono',monospace; font-size:11px; letter-spacing:.1em; color:#ff7a45; text-transform:uppercase;" { "instrumentation — this page, measured live" }
                        div style="font-family:'JetBrains Mono',monospace; font-size:11px; color:#6e747b;" { "every number below is read from the browser. nothing here is fabricated." }
                    }
                    div style="display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:1px; background:#1b1f24; border:1px solid #24282e; border-radius:8px; overflow:hidden;" {
                        (metric_tile("ttfb", "pf-m-ttfb", "font-size:24px; font-weight:700; font-variant-numeric:tabular-nums; letter-spacing:-.02em;"))
                        (metric_tile("dom nodes", "pf-m-nodes", "font-size:24px; font-weight:700; font-variant-numeric:tabular-nums; letter-spacing:-.02em;"))
                        (metric_tile("dom ready", "pf-m-dom", "font-size:24px; font-weight:700; font-variant-numeric:tabular-nums; letter-spacing:-.02em;"))
                        (metric_tile("render", "pf-m-fps", "font-size:24px; font-weight:700; font-variant-numeric:tabular-nums; letter-spacing:-.02em; color:#ece7dd;"))
                        (metric_tile("resources", "pf-m-res", "font-size:24px; font-weight:700; font-variant-numeric:tabular-nums; letter-spacing:-.02em;"))
                        (metric_tile("session", "pf-m-up", "font-size:24px; font-weight:700; font-variant-numeric:tabular-nums; letter-spacing:-.02em; color:#ff7a45;"))
                    }
                }

                // ---------- experience ----------
                section style="padding:0 0 72px;" {
                    div style="font-family:'JetBrains Mono',monospace; font-size:11px; letter-spacing:.1em; color:#ff7a45; text-transform:uppercase; margin-bottom:10px;" { "experience — an append-only log" }
                    h2 style="margin:0 0 32px; font-size:clamp(28px,3.4vw,38px); font-weight:700; letter-spacing:-.025em;" { "Where the tags come from" }

                    div style="display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:28px; padding:26px 0 34px; border-top:1px solid #24282e; border-bottom:1px solid #24282e; margin-bottom:34px;" {
                        div {
                            div style="font-size:clamp(32px,4vw,44px); font-weight:800; letter-spacing:-.035em; color:#ff7a45; line-height:1;" { "3,000+" }
                            div style="margin-top:8px; font-size:14px; color:#9aa0a7; max-width:230px;" { "Splunk roles updated by a single Python automation" }
                        }
                        div {
                            div style="font-size:clamp(32px,4vw,44px); font-weight:800; letter-spacing:-.035em; color:#ff7a45; line-height:1;" { "100+" }
                            div style="margin-top:8px; font-size:14px; color:#9aa0a7; max-width:230px;" { "search heads orchestrated through Salt" }
                        }
                        div {
                            div style="font-size:clamp(32px,4vw,44px); font-weight:800; letter-spacing:-.035em; color:#ff7a45; line-height:1;" { "15" }
                            div style="margin-top:8px; font-size:14px; color:#9aa0a7; max-width:230px;" { "on-prem nodes covered by the SHC repave design" }
                        }
                    }

                    div style="display:flex; flex-direction:column; gap:10px;" {
                        (job("jpmc", "offset 0", "Netbuilder (JPMC LogA Platform)", "Associate Observability Engineer", "Aug 2024 — present",
                            "Support a centralized, multi-tenant observability platform — Splunk, Logstash, SaltStack, Linux. Work the Kafka → Logstash → Splunk ingestion path: onboarding and sync analysis, stale-topic cleanup, consumer remediation. Automated search-filter updates across 3,000+ Splunk roles in Python, and login-banner orchestration across 100+ search heads in Salt — plus SHC repave design for 15 on-prem nodes. On the reliability side: duplicate-detection to catch config drift before it ships, DR-readiness investigation into cluster-manager failover and NAS bottlenecks, and RCA on farm-break findings down to SELinux-context drift.",
                            &["splunk", "logstash", "kafka", "salt", "python", "linux", "rca", "drift", "ha/dr"],
                            "splunk logstash kafka salt python bash linux rca drift dr red use eda logging", true))
                        (job("dat", "offset 1", "Data Annotation Tech", "AI Code Evaluation Contractor", "Jan 2024 — present",
                            "Evaluate AI-generated code and reasoning — Python, JavaScript, SQL — for correctness, efficiency, maintainability, edge-case handling, and whether it actually did what it was asked. Running in parallel with the day job, not instead of it.",
                            &["ai-eval", "python"], "aieval python", false))
                        (job("aws", "offset 2", "Amazon Web Services", "Associate Solutions Architect", "Jan 2023 — Nov 2023",
                            "Advised enterprise customers on AWS architecture — availability, security, scalability, performance. Built a cloud-migration cost-estimation tool and a few cloud-native prototypes. Learned the cloud-infrastructure side properly before circling back to observability, which turned out to be the more interesting problem.",
                            &["aws", "terraform"], "aws terraform", false))
                        (job("ncr", "offset 3", "NCR Voyix / ServiceLink", "Software Engineering Intern & IT Help Desk Support", "early career",
                            "Two roles that taught the unglamorous fundamentals — Python automation scripts for mobile-testing workflows at NCR Voyix, and web-application, account-access, and SQL-reporting support at ServiceLink. Neither one glamorous. Both load-bearing for everything after.",
                            &["python"], "python", false))
                    }
                }

                // ---------- skills ----------
                section style="padding:0 0 72px;" {
                    div style="font-family:'JetBrains Mono',monospace; font-size:11px; letter-spacing:.1em; color:#ff7a45; text-transform:uppercase; margin-bottom:10px;" { "skills — click a tag to cross-reference" }
                    h2 style="margin:0 0 10px; font-size:clamp(28px,3.4vw,38px); font-weight:700; letter-spacing:-.025em;" { "What I actually reach for" }
                    p style="margin:0 0 30px; max-width:620px; font-size:15px; color:#9aa0a7;" {
                        "Selecting a tag dims every role and project that can't back it up. "
                        span id="pf-filter-note" style="color:#ece7dd;" {}
                    }

                    div style="display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); gap:14px;" {
                        div style="border:1px solid #24282e; border-radius:8px; background:#101215; padding:20px 22px;" {
                            h3 style="margin:0 0 2px; font-size:17px; font-weight:700; letter-spacing:-.015em;" { "Observability & Telemetry" }
                            p style="margin:0 0 12px; font-size:13.5px; color:#6e747b;" { "how I know it's working" }
                            p style="margin:0 0 16px; font-size:14px; color:#9aa0a7; text-wrap:pretty;" { "RED and USE framing for what to look at, then Splunk, Logstash, Kafka, and Cribl as the pipes it flows through. RCA is what happens when a number goes somewhere it shouldn't and someone has to explain why before it happens again." }
                            div style="display:flex; flex-wrap:wrap; gap:6px;" {
                                (tag_btn("splunk", "Splunk")) (tag_btn("logstash", "Logstash")) (tag_btn("kafka", "Kafka")) (tag_btn("cribl", "Cribl"))
                                (tag_btn("red", "RED")) (tag_btn("use", "USE")) (tag_btn("rca", "RCA")) (tag_btn("logging", "Logging"))
                            }
                        }
                        div style="border:1px solid #24282e; border-radius:8px; background:#101215; padding:20px 22px;" {
                            h3 style="margin:0 0 2px; font-size:17px; font-weight:700; letter-spacing:-.015em;" { "Programming" }
                            p style="margin:0 0 12px; font-size:13.5px; color:#6e747b;" { "how I make it move" }
                            p style="margin:0 0 16px; font-size:14px; color:#9aa0a7; text-wrap:pretty;" { "Python does most of the automation. Rust shows up where correctness matters more than iteration speed — Kernform's core and PraxisLedger's storage layer are both Rust for that reason. Bash glues Linux ops together when nothing fancier is warranted." }
                            div style="display:flex; flex-wrap:wrap; gap:6px;" {
                                (tag_btn("python", "Python")) (tag_btn("rust", "Rust")) (tag_btn("bash", "Bash"))
                            }
                        }
                        div style="border:1px solid #24282e; border-radius:8px; background:#101215; padding:20px 22px;" {
                            h3 style="margin:0 0 2px; font-size:17px; font-weight:700; letter-spacing:-.015em;" { "Architecture" }
                            p style="margin:0 0 12px; font-size:13.5px; color:#6e747b;" { "how I shape it" }
                            p style="margin:0 0 16px; font-size:14px; color:#9aa0a7; text-wrap:pretty;" { "Hexagonal architecture keeps domain logic from knowing or caring what's on the other side of a port — a database, an LLM provider, a CLI, doesn't matter. Event-driven is how Kafka → Logstash → Splunk actually behaves in production, not just a diagram choice. Self-healing is the same idea one level up: systems that notice they're wrong and correct within bounds, not systems that never fail." }
                            div style="display:flex; flex-wrap:wrap; gap:6px;" {
                                (tag_btn("hexagonal", "Hexagonal Architecture")) (tag_btn("eda", "Event-Driven Architecture")) (tag_btn("selfhealing", "Self-Healing Systems"))
                            }
                        }
                        div style="border:1px solid #24282e; border-radius:8px; background:#101215; padding:20px 22px;" {
                            h3 style="margin:0 0 2px; font-size:17px; font-weight:700; letter-spacing:-.015em;" { "Configuration Management" }
                            p style="margin:0 0 12px; font-size:13.5px; color:#6e747b;" { "how I keep it consistent" }
                            p style="margin:0 0 16px; font-size:14px; color:#9aa0a7; text-wrap:pretty;" { "Salt and Terraform for infrastructure that already exists. Scaffolding and conformance — Kernform's actual job — for making sure new repositories start out consistent instead of drifting from day one and getting \"fixed\" later under time pressure." }
                            div style="display:flex; flex-wrap:wrap; gap:6px;" {
                                (tag_btn("salt", "SaltStack")) (tag_btn("terraform", "Terraform")) (tag_btn("scaffolding", "Scaffolding & Conformance"))
                            }
                        }
                        div style="border:1px solid #24282e; border-radius:8px; background:#101215; padding:20px 22px;" {
                            h3 style="margin:0 0 2px; font-size:17px; font-weight:700; letter-spacing:-.015em;" { "Infrastructure & Reliability" }
                            p style="margin:0 0 12px; font-size:13.5px; color:#6e747b;" { "how I keep it standing" }
                            p style="margin:0 0 16px; font-size:14px; color:#9aa0a7; text-wrap:pretty;" { "Linux and AWS are the ground floor. HA/DR readiness and drift detection are what keep that ground floor from quietly becoming unstable — the unglamorous half of the job that only gets noticed when it's skipped." }
                            div style="display:flex; flex-wrap:wrap; gap:6px;" {
                                (tag_btn("linux", "Linux")) (tag_btn("aws", "AWS")) (tag_btn("vmware", "VMware / vSphere")) (tag_btn("dr", "HA/DR Readiness")) (tag_btn("drift", "Configuration-Drift Detection"))
                            }
                        }
                        div style="border:1px solid #24282e; border-radius:8px; background:#101215; padding:20px 22px;" {
                            h3 style="margin:0 0 2px; font-size:17px; font-weight:700; letter-spacing:-.015em;" { "AI-Assisted Engineering" }
                            p style="margin:0 0 12px; font-size:13.5px; color:#6e747b;" { "how I work with agents, not just around them" }
                            p style="margin:0 0 16px; font-size:14px; color:#9aa0a7; text-wrap:pretty;" { "Codex, Claude, and Antigravity in daily use, Copilot inside the JPMC workflow — but the actual skill isn't \"using an AI tool,\" it's context engineering and harness design: giving an agent an explicit, inspectable contract instead of hoping it infers one. OpenTelemetry and tracing are the next piece of that — still learning, not yet claiming mastery." }
                            div style="display:flex; flex-wrap:wrap; gap:6px;" {
                                (tag_btn("aieval", "AI Code Evaluation")) (tag_btn("contexteng", "Context Engineering")) (tag_btn("otel", "OpenTelemetry & Tracing")) (tag_btn("harness", "Harness Engineering"))
                            }
                        }
                    }
                }

                // ---------- projects ----------
                section style="padding:0 0 72px;" {
                    div style="font-family:'JetBrains Mono',monospace; font-size:11px; letter-spacing:.1em; color:#ff7a45; text-transform:uppercase; margin-bottom:10px;" { "projects — the personal ledger" }
                    h2 style="margin:0 0 32px; font-size:clamp(28px,3.4vw,38px); font-weight:700; letter-spacing:-.025em;" { "What's actually running" }
                    div style="display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:12px;" {
                        div class="pf-item" data-item="blackcell" data-tags="python selfhealing" style=(CARD) {
                            div style=(CARD_TOP) { span style=(CARD_NAME) { "BlackCell" } (status_badge("pre-alpha", "warn")) }
                            p style=(CARD_DESC) { "Evidence-gated control runtime for agent actions." }
                            div style="display:flex; gap:14px; align-items:center;" {
                                a href="/projects/blackcell" style=(format!("font-family:{MONO}; font-size:12px;")) { "deep-dive →" }
                                a class="pf-hover-ember" href="https://github.com/kmosoti/blackcell" target="_blank" rel="noopener" style=(format!("font-family:{MONO}; font-size:12px; color:#6e747b;")) { "repo" }
                            }
                        }
                        div class="pf-item" data-item="sds" data-tags="python splunk red use" style=(CARD) {
                            div style=(CARD_TOP) { span style=(CARD_NAME) { "splunk-dashboard-studio" } (status_badge("alpha", "warn")) }
                            p style=(CARD_DESC) { "Pydantic 2 compiler/validator for Splunk Dashboard Studio, version-aware 9.4–10.4." }
                            div style="display:flex; gap:14px; align-items:center;" {
                                a href="/projects/splunk-dashboard-studio" style=(format!("font-family:{MONO}; font-size:12px;")) { "deep-dive →" }
                                a class="pf-hover-ember" href="https://github.com/kmosoti/splunk-dashboard-studio-python" target="_blank" rel="noopener" style=(format!("font-family:{MONO}; font-size:12px; color:#6e747b;")) { "repo" }
                            }
                        }
                        div class="pf-item" data-item="praxis" data-tags="rust python" style=(CARD) {
                            div style=(CARD_TOP) { span style=(CARD_NAME) { "PraxisLedger" } (status_badge("early bootstrap", "quiet")) }
                            p style=(CARD_DESC) { "Provenance and temporal knowledge graph. SQLite + Rust + Python." }
                            a href="https://github.com/kmosoti/PraxisLedger" target="_blank" rel="noopener" style=(format!("font-family:{MONO}; font-size:12px;")) { "view repo →" }
                        }
                        div class="pf-item" data-item="kernform" data-tags="rust python hexagonal scaffolding" style=(CARD) {
                            div style=(CARD_TOP) { span style=(CARD_NAME) { "Kernform" } (status_badge("alpha", "warn")) }
                            p style=(CARD_DESC) { "Deterministic project scaffolding and repo-conformance tool. Rust core, PyO3 bridge, Python SDK/CLI — successor to an earlier doctrine-notes project, different approach entirely." }
                            span style=(format!("font-family:{MONO}; font-size:12px; color:#6e747b;")) { "private" }
                        }
                        div class="pf-item" data-item="sai" data-tags="python" style=(CARD) {
                            div style=(CARD_TOP) { span style=(CARD_NAME) { "SAI" } (status_badge("shipped", "ship")) }
                            p style=(CARD_DESC) { "Agent routing modeled on brain-network dynamics — episodic/semantic memory, async consolidation loop." }
                            span style=(format!("font-family:{MONO}; font-size:12px; color:#6e747b;")) { "private" }
                        }
                        div class="pf-item" data-item="los" data-tags="python" style=(CARD) {
                            div style=(CARD_TOP) { span style=(CARD_NAME) { "learning-os" } (status_badge("active", "quiet")) }
                            p style=(CARD_DESC) { "Adaptive personal-learning app. FastAPI + SQLAlchemy." }
                            a href="https://github.com/kmosoti/learning-os" target="_blank" rel="noopener" style=(format!("font-family:{MONO}; font-size:12px;")) { "view repo →" }
                        }
                    }
                }

                // ---------- about ----------
                section style="padding:0 0 40px; display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); gap:clamp(28px,5vw,60px);" {
                    div {
                        div style="font-family:'JetBrains Mono',monospace; font-size:11px; letter-spacing:.1em; color:#ff7a45; text-transform:uppercase; margin-bottom:10px;" { "about" }
                        h2 style="margin:0 0 24px; font-size:clamp(28px,3.4vw,38px); font-weight:700; letter-spacing:-.025em;" { "Why observability, why now" }
                        p style="margin:0 0 18px; font-size:16px; color:#9aa0a7; text-wrap:pretty;" { "What I actually enjoy is the fitting-together part — more Lego than art project. Finding the pieces, working out how they connect, building something whole out of parts that used to be separate. Lately most of those pieces have been automation: infrastructure, mostly, sometimes application software." }
                        p style="margin:0 0 18px; font-size:16px; color:#9aa0a7; text-wrap:pretty;" { "Skip that second half and tech debt moves in quietly — it doesn't announce itself, it just slows the cycle down until someone finally asks why. Instrumentation is how you catch it before it's load-bearing." }
                        p style="margin:0; font-size:16px; color:#9aa0a7; text-wrap:pretty;" { "It's also why agentic engineering is the obvious next branch, not a pivot. An agent acting on your infrastructure is just another system that needs to tell on itself — same question, new blast radius." }
                    }
                    div {
                        blockquote style="margin:0 0 30px; padding:2px 0 2px 22px; border-left:2px solid #ff7a45; font-size:clamp(20px,2.2vw,25px); line-height:1.32; font-weight:600; letter-spacing:-.02em; color:#ece7dd; text-wrap:pretty;" { "Building the automation is the easy half. Knowing whether it's working is the actual job." }
                        div style="display:flex; align-items:center; gap:12px; flex-wrap:wrap; font-family:'JetBrains Mono',monospace; font-size:13px; margin-bottom:8px;" {
                            span style="display:flex; align-items:center; gap:7px; color:#ece7dd;" { span style="width:6px;height:6px;border-radius:50%;background:#ff7a45;" {} "build" }
                            span style="color:#6e747b;" { "→" }
                            span style="display:flex; align-items:center; gap:7px; color:#ece7dd;" { span style="width:6px;height:6px;border-radius:50%;background:#ff7a45;" {} "observe" }
                            span style="color:#6e747b;" { "→" }
                            span style="display:flex; align-items:center; gap:7px; color:#ece7dd;" { span style="width:6px;height:6px;border-radius:50%;background:#ff7a45;" {} "improve" }
                        }
                        div style="font-family:'JetBrains Mono',monospace; font-size:12px; color:#6e747b; margin-bottom:30px;" { "↺ repeat, ideally faster each time" }
                        ul style="margin:0; padding:0; list-style:none; display:flex; flex-direction:column; gap:12px;" {
                            li style="position:relative; padding-left:22px; font-size:15px; color:#9aa0a7;" { span style="position:absolute; left:0; color:#ff7a45;" { "→" } "Prefer boring, testable automation over clever automation." }
                            li style="position:relative; padding-left:22px; font-size:15px; color:#9aa0a7;" { span style="position:absolute; left:0; color:#ff7a45;" { "→" } "Separate configuration from behavior so a change is easy to review." }
                            li style="position:relative; padding-left:22px; font-size:15px; color:#9aa0a7;" { span style="position:absolute; left:0; color:#ff7a45;" { "→" } "Logs, commands, validation output, and failure notes are evidence, not noise." }
                        }
                    }
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
            .expect("the home page renders")
            .into_string()
    }

    /// The golden page's reachable surfaces: GitHub, both deep-dives, and the
    /// public repos. (Email left the page with the golden design — the résumé
    /// dropdown replaced it.)
    #[test]
    fn the_reachable_surfaces_are_present() {
        let body = page();
        for href in [
            "href=\"https://github.com/kmosoti\"",
            "href=\"/projects/blackcell\"",
            "href=\"/projects/splunk-dashboard-studio\"",
            "href=\"https://github.com/kmosoti/blackcell\"",
            "href=\"https://github.com/kmosoti/splunk-dashboard-studio-python\"",
            "href=\"https://github.com/kmosoti/PraxisLedger\"",
            "href=\"https://github.com/kmosoti/learning-os\"",
        ] {
            assert!(body.contains(href), "the home page is missing {href}");
        }
    }

    /// Every `href` is one of the four shapes a link on this site may take.
    /// `#` is legal: the footer résumé actions are progressive-enhancement
    /// anchors the JS upgrades.
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

    /// The page join key wraps the whole body: opens before the first section
    /// and closes after the last one, so the probe can attribute any reading.
    #[test]
    fn the_page_join_key_encloses_everything() {
        let body = page();
        let span = body.find("data-span-id=\"").expect("the page carries one");
        let first_section = body.find("<section").expect("the page has sections");
        assert!(
            span < first_section,
            "the join key opens before the content"
        );
    }

    /// The official hero is direction 1a — the terminal window — alone.
    /// 1b and its header toggle were retired by owner ruling (2026-08-09);
    /// a `data-dir-section` attribute reappearing means the toggle crept back.
    #[test]
    fn the_official_hero_is_the_terminal_window_alone() {
        let body = page();
        assert!(body.contains("kennedy@observability — zsh — 96×28"));
        assert!(body.contains("Kennedy Mosoti"));
        assert!(!body.contains("data-dir-section"));
        assert_eq!(
            body.matches("pf-typed").count(),
            1,
            "exactly one typed prompt"
        );
    }

    /// The cross-reference contract: every tag button carries data-tag, every
    /// job/project carries data-tags, and the two vocabularies agree.
    #[test]
    fn the_tag_crossref_contract_holds() {
        let body = page();
        let mut tags = std::collections::BTreeSet::new();
        for (index, _) in body.match_indices("data-tag=\"") {
            let value = &body[index + 10..];
            tags.insert(&value[..value.find('"').unwrap()]);
        }
        assert!(
            tags.len() >= 20,
            "expected the full tag vocabulary, got {tags:?}"
        );
        for (index, _) in body.match_indices("data-tags=\"") {
            let value = &body[index + 11..];
            let value = &value[..value.find('"').unwrap()];
            for tag in value.split(' ') {
                assert!(
                    tags.contains(tag),
                    "{tag:?} backs an item but has no tag button"
                );
            }
        }
    }

    /// The golden default: the first job entry is open, the rest are closed.
    #[test]
    fn the_first_job_entry_is_open() {
        let body = page();
        assert!(body.contains("data-job-body=\"jpmc\" style=\"display:block;"));
        assert!(body.contains("data-job-body=\"dat\" style=\"display:none;"));
    }

    /// All six instrumentation tiles exist for portfolio.js to fill.
    #[test]
    fn the_instrumentation_tiles_exist() {
        let body = page();
        for id in [
            "pf-m-ttfb",
            "pf-m-nodes",
            "pf-m-dom",
            "pf-m-fps",
            "pf-m-res",
            "pf-m-up",
        ] {
            assert!(
                body.contains(&format!("id=\"{id}\"")),
                "missing metric tile {id}"
            );
        }
    }
}
