//! The route manifest: one list of every URL this site answers, and what kind
//! of thing is at each one.
//!
//! There are two consumers — [`crate::server::app`], which builds the axum
//! router, and [`crate::export`], which writes the static site — and before
//! this file existed they each carried their own copy of the list. That is the
//! failure this module exists to make impossible: a page added to the server
//! and forgotten by the exporter is a URL that works in dev and 404s at
//! mosoti.dev, and nothing would have said so.
//!
//! So the manifest is not a list of *strings* to be matched up later. A
//! [`RouteKind::Page`] carries the function pointer that renders it, and both
//! consumers call it. The server cannot serve a page the exporter cannot write,
//! because there is only one page function and one list of paths.
//!
//! The dynamic routes are in here too, even though the exporter skips them. A
//! manifest that listed only the exportable half would be a list of *pages*
//! wearing the name of a list of *routes*, and the next person to add a
//! fragment endpoint would have no reason to think it belonged here.

use crate::error::RouteError;
use crate::pages;
use crate::state::AppState;
use maud::Markup;

/// A page: state in, whole document out. Both binaries call this, and the
/// export test asserts the bytes match what the router serves.
pub type PageRender = fn(&AppState) -> Result<Markup, RouteError>;

/// One route. `path` is the axum pattern, verbatim — the router registers
/// exactly this string, so a typo here is a typo in one place.
#[derive(Clone, Copy, Debug)]
pub struct Route {
    pub path: &'static str,
    pub kind: RouteKind,
}

/// What lives at a route, and therefore what the exporter must do about it.
#[derive(Clone, Copy, Debug)]
pub enum RouteKind {
    /// A whole HTML document. Exported to `<path>/index.html`.
    Page { render: PageRender },

