//! The HTTP half: the routing table, the handlers, and graceful shutdown.
//!
//! Every route in here comes out of [`crate::routes::ROUTES`] — the paths are
//! read from the manifest rather than typed again, so the router and the static
//! exporter cannot disagree about which URLs exist. See `routes.rs` for why
//! that matters more than it looks.

use crate::error::{RouteError, StartupError};
use crate::fragments;
use crate::routes::{self, Route, RouteKind};
use crate::state::AppState;
use axum::Router;
use axum::extract::{Path, State};
use axum::http::{HeaderValue, StatusCode, header};
use axum::response::{IntoResponse, Redirect, Response};
use axum::routing::get;
use tower_http::trace::TraceLayer;

/// Default listen port. `UI_SERVO_PORT` overrides.
pub const DEFAULT_PORT: u16 = 8080;

/// Configure logging, read the environment, bind, serve until a signal.
pub async fn run() -> Result<(), StartupError> {
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
        routes = ROUTES_LEN,
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

const ROUTES_LEN: usize = routes::ROUTES.len();

/// The whole routing table, built from the manifest.
///
/// The loop is not decoration over a list that could have been written out. It
/// is what makes "the exporter renders every page the server serves" true by
/// construction: a page reaches this router only by being in `ROUTES`, and
/// being in `ROUTES` is also how it reaches `dist/`.
pub fn app(state: AppState) -> Router {
    let mut router = Router::new();
    for route in routes::ROUTES {
        router = register(router, route);
    }
    // Not in the manifest, because it is not a page and not an asset: a service
    // worker's scope is the directory it is served from, so this one has to sit
    // at the document root or it could never answer a navigation. Declared in
    // `routes::ROOT_FILE_ROUTES` so the source scan knows it is deliberate.
    router = router.route("/sw.js", get(service_worker));
    router.layer(TraceLayer::new_for_http()).with_state(state)
}

/// One manifest entry, wired to the handler its kind implies.
///
/// The `match` is exhaustive over [`RouteKind`], so a new kind cannot be added
/// to the manifest without the compiler stopping here and asking what the
/// server should do with it.
fn register(router: Router<AppState>, route: Route) -> Router<AppState> {
    match route.kind {
        RouteKind::Page { render } => router.route(
            route.path,
            get(move |State(state): State<AppState>| async move { render(&state) }),
        ),
        RouteKind::Redirect { to } => router.route(
            route.path,
            get(move || async move { Redirect::permanent(to) }),
        ),
        // Dynamic: one handler each, selected by the manifest's own constant so
        // the path string still exists in exactly one place.
        RouteKind::Fragment { .. } => match route.path {
            routes::FRAGMENT_BY_NAME => router.route(route.path, get(fragment)),
            routes::FRAGMENT_PROMOTED => router.route(route.path, get(promoted_fragment)),
            unwired => panic!(
                "routes::ROUTES lists the fragment route {unwired:?}, which server::register \
                 has no handler for. Wire it here, or the manifest is claiming a URL that 404s."
            ),
        },
        RouteKind::Assets { .. } => router.nest(route.path, assets_router()),
    }
}

/// The static file tree.
fn assets_router() -> Router<AppState> {
    Router::new()
        // Explicit before the directory: in dev, probe.js is read live from the
        // sibling `probe/` unit so editing the sensor does not need a rebuild.
        .route("/probe.js", get(probe_js))
        .fallback(get(static_asset))
}

/// A bare fragment, for htmx to swap. No shell, no doctype — returning a whole
/// document here would nest `<html>` inside the live page and quietly corrupt
/// every measurement taken afterwards.
async fn fragment(Path(name): Path<String>) -> Result<maud::Markup, RouteError> {
    fragments::render(&name).ok_or(RouteError::UnknownFragment(name))
}

/// A human pick, served only if it can prove it was gated. An unpromoted part is
/// a 404; an ungated or edited one is a 500, because serving it would be the one
/// way an unreviewed fragment reaches a visitor.
async fn promoted_fragment(
    State(state): State<AppState>,
    Path(part): Path<String>,
) -> Result<maud::Markup, RouteError> {
    // Through `state`, not straight off disk: in release that is the map built
    // and verified at boot, so this route and the home page cannot disagree
    // about what is promoted, and a post-boot edit cannot reach a visitor here
    // while the cached page still serves the version that was checked.
    match state
        .promoted(&part)
        .map(|p| fragments::promoted::render_verified(&p))
    {
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

/// Serve one file from the asset directory, having proved it is in there.
///
/// This replaced `ServeDir`, and the reason is the third instance of one bug.
/// Promoted markup must be unreachable except through the route that verifies
/// it, and that was defended first by deny routes (beaten by percent-encoding:
/// axum matches the raw path, `ServeDir` decodes it), then by moving the files
/// out of the served tree and checking the roots at boot (beaten by a symlink
/// inside the tree), then by also walking for symlinks at boot — beaten by
/// creating the link before its target exists, and by creating it after boot.
///
/// The pattern is that a check performed *once*, about a filesystem that changes
/// *continuously*, is a guess. So containment is now established per request, on
/// the resolved path, at the moment of serving: canonicalise, and refuse
/// anything that does not land inside the asset root. A symlink cannot widen
/// that, whenever it was created, because the answer is computed after the
/// kernel has followed it.
///
/// **What this deliberately does not defend, and why.** A *hardlink* from the
/// served directory to a promoted file is served — verified, not overlooked. It
/// is also indistinguishable, to any path-based check, from `cp promoted/hero.html
/// assets/`: both are a regular file inside the root, with the right content, put
/// there by somebody with write access. A rule that catches the hardlink and not
/// the copy would buy nothing and read as coverage, which is the failure mode
/// this file has already produced three times. So the boundary is stated instead:
/// this defends against *reaching outside* the served directory, not against the
/// contents of the served directory, and anyone who can write into it can serve
/// whatever they like from it — including, no doubt, better markup than the loop
/// would have picked.
async fn static_asset(
    State(state): State<AppState>,
    uri: axum::http::Uri,
) -> Result<Response, RouteError> {
    let relative = uri.path().trim_start_matches('/');
    let decoded = percent_decode(relative);

    // Reject anything that is not a plain sequence of names before touching the
    // disk. `..` is handled by the containment check below as well; this is the
    // cheap half, and it keeps the error honest for the obvious cases.
    if decoded.is_empty()
        || decoded
            .split('/')
            .any(|part| part.is_empty() || part == "." || part == ".." || part.contains('\0'))
    {
        return Err(RouteError::UnknownFragment(decoded));
    }

    let root = state
        .assets_dir()
        .canonicalize()
        .map_err(|_| RouteError::UnknownFragment(decoded.clone()))?;
    let resolved = root
        .join(&decoded)
        .canonicalize()
        .map_err(|_| RouteError::UnknownFragment(decoded.clone()))?;

    // The whole point: this is the *resolved* path, so a symlink has already
    // been followed and cannot smuggle the answer past us.
    if !resolved.starts_with(&root) || !resolved.is_file() {
        return Err(RouteError::UnknownFragment(decoded));
    }

    let body = tokio::fs::read(&resolved)
        .await
        .map_err(|_| RouteError::UnknownFragment(decoded.clone()))?;
    Ok((
        [(
            header::CONTENT_TYPE,
            HeaderValue::from_static(content_type_of(&resolved)),
        )],
        body,
    )
        .into_response())
}

/// The `Content-Type` for one asset, by extension.
///
/// Shared with the exporter, which has no server to ask: GitHub Pages picks the
/// type from the same extension, and this is the list that says which
/// extensions the site expects to ship at all.
pub fn content_type_of(path: &std::path::Path) -> &'static str {
    match path.extension().and_then(|ext| ext.to_str()) {
        Some("css") => "text/css; charset=utf-8",
        Some("js") => "text/javascript; charset=utf-8",
        Some("json") => "application/json",
        Some("webmanifest") => "application/manifest+json",
        Some("wasm") => "application/wasm",
        Some("svg") => "image/svg+xml",
        Some("png") => "image/png",
        // Without this the résumé is served as octet-stream, which every
        // browser turns into a download prompt rather than a document.
        Some("pdf") => "application/pdf",
        Some("woff2") => "font/woff2",
        Some("html") => "text/html; charset=utf-8",
        _ => "application/octet-stream",
    }
}

/// Percent-decoding, so containment is checked against the path the filesystem
/// will actually see rather than the one the client typed.
///
/// Doing this ourselves is the lesson from the first bypass: the router matched
/// the raw path while the file server matched the decoded one, and every rule
/// written against the raw form was decoration.
fn percent_decode(raw: &str) -> String {
    let bytes = raw.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        match bytes[index] {
            b'%' if index + 2 < bytes.len() => {
                let hex = std::str::from_utf8(&bytes[index + 1..index + 3]).unwrap_or("");
                match u8::from_str_radix(hex, 16) {
                    Ok(byte) => {
                        out.push(byte);
                        index += 3;
                    }
                    Err(_) => {
                        out.push(bytes[index]);
                        index += 1;
                    }
                }
            }
            byte => {
                out.push(byte);
                index += 1;
            }
        }
    }
    String::from_utf8_lossy(&out).into_owned()
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

/// Serve the service worker, stamped `dev`.
///
/// The same embedded template the exporter ships, with the version token
/// replaced by a name no build can produce. Three things follow from that, all
/// wanted: anyone who opens this file in devtools can see at a glance that this
/// instance's cache is not a production one; a browser that visited a real
/// deploy first will not confuse the two caches; and the worker itself reads
/// the stamp and refuses to keep anything — `cargo run` without `UI_SERVO_DEV`
/// is a non-dev server, so its pages *do* register this, and a cache-first
/// worker on `127.0.0.1` with a version that never changes would hide every
/// subsequent asset edit behind it. See `EPHEMERAL` in `src/sw.js`.
///
/// `no-store`, like `probe.js` and for a sharper reason: a worker outlives the
/// page that registered it, so a cached copy of this file would keep serving a
/// version of the site the developer edited away an hour ago — and would do it
/// from behind the very cache that makes the staleness invisible.
async fn service_worker() -> Response {
    (
        StatusCode::OK,
        [
            (
                header::CONTENT_TYPE,
                HeaderValue::from_static("text/javascript; charset=utf-8"),
            ),
            (header::CACHE_CONTROL, HeaderValue::from_static("no-store")),
        ],
        crate::export::SW_TEMPLATE.replace(crate::export::SW_VERSION_PLACEHOLDER, "dev"),
    )
        .into_response()
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
    use crate::layout;
    use axum::body::Body;
    use axum::http::Request;
    use tower::ServiceExt;

    /// The two page routes, which are also the two nav entries — read off the
    /// manifest rather than restated, so a third page joins this test by being
    /// added to `ROUTES`. `/projects` and `/writing` are redirects, and are
    /// asserted as redirects in `the_retired_pages_redirect_permanently`.
    fn pages() -> Vec<&'static str> {
        routes::ROUTES
            .iter()
            .filter(|route| matches!(route.kind, RouteKind::Page { .. }))
            .map(|route| route.path)
            .collect()
    }

    /// Drive the real routing table through `oneshot` — no socket, no port, but
    /// the same `Router` `main` serves.
    async fn response(path: &str, dev: bool) -> Response {
        app(AppState::for_tests(dev))
            .oneshot(
                Request::builder()
                    .uri(path)
                    .body(Body::empty())
                    .expect("valid request"),
            )
            .await
            .expect("router is infallible")
    }

    async fn get(path: &str, dev: bool) -> (StatusCode, String) {
        let response = response(path, dev).await;
        let status = response.status();
        let body = axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .expect("body collects");
        (status, String::from_utf8_lossy(&body).into_owned())
    }

    /// One response header, or the empty string. Enough to judge a redirect or
    /// an asset without collecting a body.
    async fn header_of(path: &str, name: header::HeaderName) -> (StatusCode, String) {
        let response = response(path, false).await;
        let value = response
            .headers()
            .get(name)
            .and_then(|value| value.to_str().ok())
            .unwrap_or_default()
            .to_owned();
        (response.status(), value)
    }

    #[tokio::test]
    async fn every_page_route_returns_200_with_a_span_id() {
        for path in pages() {
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

    /// The router answers for every route in the manifest, in the way that
    /// route's kind promises.
    ///
    /// This is the assertion that keeps `routes.rs` honest as a description of
    /// the server rather than a wish about it. The exporter reads the same
    /// list and does not make requests at all, so if the manifest claims a page
    /// the router does not serve, this is the only place it surfaces.
    #[tokio::test]
    async fn the_router_answers_for_every_manifest_route() {
        for route in routes::ROUTES {
            let sample = route.sample();
            match route.kind {
                RouteKind::Page { .. } => {
                    let (status, body) = get(sample, false).await;
                    assert_eq!(status, StatusCode::OK, "{sample}");
                    assert!(
                        body.starts_with("<!DOCTYPE html>"),
                        "{sample} is listed as a page but is not a document"
                    );
                }
                RouteKind::Redirect { to } => {
                    let (status, location) = header_of(sample, header::LOCATION).await;
                    assert_eq!(status, StatusCode::PERMANENT_REDIRECT, "{sample}");
                    assert_eq!(location, to, "{sample}");
                }
                RouteKind::Fragment { .. } => {
                    let (status, body) = get(sample, false).await;
                    // A 404 here has two possible causes and the message names
                    // both: the route was never registered, or the sample names
                    // something that is not in the repo any more (a fragment
                    // renamed, a promotion withdrawn). Either way the manifest
                    // is now describing a URL nobody can fetch.
                    assert_eq!(
                        status,
                        StatusCode::OK,
                        "{sample} does not resolve — either {} is unrouted, or the \
                         thing the sample names is gone from the repo",
                        route.path
                    );
                    assert!(
                        !body.contains("<html"),
                        "{sample} is listed as a fragment but returned a document"
                    );
                }
                RouteKind::Assets { .. } => {
                    let (status, _) = get(sample, false).await;
                    assert_eq!(status, StatusCode::OK, "{sample}");
                }
            }
        }
    }

    /// A path the manifest does not list is a 404 rather than something the
    /// router quietly picked up. Without this, "the router answers for every
    /// manifest route" would also pass on a router that answered for
    /// everything.
    #[tokio::test]
    async fn a_path_outside_the_manifest_is_not_served() {
        for path in ["/blog", "/about/", "/projects/index.html", "/fragments"] {
            let (status, _) = get(path, false).await;
            assert_ne!(status, StatusCode::OK, "{path} is served but unlisted");
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

    /// The retired pages. A redirect *and* a page at one path would be two
    /// things to keep in step, so the modules were deleted rather than kept
    /// unrouted — an unrouted page still compiles, still passes review, and
    /// still tempts somebody to route it again.
    #[tokio::test]
    async fn the_retired_pages_redirect_permanently() {
        for path in ["/projects", "/writing"] {
            let (status, location) = header_of(path, header::LOCATION).await;
            assert_eq!(status, StatusCode::PERMANENT_REDIRECT, "{path}");
            assert_eq!(location, "/", "{path}");
        }
    }

    /// Nothing the site renders links to a route that redirects.
    ///
    /// `the_nav_links_only_to_routes_that_exist` cannot catch this: a redirect
    /// is not a 404, so a stale link passes that test while costing every
    /// visitor who follows it a second request and a wrong-looking address bar.
    /// Checked over whole pages rather than the nav, because the About page's
    /// "the projects page" link was exactly this and lived in the body.
    #[tokio::test]
    async fn no_page_links_to_a_route_that_redirects() {
        for path in pages() {
            let (_, body) = get(path, false).await;
            for retired in ["/projects", "/writing"] {
                assert!(
                    !body.contains(&format!("href=\"{retired}\"")),
                    "{path} links to {retired}, which redirects"
                );
            }
        }
    }

    /// The résumé is the one asset a visitor is asked to open rather than the
    /// browser to consume, and it has to arrive as a document rather than as a
    /// download prompt. The favicon is here too because a `<link rel="icon">`
    /// that 404s is invisible in every test that only reads markup.
    #[tokio::test]
    async fn the_committed_assets_serve_with_the_right_type() {
        for (path, content_type) in [
            ("/assets/kennedy-mosoti-resume.pdf", "application/pdf"),
            ("/assets/favicon.svg", "image/svg+xml"),
            ("/assets/manifest.webmanifest", "application/manifest+json"),
            ("/assets/icons/icon-192.png", "image/png"),
            ("/assets/icons/icon-512.png", "image/png"),
            ("/assets/icons/icon-512-maskable.png", "image/png"),
            ("/assets/icons/apple-touch-180.png", "image/png"),
        ] {
            let (status, actual) = header_of(path, header::CONTENT_TYPE).await;
            assert_eq!(status, StatusCode::OK, "{path}");
            assert_eq!(actual, content_type, "{path}");
        }

        // Every asset the portfolio shell asks for on every page load, so a
        // rename shows up here rather than as a silent 404 in somebody's
        // console. `/about` still wears the classic shell, so its assets are
        // checked against it separately.
        let (_, page) = get("/", false).await;
        for href in [
            "/assets/favicon.svg",
            "/assets/fonts/fonts.css",
            "/assets/portfolio.css",
            "/assets/portfolio.js",
            "/assets/manifest.webmanifest",
            "/assets/icons/apple-touch-180.png",
        ] {
            assert!(page.contains(href), "the shell no longer references {href}");
            let (status, _) = get(href, false).await;
            assert_eq!(
                status,
                StatusCode::OK,
                "the shell references {href}, which 404s"
            );
        }
        let (_, page) = get("/about", false).await;
        for href in [
            "/assets/favicon.svg",
            "/assets/tokens.css",
            "/assets/site.css",
            "/assets/htmx.min.js",
            "/assets/islands/loader.js",
            // Owner-allowed exception (batch, 2026-08-09): tokens.css has
            // always declared "JetBrains Mono" for this shell; /about now
            // actually loads the webfont it asks for.
            "/assets/fonts/fonts.css",
            "/assets/manifest.webmanifest",
            "/assets/icons/apple-touch-180.png",
        ] {
            assert!(page.contains(href), "the shell no longer references {href}");
            let (status, _) = get(href, false).await;
            assert_eq!(
                status,
                StatusCode::OK,
                "the shell references {href}, which 404s"
            );
        }
    }

    /// Both shells promise the same installable identity — `layout::pwa_head`
    /// is the one place that draws it, so this is really one assertion (the
    /// two shells agree) checked five substrings deep.
    #[tokio::test]
    async fn both_shells_declare_the_pwa_head() {
        for path in ["/", "/about"] {
            let (_, page) = get(path, false).await;
            for needle in [
                r#"link rel="manifest" href="/assets/manifest.webmanifest""#,
                r#"link rel="apple-touch-icon" href="/assets/icons/apple-touch-180.png""#,
                r##"meta name="theme-color" content="#08090b""##,
                r#"meta name="apple-mobile-web-app-capable" content="yes""#,
                r#"meta name="apple-mobile-web-app-status-bar-style" content="black-translucent""#,
            ] {
                assert!(page.contains(needle), "{path} is missing {needle:?}");
            }
        }
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

        // And the other direction, on the same flag: the service worker is the
        // probe's inverse. A worker registered during a measured run would hold
        // the previous render in a cache the loop cannot see, so the page under
        // measurement would stop being the page the server just built.
        assert!(
            !dev.contains("sw-register.js"),
            "a dev page registers the service worker"
        );
        assert!(
            plain.contains("src=\"/assets/sw-register.js\""),
            "a non-dev page does not register the service worker — the export is byte-identical \
             to this page, so it would ship without offline support"
        );
    }

    /// The worker is served from the document root, stamped, and uncacheable.
    ///
    /// The root is the contract: a worker's scope is its own directory, and one
    /// served from `/assets/` could not answer a navigation however correct the
    /// rest of it was. `dev` as the version keeps this instance's cache name
    /// apart from any real build's.
    #[tokio::test]
    async fn the_service_worker_is_served_at_the_root_stamped_dev() {
        let (status, body) = get("/sw.js", false).await;
        assert_eq!(status, StatusCode::OK);
        assert!(
            !body.contains(crate::export::SW_VERSION_PLACEHOLDER),
            "the served worker is unstamped"
        );
        assert!(body.contains("VERSION = 'dev'"), "{body:.200}");
        assert!(body.contains("CACHE_PREFIX + VERSION"), "{body:.200}");

        let (_, content_type) = header_of("/sw.js", header::CONTENT_TYPE).await;
        assert_eq!(content_type, "text/javascript; charset=utf-8");
        let (_, cache_control) = header_of("/sw.js", header::CACHE_CONTROL).await;
        assert_eq!(cache_control, "no-store");
    }
}
