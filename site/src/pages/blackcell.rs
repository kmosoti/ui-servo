//! `/projects/blackcell` — the BlackCell deep-dive, ported verbatim from the
//! golden file: header, prose, the scenario controls, the observe→verify flow
//! with its two outcomes, the log panel, and the honest closing caption.

use super::deepdive::{MONO, flow_arrow, flow_node, log_panel, opener, run_btn, seg_btn, token};
use crate::layout::{PageMeta, portfolio_shell};
use crate::state::AppState;
use maud::{Markup, html};

pub fn render(state: &AppState) -> Result<Markup, crate::error::RouteError> {
    Ok(portfolio_shell(
        state,
        PageMeta {
            title: "BlackCell",
            description: "BlackCell — local-first, evidence-gated control runtime for coding agents.",
            route: "/projects/blackcell",
        },
        html! {
            div style="max-width:1000px; margin:0 auto; padding:60px clamp(20px,4vw,44px) 120px;" {
                (opener("blackcell"))
                // The hero wraps in a relative div so the black-hole canvas
                // (owner, 2026-08-09) can sit in the empty space right of the
                // title and lede. Text stays in normal flow and paints above
                // it; portfolio.js runs the accretion-disk scene.
                div style="position:relative;" {
                    canvas id="pf-bh" class="pf-bh" data-print-hide="1" {}
                    div style="display:flex; align-items:center; gap:14px; flex-wrap:wrap;" {
                        h1 style="margin:0; font-size:clamp(36px,5vw,54px); font-weight:800; letter-spacing:-.035em;" { "BlackCell" }
                        span style=(format!("font-family:{MONO}; font-size:11px; letter-spacing:.06em; text-transform:uppercase; padding:4px 9px; border:1px solid #f2b134; color:#f2b134; border-radius:4px;")) { "pre-alpha" }
                    }
                    p style="margin:20px 0 22px; max-width:700px; font-size:21px; line-height:1.4; color:#ece7dd; text-wrap:pretty;" { "Local-first, evidence-gated control runtime for coding agents." }
                    p style="margin:0 0 16px; max-width:700px; font-size:16px; color:#9aa0a7; text-wrap:pretty;" { "BlackCell doesn't take an agent's word that something worked — it makes the agent prove it, then writes the proof down. Every task moves through the same loop: observe the repository, construct a plan bounded to what's actually needed, execute through a provider adapter inside an isolated worktree, review the result independently, and verify it deterministically before anything gets recorded." }
                }

                h2 style="margin:44px 0 12px; font-size:22px; font-weight:700; letter-spacing:-.02em;" { "What happens when verification fails" }
                p style="margin:0 0 26px; max-width:700px; font-size:16px; color:#9aa0a7; text-wrap:pretty;" { "Failure doesn't mean the loop panics, or quietly keeps trying forever. Retries are bounded — two attempts here, for illustration — and running out doesn't trigger a third attempt on its own. It hands control back to the host. No unbounded autonomous writes, no agent swarm working around a wall it just hit." }

                div style="display:flex; flex-wrap:wrap; gap:24px; align-items:flex-end; padding:18px 20px; border:1px solid #24282e; border-radius:8px; background:#101215; margin-bottom:20px;" {
                    div {
                        div style=(format!("font-family:{MONO}; font-size:10.5px; letter-spacing:.08em; color:#6e747b; text-transform:uppercase; margin-bottom:8px;")) { "scenario" }
                        div style="display:inline-flex; border:1px solid #24282e; border-radius:5px; overflow:hidden;" {
                            (seg_btn("bc", "clean", "clean pass", true))
                            (seg_btn("bc", "retry", "fail once, retry", false))
                            (seg_btn("bc", "exhausted", "attempts exhausted", false))
                        }
                    }
                    (run_btn("pf-bc-run", "run task", ""))
                }

                div id="pf-bc-wrap" style="position:relative; border:1px solid #24282e; border-radius:8px; background:#0c0e11; padding:26px 20px 22px; margin-bottom:16px;" {
                    div style="display:flex; align-items:stretch; gap:8px; flex-wrap:wrap;" {
                        (flow_node("bc", "observe", Some("◎"), "observe", "repo state", None, "#6e747b"))
                        (flow_arrow())
                        (flow_node("bc", "plan", Some("▤"), "plan", "bounded", None, "#6e747b"))
                        (flow_arrow())
                        (flow_node("bc", "execute", Some("▶"), "execute", "isolated worktree", None, "#6e747b"))
                        (flow_arrow())
                        (flow_node("bc", "review", Some("⟲"), "review", "independent", None, "#6e747b"))
                        (flow_arrow())
                        (flow_node("bc", "verify", Some("✓"), "verify", "—", Some("pf-bc-verifysub"), "#f2b134"))
                    }
                    div style="display:flex; gap:14px; margin-top:18px;" {
                        div data-bc-node="record" style="flex:1; padding:14px 16px; text-align:center; border-radius:7px; background:#141619; border-style:solid; border-width:1px; border-color:#24282e; box-shadow:none; opacity:.4; transition:opacity .3s, border-color .3s;" {
                            div style="font-size:14.5px; font-weight:700;" { "recorded" }
                            div style=(format!("font-family:{MONO}; font-size:11.5px; color:#6e747b; margin-top:3px;")) { "append-only evidence, SQLite" }
                        }
                        div data-bc-node="host" style="flex:1; padding:14px 16px; text-align:center; border-radius:7px; background:#141619; border-style:solid; border-width:1px; border-color:#24282e; box-shadow:none; opacity:.4; transition:opacity .3s, border-color .3s;" {
                            div style="font-size:14.5px; font-weight:700;" { "returned to host" }
                            div style=(format!("font-family:{MONO}; font-size:11.5px; color:#6e747b; margin-top:3px;")) { "bounded attempts exhausted" }
                        }
                    }
                    (token("pf-bc-token"))
                }

                (log_panel("pf-bc-log", "170px"))

                p style="margin:0; max-width:720px; font-size:15px; color:#6e747b; text-wrap:pretty;" { "Six real design decisions, simplified into one runnable loop — the actual system also handles provider-adapter negotiation, DAG execution across multiple bounded plans, and prior-experience retrieval feeding into future plans. This shows the shape, not the whole machine." }
            }
        },
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_flow_and_outcomes_render() {
        let body = render(&AppState::for_tests(false)).unwrap().into_string();
        for key in [
            "observe", "plan", "execute", "review", "verify", "record", "host",
        ] {
            assert!(
                body.contains(&format!("data-bc-node=\"{key}\"")),
                "the BlackCell flow is missing node {key}"
            );
        }
        assert!(body.contains("id=\"pf-bc-run\""));
        assert!(body.contains("id=\"pf-bc-wrap\""));
        assert!(body.contains("id=\"pf-bc-log\""));
        assert!(body.contains("data-bc-scenario=\"exhausted\""));
        assert!(
            body.contains("id=\"pf-bh\""),
            "the hero black hole canvas is gone"
        );
    }
}
