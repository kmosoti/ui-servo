//! Shared markup for the two project deep-dive pages — the flow-simulator
//! pieces both use, ported verbatim from the golden file. The nodes carry
//! `data-<sim>-node` attributes; `assets/portfolio.js` runs the token, the
//! log, and the pass/fail styling.

use maud::{Markup, html};

pub const MONO: &str = "'JetBrains Mono',monospace";

/// The back-to-profile link and eyebrow every deep-dive opens with.
pub fn opener(slug: &str) -> Markup {
    html! {
        a class="pf-hover-ember" href="/" style="font-family:'JetBrains Mono',monospace; font-size:12px; color:#6e747b;" { "← profile" }
        div style="font-family:'JetBrains Mono',monospace; font-size:11px; letter-spacing:.1em; color:#ff7a45; text-transform:uppercase; margin:28px 0 12px;" { "project deep-dive / " (slug) }
    }
}

/// A flow node. `sim` is the simulator prefix (`bc`/`sp`); `glyph` is the
/// optional icon line; `sub_id` hangs an id on the sub-line for JS updates,
/// and `sub_color` overrides its colour (the BlackCell verify node is amber).
pub fn flow_node(
    sim: &str,
    key: &str,
    glyph: Option<&str>,
    title: &str,
    sub: &str,
    sub_id: Option<&str>,
    sub_color: &str,
) -> Markup {
    let node_style = "flex:1; min-width:110px; display:flex; flex-direction:column; align-items:center; gap:3px; padding:14px 8px; border-radius:7px; background:#141619; border-style:solid; border-width:1px; border-color:#24282e; box-shadow:none; opacity:1; transition:border-color .3s, box-shadow .3s, opacity .3s;";
    let sub_style = format!("font-family:{MONO}; font-size:11px; color:{sub_color};");
    html! {
        div data-node=(format!("{sim}:{key}")) data-bc-node=[(sim == "bc").then_some(key)] data-sp-node=[(sim == "sp").then_some(key)] style=(node_style) {
            @if let Some(glyph) = glyph {
                span style="font-size:18px; color:#ff7a45;" { (glyph) }
            }
            span style="font-size:13.5px; font-weight:700;" { (title) }
            @if let Some(id) = sub_id {
                span id=(id) style=(sub_style) { (sub) }
            } @else {
                span style=(sub_style) { (sub) }
            }
        }
    }
}

/// The arrow between flow nodes.
pub fn flow_arrow() -> Markup {
    html! { div style="display:flex; align-items:center; color:#3a4047;" { "→" } }
}

/// The travelling token, absolutely positioned inside a flow wrap.
pub fn token(id: &str) -> Markup {
    html! {
        div id=(id) data-token="1" style="position:absolute; top:0; left:0; width:10px; height:10px; border-radius:50%; background:#ff7a45; box-shadow:0 0 10px 3px rgba(255,122,69,.5); pointer-events:none; z-index:5; transition:transform .4s cubic-bezier(.4,0,.2,1); transform:translate(0px,0px); opacity:0;" {}
    }
}

/// The scrolling log panel.
pub fn log_panel(id: &str, height: &str) -> Markup {
    let style = format!(
        "height:{height}; overflow-y:auto; padding:12px 14px; border:1px solid #24282e; border-radius:8px; background:#101215; font-family:{MONO}; font-size:12.5px; margin-bottom:24px;"
    );
    html! { div id=(id) style=(style) {} }
}

/// A segmented-control button. `on` renders the golden active state.
pub fn seg_btn(attr: &str, val: &str, label: &str, on: bool) -> Markup {
    let style = format!(
        "padding:7px 13px; border:0; cursor:pointer; font-family:{MONO}; font-size:12px; color:{}; background:{};",
        if on { "#140904" } else { "#9aa0a7" },
        if on { "#ff7a45" } else { "transparent" }
    );
    html! {
        @if attr == "bc" {
            button data-bc-scenario=(val) style=(style) { (label) }
        } @else {
            button data-sp-version=(val) style=(style) { (label) }
        }
    }
}

/// The ember run button.
pub fn run_btn(id: &str, label: &str, extra: &str) -> Markup {
    let style = format!(
        "padding:9px 20px; border:1px solid #ff7a45; border-radius:5px; cursor:pointer; background:#ff7a45; color:#140904; font-family:{MONO}; font-size:12.5px; font-weight:700; opacity:1;{extra}"
    );
    html! { button id=(id) style=(style) { (label) } }
}
