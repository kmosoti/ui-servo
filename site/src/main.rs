//! ui-servo-site — the product skeleton the control loop steers.
//!
//! Four server-rendered pages, a fragment endpoint htmx swaps against, and a
//! static asset directory holding the generated token sheet, the generated
//! motion table and a vendored htmx. In dev (`UI_SERVO_DEV=1`) every page also
//! carries the browser probe and the config it reads.
//!
//! Run it:
//!
//! ```text
//! UI_SERVO_DEV=1 cargo run          # http://localhost:8080
//! UI_SERVO_PORT=9000 cargo run      # somewhere else
//! ```

mod error;
mod fragments;
mod layout;
mod pages;
mod span;
mod state;

use axum::Router;
use axum::extract::{Path, State};
use axum::http::{HeaderValue, StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use error::{RouteError, StartupError};
use maud::Markup;
use state::AppState;
use std::process::ExitCode;
use tower_http::services::ServeDir;
use tower_http::trace::TraceLayer;

/// Default listen port. `UI_SERVO_PORT` overrides.
const DEFAULT_PORT: u16 = 8080;

#[tokio::main]
async fn main() -> ExitCode {
    match run().await {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            // Not `Result` from `main`: that prints the Debug form, which
            // swallows the remediation text these errors exist to carry
            // ("re-emit tokens.css with …"). Print Display, plus the source
            // chain, because a startup failure here is someone's next command.
            eprintln!("error: {error}");
            let mut source = std::error::Error::source(&error);
            while let Some(cause) = source {
                eprintln!("  caused by: {cause}");
                source = cause.source();
            }
            ExitCode::FAILURE
        }
    }
}

async fn run() -> Result<(), StartupError> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info,tower_http=info".into()),
        )
        .init();

    let state = AppState::from_env()?;
    let port = port_from_env()?;

    tracing::info!(
        assets = %state.assets_dir().display(),
        dev = state.dev(),
        turn_id = state.turn_id(),
        probe = ?state.probe_path().map(std::path::Path::display),
        fragments = ?fragments::NAMES,
        "starting ui-servo-site"
    );
    if state.dev() && state.probe_path().is_none() {
        tracing::warn!("UI_SERVO_DEV=1 but no probe.js found; pages will request it and 404");
    }

    let addr = format!("127.0.0.1:{port}");
    let listener = tokio::net::TcpListener::bind(&addr)
        .await
        .map_err(|source| StartupError::Bind {
            addr: addr.clone(),
            source,
        })?;

    tracing::info!("listening on http://{addr}");
    axum::serve(listener, app(state))
        .with_graceful_shutdown(shutdown_signal())
        .await
        .map_err(StartupError::Serve)?;

    tracing::info!("shut down cleanly");
    Ok(())
}

/// The whole routing table, in one readable place.
fn app(state: AppState) -> Router {
    let assets = Router::new()
        // Explicit before the directory: in dev, probe.js is read live from the
        // sibling `probe/` unit so editing the sensor does not need a rebuild.
        .route("/probe.js", get(probe_js))
        // Promoted fragments live under assets/ because that is where the site's
        // own files live, but they must never be reachable as static files: the
        // whole point of `promoted::load` is that a fragment proves it was gated
        // before it renders, and a raw ServeDir hit would skip the provenance
        // check, the hash check and `frame()` in one request. Refused here, at
        // the only place that could have leaked them.
        .route("/fragments", get(promotion_is_not_static))
        .route("/fragments/{*rest}", get(promotion_is_not_static))
        .fallback_service(ServeDir::new(state.assets_dir()));

    Router::new()
        .route("/", get(home))
        .route("/projects", get(projects))
        .route("/writing", get(writing))
        .route("/about", get(about))
        .route("/fragments/{name}", get(fragment))
        .route("/fragments/promoted/{part}", get(promoted_fragment))
        .nest("/assets", assets)
        .layer(TraceLayer::new_for_http())
        .with_state(state)
}

async fn home(State(state): State<AppState>) -> Result<Markup, RouteError> {
    pages::home::render(&state)
}

async fn projects(State(state): State<AppState>) -> Markup {
    pages::projects::render(&state)
}

async fn writing(State(state): State<AppState>) -> Markup {
    pages::writing::render(&state)
}

async fn about(State(state): State<AppState>) -> Markup {
    pages::about::render(&state)
}

/// A bare fragment, for htmx to swap. No shell, no doctype — returning a whole
/// document here would nest `<html>` inside the live page and quietly corrupt
/// every measurement taken afterwards.
async fn fragment(Path(name): Path<String>) -> Result<Markup, RouteError> {
    fragments::render(&name).ok_or(RouteError::UnknownFragment(name))
}

/// A human pick, served only if it can prove it was gated. An unpromoted part is
/// a 404; an ungated or edited one is a 500, because serving it would be the one
/// way an unreviewed fragment reaches a visitor.
async fn promoted_fragment(
    State(state): State<AppState>,
    Path(part): Path<String>,
) -> Result<Markup, RouteError> {
    match fragments::promoted::render(state.assets_dir(), &part) {
        Ok(markup) => Ok(markup),
        Err(fragments::promoted::PromotionError::NotPromoted(part)) => {
            Err(RouteError::UnknownFragment(part))
        }
        Err(error) => {
            tracing::error!(%error, "refusing to serve a promoted fragment");
            Err(RouteError::UngatedPromotion(error.to_string()))
        }
    }
}

