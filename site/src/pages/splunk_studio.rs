//! `/projects/splunk-dashboard-studio` — the compiler deep-dive, ported
//! verbatim from the golden file: header, framework-pack chips, the
//! fault-injection controls, model.py beside the live payload, the
//! generate→emit pipeline, and the differential-validation caption.

use super::deepdive::{MONO, flow_arrow, flow_node, log_panel, opener, run_btn, seg_btn, token};
use crate::layout::{PageMeta, portfolio_shell};
use crate::state::AppState;
use maud::{Markup, PreEscaped, html};

fn pack_chip(text: &str) -> Markup {
    html! {
        span style="font-family:'JetBrains Mono',monospace; font-size:11.5px; color:#9aa0a7; border:1px solid #24282e; border-radius:4px; padding:4px 9px;" { (text) }
    }
}

fn fault_checkbox(key: &str, label: &str) -> Markup {
    html! {
        label style="display:flex; align-items:center; gap:8px; font-size:13.5px; color:#9aa0a7; cursor:pointer;" {
            input type="checkbox" data-sp-t=(key) style="accent-color:#ff7a45;";
            (label)
        }
    }
}

pub fn render(state: &AppState) -> Result<Markup, crate::error::RouteError> {
    Ok(portfolio_shell(
        state,
        PageMeta {
            title: "splunk-dashboard-studio",
            description: "splunk-dashboard-studio — Pydantic 2 compiler for Splunk Dashboard Studio: typed Python in, version-targeted JSON out.",
            route: "/projects/splunk-dashboard-studio",
        },
        html! {
            div style="max-width:1000px; margin:0 auto; padding:60px clamp(20px,4vw,44px) 120px;" {
                (opener("splunk-dashboard-studio"))
                div style="display:flex; align-items:center; gap:14px; flex-wrap:wrap;" {
                    h1 style="margin:0; font-size:clamp(30px,4.4vw,48px); font-weight:800; letter-spacing:-.035em;" { "splunk-dashboard-studio" }
                    span style=(format!("font-family:{MONO}; font-size:11px; letter-spacing:.06em; text-transform:uppercase; padding:4px 9px; border:1px solid #f2b134; color:#f2b134; border-radius:4px;")) { "alpha" }
                }
                p style="margin:20px 0 22px; max-width:720px; font-size:21px; line-height:1.4; color:#ece7dd; text-wrap:pretty;" { "Pydantic 2 compiler for Splunk Dashboard Studio — typed Python in, version-targeted JSON out." }
                p style="margin:0 0 20px; max-width:720px; font-size:16px; color:#9aa0a7; text-wrap:pretty;" { "Dashboard Studio's JSON is easy to hand-author and easy to get subtly wrong — a typo'd key doesn't fail loudly, it just fails to render, or renders wrong, days later. This compiles a typed model down to the JSON Splunk actually expects, checked twice: once against the typed schema, once against Splunk's own official validator, so drift between the two gets caught instead of shipped." }

                div style="display:flex; flex-wrap:wrap; gap:6px; margin-bottom:36px;" {
                    (pack_chip("Golden Four")) (pack_chip("RED")) (pack_chip("USE")) (pack_chip("MELT")) (pack_chip("SLO"))
                    (pack_chip("Kubernetes")) (pack_chip("CI/CD")) (pack_chip("Security")) (pack_chip("Business"))
                }

                h2 style="margin:0 0 12px; font-size:22px; font-weight:700; letter-spacing:-.02em;" { "What the compiler catches" }
                p style="margin:0 0 22px; max-width:700px; font-size:16px; color:#9aa0a7; text-wrap:pretty;" {
                    "Toggle a mistake below, pick a target version, and compile. Nothing reaches "
                    span style=(format!("font-family:{MONO}; color:#ff7a45;")) { "emit" }
                    " until both validation stages agree it's clean."
                }

                div style="display:flex; flex-wrap:wrap; gap:28px; align-items:flex-start; padding:18px 20px; border:1px solid #24282e; border-radius:8px; background:#101215; margin-bottom:20px;" {
                    div {
                        div style=(format!("font-family:{MONO}; font-size:10.5px; letter-spacing:.08em; color:#6e747b; text-transform:uppercase; margin-bottom:8px;")) { "target version" }
                        div style="display:inline-flex; border:1px solid #24282e; border-radius:5px; overflow:hidden;" {
                            (seg_btn("sp", "9.4.x", "9.4.x", false))
                            (seg_btn("sp", "10.0.x", "10.0.x", false))
                            (seg_btn("sp", "10.2.x", "10.2.x", true))
                            (seg_btn("sp", "10.4.x", "10.4.x", false))
                        }
                    }
                    div {
                        div style=(format!("font-family:{MONO}; font-size:10.5px; letter-spacing:.08em; color:#6e747b; text-transform:uppercase; margin-bottom:8px;")) { "introduce a problem" }
                        div style="display:flex; flex-direction:column; gap:7px;" {
                            (fault_checkbox("unknown", "unknown field in payload"))
                            (fault_checkbox("missing", "missing required field"))
                            (fault_checkbox("type", "wrong type for a value"))
                            (fault_checkbox("diff", "passes our schema, not Splunk's own"))
                        }
                    }
                    (run_btn("pf-sp-run", "compile", " align-self:flex-end;"))
                }

                div style="display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:14px; margin-bottom:20px;" {
                    div {
                        div style=(format!("font-family:{MONO}; font-size:10.5px; letter-spacing:.08em; color:#6e747b; text-transform:uppercase; margin-bottom:8px;")) { "model.py" }
                        pre style=(format!("margin:0; padding:14px 16px; border:1px solid #24282e; border-radius:8px; background:#101215; font-family:{MONO}; font-size:12.5px; line-height:1.7; color:#9aa0a7; overflow-x:auto;")) {
                            "class " span style="color:#ff7a45;" { "RevenuePanel" } "(DashboardPanel):\n"
                            "    title: str = " span style="color:#ff7a45;" { (PreEscaped("&quot;Revenue&quot;")) } "\n"
                            "    data_source: str\n"
                            "    visualization: Literal[" span style="color:#ff7a45;" { (PreEscaped("&quot;line&quot;")) } ", " span style="color:#ff7a45;" { (PreEscaped("&quot;bar&quot;")) } "]\n"
                            "    threshold: float | None = None"
                        }
                    }
                    div {
                        div style=(format!("font-family:{MONO}; font-size:10.5px; letter-spacing:.08em; color:#6e747b; text-transform:uppercase; margin-bottom:8px;")) { "compiling: revenue-panel.json" }
                        div id="pf-sp-payload" style=(format!("padding:14px 16px; border:1px solid #24282e; border-radius:8px; background:#101215; font-family:{MONO}; font-size:12.5px; line-height:1.7; color:#9aa0a7; overflow-x:auto;")) {
                            // Server-rendered clean payload; portfolio.js re-renders on fault toggles.
                            div style="white-space:pre; color:#9aa0a7;" { "{" }
                            div style="white-space:pre; color:#9aa0a7;" { "  \"title\": \"Revenue\"," }
                            div style="white-space:pre; color:#9aa0a7;" { "  \"data_source\": \"index=sales\"," }
                            div style="white-space:pre; color:#9aa0a7;" { "  \"visualization\": \"line\"," }
                            div style="white-space:pre; color:#9aa0a7;" { "  \"threshold\": 500000" }
                            div style="white-space:pre; color:#9aa0a7;" { "}" }
                        }
                    }
                }

                div id="pf-sp-wrap" style="position:relative; border:1px solid #24282e; border-radius:8px; background:#0c0e11; padding:26px 20px 22px; margin-bottom:16px;" {
                    div style="display:flex; align-items:stretch; gap:8px; flex-wrap:wrap;" {
                        (flow_node("sp", "generate", None, "generate", "from model.py", None, "#6e747b"))
                        (flow_arrow())
                        (flow_node("sp", "valPy", None, "validate", "Pydantic", None, "#6e747b"))
                        (flow_arrow())
                        (flow_node("sp", "valNpm", None, "validate", "Splunk NPM", None, "#6e747b"))
                        (flow_arrow())
                        (flow_node("sp", "optimize", None, "optimize", "normalize output", None, "#6e747b"))
                        (flow_arrow())
                        (flow_node("sp", "emit", None, "emit", "—", Some("pf-sp-emitsub"), "#6e747b"))
                    }
                    (token("pf-sp-token"))
                }

                p id="pf-sp-err" style="margin:0 0 16px; font-size:14px; color:#ef4759; display:none;" {}

                (log_panel("pf-sp-log", "160px"))

                p style="margin:0; max-width:720px; font-size:15px; color:#6e747b; text-wrap:pretty;" { "The differential stage exists because a shape can be perfectly valid Python and still be a shape Splunk's own schema doesn't recognize yet — that's the whole reason a second, independent validator sits in the pipeline instead of trusting the typed model alone." }
            }
        },
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_pipeline_and_controls_render() {
        let body = render(&AppState::for_tests(false)).unwrap().into_string();
        for key in ["generate", "valPy", "valNpm", "optimize", "emit"] {
            assert!(
                body.contains(&format!("data-sp-node=\"{key}\"")),
                "the compiler pipeline is missing node {key}"
            );
        }
        for fault in ["unknown", "missing", "type", "diff"] {
            assert!(
                body.contains(&format!("data-sp-t=\"{fault}\"")),
                "missing fault toggle {fault}"
            );
        }
        assert!(body.contains("data-sp-version=\"10.2.x\""));
        assert!(body.contains("id=\"pf-sp-payload\""));
        assert!(body.contains("id=\"pf-sp-err\""));
    }
}
