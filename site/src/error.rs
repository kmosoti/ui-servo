//! Fallible edges of the site: startup configuration and unknown fragments.

use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use std::path::PathBuf;

/// Everything that can go wrong before the server is listening.
///
/// Startup is the only place this binary is allowed to be picky: a missing
/// token sheet or an unparseable motion table means the page would render
/// off-contract, and rendering off-contract silently is exactly the failure
/// mode the whole loop exists to prevent.
#[derive(Debug, thiserror::Error)]
pub enum StartupError {
    #[error("asset directory {0} does not exist (set UI_SERVO_ASSETS to override)")]
    MissingAssets(PathBuf),

    #[error("cannot read {path}: {source}")]
    Unreadable {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },

    #[error("{path} is not valid JSON: {source}")]
    MalformedJson {
        path: PathBuf,
        #[source]
        source: serde_json::Error,
    },

    #[error("{path} is missing the required key {key:?}; regenerate it (see site/README.md)")]
    MotionTableIncomplete { path: PathBuf, key: &'static str },

    #[error(
        "{path} declares no --color-* token; it is empty or truncated. \
         Re-emit it: uv run python -m ui_servo.domain.contract --emit-css site/assets/tokens.css"
    )]
    TokensNotEmitted { path: PathBuf },

    /// A promoted fragment on disk cannot prove it was gated.
    ///
    /// Checked at boot rather than on first request, because "the server came
    /// up" is the signal a deploy watches. A tampered hero that only fails when
    /// somebody happens to load the home page is a broken site reported as a
    /// healthy one. Dev mode skips this: a pick under active edit is expected to
    /// be mid-flight, and there the per-request 500 is the right feedback.
    #[error(
        "promoted fragment refused at startup: {0}. Re-promote it through the gauntlet \
         (uv run python -m ui_servo.cli.promote) rather than editing the file."
    )]
    UngatedPromotion(String),

    /// The promotion directory resolved to somewhere inside the static root.
    ///
    /// The separation is the entire defence: promoted markup is unservable
    /// without a provenance check *because* no `ServeDir` can reach it. Route
    /// denials were tried first and lost to percent-encoding. Configuration can
    /// silently undo the replacement, so it is checked rather than assumed.
    #[error(
        "the promotion directory {promoted} is inside the asset root {assets}, so the static \
         file server could hand out promoted markup without its provenance ever being checked. \
         Point UI_SERVO_ASSETS or UI_SERVO_PROMOTED_ROOT somewhere they do not overlap."
    )]
    PromotedInsideAssets { promoted: PathBuf, assets: PathBuf },

    #[error("UI_SERVO_PORT={0:?} is not a port number")]
    BadPort(String),

    #[error("cannot bind {addr}: {source}")]
    Bind {
        addr: String,
        #[source]
        source: std::io::Error,
    },

    #[error("server stopped with an error: {0}")]
    Serve(#[source] std::io::Error),
}

/// Everything that can go wrong while serving one request.
#[derive(Debug, thiserror::Error)]
pub enum RouteError {
    #[error("no fragment named {0:?}")]
    UnknownFragment(String),

    #[error("probe.js is not available; run the probe unit (U3) or set UI_SERVO_PROBE")]
    ProbeUnavailable,

    /// A promoted fragment exists but could not prove it was gated. Refusing is
    /// the whole point of the provenance check, so this is a 500 and not a
    /// quiet fallback to the placeholder.
    #[error("promoted fragment refused: {0}")]
    UngatedPromotion(String),
}

impl IntoResponse for RouteError {
    fn into_response(self) -> Response {
        let status = match self {
            Self::UnknownFragment(_) | Self::ProbeUnavailable => StatusCode::NOT_FOUND,
            Self::UngatedPromotion(_) => StatusCode::INTERNAL_SERVER_ERROR,
        };
        tracing::debug!(error = %self, "request rejected");
        (status, self.to_string()).into_response()
    }
}