    /// A retired URL. A 308 on the server; a meta-refresh stub in the export,
    /// because GitHub Pages serves files and cannot be told to send a status.
    Redirect { to: &'static str },

    /// An htmx fragment endpoint. Never exported: the path is a pattern, not a
    /// file, and a static host has nothing to match it with. `sample` is a
    /// concrete instantiation, so the router/manifest agreement test can
    /// actually issue a request against a dynamic route.
    Fragment { sample: &'static str },

    /// The static file tree. The exporter copies the directory rather than
    /// rendering anything, so this entry exists to be *skipped* on purpose
    /// rather than by omission.
    Assets { sample: &'static str },
}

/// `/fragments/{name}` — one fragment, by name.
pub const FRAGMENT_BY_NAME: &str = "/fragments/{name}";
/// `/fragments/promoted/{part}` — the human's pick for one part, provenance
/// checked on the way out.
pub const FRAGMENT_PROMOTED: &str = "/fragments/promoted/{part}";
/// The static asset mount point.
pub const ASSETS: &str = "/assets";

/// Every route the site answers. The order is the order the router registers
/// them in, which for axum is not significant — it is grouped for reading.
pub const ROUTES: [Route; 9] = [
    Route {
        path: "/",
        kind: RouteKind::Page {
            render: pages::home::render,
        },
    },
    Route {
        path: "/projects/blackcell",
        kind: RouteKind::Page {
            render: pages::blackcell::render,
        },
    },
    Route {
        path: "/projects/splunk-dashboard-studio",
        kind: RouteKind::Page {
            render: pages::splunk_studio::render,
        },
    },
    Route {
        path: "/about",
        kind: RouteKind::Page { render: about },
    },
    // The two placeholder pages, retired. Permanent rather than temporary
    // because the invented content is not coming back, and a redirect rather
    // than a kept-but-empty page because those URLs are already in somebody's
    // history and a 404 there teaches nothing.
    //
    // Both land on `/`, not on each other's successor: there is no successor
    // yet, and pointing at a page that does not exist is how a redirect becomes
    // a second thing to maintain.
    Route {
        path: "/projects",
        kind: RouteKind::Redirect { to: "/" },
    },
    Route {
        path: "/writing",
        kind: RouteKind::Redirect { to: "/" },
    },
    Route {
        path: FRAGMENT_BY_NAME,
        kind: RouteKind::Fragment {
            sample: "/fragments/colophon",
        },
    },
    Route {
        path: FRAGMENT_PROMOTED,
        kind: RouteKind::Fragment {
            sample: "/fragments/promoted/hero",
        },
    },
    Route {
        path: ASSETS,
        kind: RouteKind::Assets {
            sample: "/assets/tokens.css",
        },
    },
];

/// Routes registered *inside* the `/assets` nest, relative to it.
///
/// They are declared here rather than only in `server.rs` so that
/// [`tests::every_route_in_the_source_is_in_the_manifest`] can tell a
/// deliberately nested route from one somebody forgot to list.
pub const NESTED_ASSET_ROUTES: [&str; 1] = ["/probe.js"];

/// Files served from the document root that are not pages and never will be.
///
/// Same job as [`NESTED_ASSET_ROUTES`]: the source-scan test needs to tell a
/// deliberate registration from one somebody forgot to add to the manifest.
///
/// `/sw.js` cannot live under `/assets/` with everything else it resembles. A
/// service worker's maximum scope is its own directory unless the host sends a
/// `Service-Worker-Allowed` header, and GitHub Pages sends no headers anybody
/// can configure — so a worker at `/assets/sw.js` would be scoped to `/assets/`
/// and could never answer a navigation to `/`, which is the only thing it is
/// for. The URL is the feature.
///
/// Note what this list does *not* buy, because the name invites the assumption:
/// unlike [`RouteKind::Page`], nothing here is a function pointer, so the
/// exporter is not driven by it. `/sw.js` reaches `dist/` because
/// [`crate::export::export`] writes it by hand. A second entry added here would
/// satisfy the source scan and be exported by nobody — so add the write at the
/// same time, or this becomes the list of URLs that work in dev and 404 at
/// mosoti.dev, which is the exact failure `ROUTES` exists to prevent.
pub const ROOT_FILE_ROUTES: [&str; 1] = ["/sw.js"];

/// `/about`, adapted to [`PageRender`].
///
/// `pages::about::render` is infallible and `pages::home::render` is not. The
/// adapter lives here rather than in `pages/` so the manifest can hold one
/// uniform function type without the page modules knowing a manifest exists.
fn about(state: &AppState) -> Result<Markup, RouteError> {
    Ok(pages::about::render(state))
}

impl Route {
    /// A concrete URL that must resolve on the running server. Equal to `path`
    /// for the static kinds; a stand-in instantiation for the dynamic ones.
    pub fn sample(&self) -> &'static str {
        match self.kind {
            RouteKind::Page { .. } | RouteKind::Redirect { .. } => self.path,
            RouteKind::Fragment { sample } | RouteKind::Assets { sample } => sample,
        }
    }

    /// Where the exporter writes this route, relative to `dist/`, or `None` for
    /// the routes a file server cannot represent.
    ///
    /// Directory-plus-`index.html` rather than `about.html`, and the trade-off
    /// is worth stating because it is not free. A file host asked for `/about`
    /// with `about/index.html` on disk answers with a 301 to `/about/`; asked
    /// for the same URL with `about.html` on disk it answers 200 directly, and
    /// then `/about/` is a 404. So the flat layout is one redirect cheaper and
    /// matches axum exactly (which also 404s `/about/`) — but it depends on the
    /// host resolving an extensionless request to a `.html` file, and if that
    /// assumption is wrong the site's main nav link 404s. The directory layout
    /// is wrong by at most one redirect on hosts that behave either way, and
    /// that is the failure worth choosing.
    ///
    /// If the extra hop ever matters, the fix is a `link rel="canonical"` and a
    /// nav that links `/about/`, not a second copy of the page on disk.
    pub fn output_path(&self) -> Option<String> {
        match self.kind {
            RouteKind::Page { .. } | RouteKind::Redirect { .. } => {
                let trimmed = self.path.trim_matches('/');
                Some(if trimmed.is_empty() {
                    "index.html".to_owned()
                } else {
                    format!("{trimmed}/index.html")
                })
            }
            RouteKind::Fragment { .. } | RouteKind::Assets { .. } => None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeSet;

    #[test]
    fn every_path_is_absolute_and_unique() {
        let mut seen = BTreeSet::new();
        for route in ROUTES {
            assert!(
                route.path.starts_with('/'),
                "{} is not an absolute path",
                route.path
            );
            assert!(seen.insert(route.path), "{} is listed twice", route.path);
        }
    }

    #[test]
    fn samples_instantiate_their_own_pattern() {
        for route in ROUTES {
            let sample = route.sample();
            assert!(sample.starts_with('/'), "{sample} is not an absolute path");
            assert!(!sample.contains('{'), "{sample} is still a pattern");
            // The stand-in has to be a URL the pattern could actually match,
            // or the agreement test proves something about a different route.
            let prefix = route.path.split('{').next().expect("split yields one");
            assert!(
                sample.starts_with(prefix),
                "{sample} cannot be matched by {}",
                route.path
            );
        }
    }

    /// Output paths are distinct and stay inside `dist/`.
    ///
    /// `output_path` interpolates the manifest's own path string into a
    /// filesystem path, so `/../x` would write outside the export root and
    /// `/foo` and `/foo/` would silently overwrite each other. Neither is in
    /// the manifest today; both are one careless entry away, and this is the
    /// test that would notice before the export did.
    #[test]
    fn output_paths_are_unique_and_stay_inside_the_export() {
        let mut seen = BTreeSet::new();
        for route in ROUTES {
            let Some(output) = route.output_path() else {
                continue;
            };
            for segment in output.split('/') {
                assert!(
                    !segment.is_empty() && segment != "." && segment != "..",
                    "{} exports to {output:?}, which does not stay inside dist/",
                    route.path
                );
            }
            assert!(
                seen.insert(output.clone()),
                "{} exports to {output:?}, which another route already claimed",
                route.path
            );
        }
    }

    #[test]
    fn exportable_routes_land_on_index_html() {
        let paths: Vec<Option<String>> = ROUTES.iter().map(Route::output_path).collect();
        assert_eq!(paths[0].as_deref(), Some("index.html"));
        assert_eq!(paths[1].as_deref(), Some("projects/blackcell/index.html"));
        assert_eq!(
            paths[2].as_deref(),
            Some("projects/splunk-dashboard-studio/index.html")
        );
        assert_eq!(paths[3].as_deref(), Some("about/index.html"));
        assert_eq!(paths[4].as_deref(), Some("projects/index.html"));
        assert_eq!(paths[5].as_deref(), Some("writing/index.html"));
        for route in ROUTES.iter().skip(6) {
            assert_eq!(route.output_path(), None, "{} is not a file", route.path);
        }
    }

    /// No `.route()` or `.nest()` call in the server names a path the manifest
    /// has never heard of.
    ///
    /// The other direction is structural — `app()` iterates `ROUTES` and gets
    /// its path strings from it, so the manifest cannot contain a route the
    /// router skipped. This is the half that is *not* structural: nothing stops
    /// somebody adding a route by hand next to the loop, and that route would
    /// be invisible to the exporter. Reading the source is crude, and it is
    /// also the only thing that notices, because axum's `Router` will not tell
    /// anyone what is in it.
    #[test]
    fn every_route_in_the_source_is_in_the_manifest() {
        let source = include_str!("server.rs");
        let known: BTreeSet<&str> = ROUTES
            .iter()
            .map(|route| route.path)
            .chain(NESTED_ASSET_ROUTES)
            .chain(ROOT_FILE_ROUTES)
            .collect();

        let mut found = 0;
        for call in [".route(\"", ".nest(\""] {
            for (index, _) in source.match_indices(call) {
                let rest = &source[index + call.len()..];
                let path = &rest[..rest.find('"').expect("a route literal is quoted")];
                assert!(
                    known.contains(path),
                    "server.rs registers {path:?}, which is not in routes::ROUTES \
                     (nor in NESTED_ASSET_ROUTES or ROOT_FILE_ROUTES). Add it, or \
                     the exporter will never know it exists."
                );
                found += 1;
            }
        }
        assert!(found > 0, "no route literals found; has server.rs moved?");
    }
}