/// Promoted fragments are served by `/fragments/promoted/{part}`, which verifies
/// provenance. The static path exists only to be closed.
async fn promotion_is_not_static() -> RouteError {
    RouteError::UngatedPromotion(
        "promoted fragments are served by /fragments/promoted/{part}, which verifies \
         their provenance; they are deliberately not reachable as static files"
            .to_owned(),
    )
}

/// Serve `probe.js` from wherever it was resolved at startup.
async fn probe_js(State(state): State<AppState>) -> Result<Response, RouteError> {
    let path = state.probe_path().ok_or(RouteError::ProbeUnavailable)?;
    let source = std::fs::read(path).map_err(|error| {
        tracing::error!(path = %path.display(), %error, "probe.js vanished after startup");
        RouteError::ProbeUnavailable
    })?;
    Ok((
        StatusCode::OK,
        [
            (
                header::CONTENT_TYPE,
                HeaderValue::from_static("text/javascript; charset=utf-8"),
            ),
            (header::CACHE_CONTROL, HeaderValue::from_static("no-store")),
        ],
        source,
    )
        .into_response())
}

fn port_from_env() -> Result<u16, StartupError> {
    match std::env::var("UI_SERVO_PORT") {
        Err(_) => Ok(DEFAULT_PORT),
        Ok(raw) => raw.parse().map_err(|_| StartupError::BadPort(raw)),
    }
}

/// Ctrl-C or SIGTERM. Containers send the latter; `cargo run` sends the former.
async fn shutdown_signal() {
    let interrupt = async {
        if let Err(error) = tokio::signal::ctrl_c().await {
            tracing::error!(%error, "cannot listen for ctrl-c");
            std::future::pending::<()>().await;
        }
    };

    #[cfg(unix)]
    let terminate = async {
        match tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()) {
            Ok(mut stream) => {
                stream.recv().await;
            }
            Err(error) => {
                tracing::error!(%error, "cannot listen for SIGTERM");
                std::future::pending::<()>().await;
            }
        }
    };

    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();

    tokio::select! {
        () = interrupt => tracing::info!("interrupt received, draining"),
        () = terminate => tracing::info!("SIGTERM received, draining"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::Body;
    use axum::http::Request;
    use tower::ServiceExt;

    /// The four page routes, which are also the four nav entries.
    const PAGES: [&str; 4] = ["/", "/projects", "/writing", "/about"];

    /// Drive the real routing table through `oneshot` — no socket, no port, but
    /// the same `Router` `main` serves.
    async fn get(path: &str, dev: bool) -> (StatusCode, String) {
        let response = app(AppState::for_tests(dev))
            .oneshot(
                Request::builder()
                    .uri(path)
                    .body(Body::empty())
                    .expect("valid request"),
            )
            .await
            .expect("router is infallible");

        let status = response.status();
        let body = axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .expect("body collects");
        (status, String::from_utf8_lossy(&body).into_owned())
    }

    #[tokio::test]
    async fn every_page_route_returns_200_with_a_span_id() {
        for path in PAGES {
            let (status, body) = get(path, false).await;
            assert_eq!(status, StatusCode::OK, "{path}");
            assert!(
                body.contains("data-span-id=\""),
                "{path} rendered without a join key"
            );
            assert!(
                body.starts_with("<!DOCTYPE html>"),
                "{path} is not a document"
            );
        }
    }

    #[tokio::test]
    async fn the_nav_links_only_to_routes_that_exist() {
        for (href, _) in layout::NAV {
            let (status, _) = get(href, false).await;
            assert_eq!(status, StatusCode::OK, "nav links to {href}, which 404s");
        }
    }

    #[tokio::test]
    async fn fragments_are_bare_and_unknown_names_404() {
        for name in fragments::NAMES {
            let (status, body) = get(&format!("/fragments/{name}"), false).await;
            assert_eq!(status, StatusCode::OK, "{name}");
            assert!(
                body.starts_with("<section data-span-id=\""),
                "{name}: {body:.60}"
            );
            assert!(
                !body.contains("<html"),
                "{name} returned a whole document; an htmx swap would nest it"
            );
        }

        let (status, _) = get("/fragments/no-such-fragment", false).await;
        assert_eq!(status, StatusCode::NOT_FOUND);
    }

    /// Dev mode is the difference between a page that is measured and a page
    /// that only looks measured.
    #[tokio::test]
    async fn dev_mode_injects_the_probe_and_its_config() {
        let (_, dev) = get("/", true).await;
        assert!(dev.contains("window.__UI_SERVO__={"));
        assert!(dev.contains("\"reducedMotionRequired\":true"), "{dev:.400}");
        assert!(dev.contains("src=\"/assets/probe.js\""));

        let (_, plain) = get("/", false).await;
        assert!(!plain.contains("__UI_SERVO__"));
        assert!(!plain.contains("probe.js"));
    }
}
