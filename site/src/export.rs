//! Static export: the same site, as files, for GitHub Pages.
//!
//! mosoti.dev is served by a file host. A file host cannot run axum, cannot
//! send a 308, and cannot answer `/fragments/{name}`. So this module renders
//! every page route by *calling the maud function* — no server, no socket, no
//! HTTP client — and writes the result where a file host will find it.
//!
//! Three things are load-bearing about how it does that.
//!
//! **It reads [`crate::routes::ROUTES`].** Not its own list. An exporter with
//! its own list is a second routing table maintained by hope, and the symptom
//! is a page that works locally and 404s in production.
//!
//! **It fails, loudly, rather than emitting a subtly broken site.** A file host
//! has no logs anyone reads and no error page worth the name; a dangling link
//! discovered by a visitor is discovered too late. So [`link_check`] resolves
//! every internal URL-valued attribute — `href`, `src`, and the htmx ones,
//! which no link checker looks at and a file host cannot answer — against what
//! was actually written, and the export exits non-zero if one does not land on
//! a file.
//!
//! **The résumé is checked byte-for-byte.** It is the only asset on the site
//! that is a *document* rather than an ingredient — the one file where a
//! truncated copy is both plausible and silent, because a broken PDF still has
//! a size and still returns 200.

use crate::layout::{PageMeta, shell};
use crate::routes::{self, RouteKind};
use crate::state::AppState;
use maud::{Markup, html};
use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};

/// The custom domain. GitHub Pages reads this file at the root of the published
/// branch and serves the site from that host.
pub const CNAME: &str = "mosoti.dev";

/// The résumé, by name, because it is asserted rather than assumed.
pub const RESUME_PDF: &str = "kennedy-mosoti-resume.pdf";

/// Where the assets land inside `dist/`. Same path as the server mounts them
/// at, so every `/assets/…` reference in the markup is correct in both.
const ASSETS_DIR: &str = "assets";

/// The token in [`SW_TEMPLATE`] that both the exporter and the server replace
/// before anything reads the file. A worker still carrying it has been served
/// by neither, which is worth being able to assert.
pub const SW_VERSION_PLACEHOLDER: &str = "__UI_SERVO_SW_VERSION__";

/// The service worker, embedded at compile time.
///
/// Unlike `probe.js` — dev tooling, read live off disk so editing the sensor
/// needs no rebuild — the worker is release-path code: it decides what a
/// visitor sees when the network does not answer. Embedding it means the
/// exporter cannot ship a file the server was never compiled against, and the
/// two cannot be different versions of the same worker.
pub const SW_TEMPLATE: &str = include_str!("sw.js");

/// Where the stamped worker lands. The document root, because a worker's scope
/// is its own directory and GitHub Pages cannot be told otherwise.
const SW_FILE: &str = "sw.js";

/// Tells GitHub Pages not to run Jekyll over the directory.
const NOJEKYLL: &str = ".nojekyll";

/// The marker that says "this directory was produced by this exporter, and may
/// therefore be deleted by it". See [`prepare`].
///
/// Not `.nojekyll`, which was the first version of this and was wrong: every
/// GitHub Pages directory in the world has one, so it authorised a recursive
/// delete of any published site somebody happened to point the output argument
/// at. The marker has to be evidence of *this* exporter, so it is a name nobody
/// else uses carrying content nobody else writes.
const MARKER: &str = ".ui-servo-export";

/// The exact contents of [`MARKER`]. Compared byte for byte: a file that merely
/// has the right name is a coincidence, and coincidences should not authorise
/// `remove_dir_all`.
const MARKER_MAGIC: &str = "ui-servo static export — safe to delete and regenerate\n";

#[derive(Debug, thiserror::Error)]
pub enum ExportError {
    #[error("cannot render {path}: {source}")]
    Render {
        path: &'static str,
        #[source]
        source: crate::error::RouteError,
    },

    #[error("cannot write {path}: {source}")]
    Write {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },

    #[error("cannot read {path}: {source}")]
    Read {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },

    /// The output directory exists and does not look like ours.
    ///
    /// The export starts by deleting its output directory, and a recursive
    /// delete pointed at the wrong path by a typo or a stray argument is not a
    /// failure anyone gets to undo. So the directory must either be empty, or
    /// carry a [`MARKER`] whose contents are exactly [`MARKER_MAGIC`].
    #[error(
        "{0} already exists, is not empty, and has no {MARKER} in it saying this exporter wrote \
         it. Refusing to delete it — point the export somewhere else, or remove it by hand if \
         it really is stale."
    )]
    NotOurs(PathBuf),

    /// The output directory would overlap an input.
    ///
    /// The export deletes its output directory first, so an output path that
    /// contains — or sits inside — one of its own inputs is a command that
    /// deletes them. The empty path is the reachable version of this: `--` with
    /// no argument, or an unset-but-exported `UI_SERVO_DIST`, both land on the
    /// working directory, which from `site/` is the crate.
    ///
    /// Two inputs are protected. `assets/` is the obvious one. `promoted/` is
    /// the one that bites quietly: exporting into it fills it with `index.html`
    /// and friends, every release boot then reads those as promoted fragments
    /// that cannot prove they were gated, and the *server* stops starting — a
    /// failure that shows up nowhere near the command that caused it.
    #[error(
        "the export destination {dist} overlaps {input}, which the export reads from — and the \
         export deletes its destination before writing. Choose a destination outside the source \
         tree."
    )]
    UnsafeDestination { dist: PathBuf, input: PathBuf },

    /// A symlink in the asset tree.
    ///
    /// Refused outright rather than followed-and-checked. Checking a link and
    /// then copying it is two operations on a path that a third party can
    /// change in between, and the whole class disappears if the export simply
    /// declines to publish anything that is not a plain file. The server is
    /// looser on purpose — it re-establishes containment per request, which a
    /// build step cannot do for an artefact it hands to a CDN — so an asset
    /// layout that serves fine can still be unexportable, and this says which
    /// file and why rather than failing somewhere downstream.
    #[error(
        "{link} is a symlink. The published site is plain files only: replace it with the file \
         it points at, so what is uploaded cannot depend on what a link meant at the moment it \
         was read."
    )]
    AssetSymlink { link: PathBuf },

    /// The file that was opened is not the file that was checked.
    ///
    /// The only way to catch a swap performed between the check and the open.
    /// Identity is compared *after* opening, so whatever happened to the path in
    /// between, the bytes about to be copied are the ones that were inspected.
    #[error(
        "{path} changed between being checked and being opened, so what would be published is \
         not what was inspected. Re-run the export."
    )]
    AssetChanged { path: PathBuf },

    /// The opened asset is not inside the asset tree.
    ///
    /// [`AssetChanged`](Self::AssetChanged) compares the leaf's identity, which
    /// says nothing about how the path reached it: a *parent* directory swapped
    /// for a symlink between the walk and the open hands both the check and the
    /// open the same out-of-tree file, in perfect agreement with each other. So
    /// after opening, the file's real location is read back — from the
    /// descriptor itself where the platform allows — and it must sit under the
    /// canonical asset root, or nothing is published.
    #[error(
        "{path} opened to a file at {resolved}, outside the asset tree. A directory on the way \
         to it is not what it was when the export started — nothing is published from outside \
         assets/."
    )]
    AssetOutsideTree { path: PathBuf, resolved: PathBuf },

    #[error(
        "{name} was copied but changed on the way: source {source_hash}, copy {copy_hash}. The \
         published PDF would not be the one in the repo."
    )]
    AssetCorrupted {
        name: String,
        source_hash: String,
        copy_hash: String,
    },

    #[error("{count} broken internal reference(s) in the exported site:\n{details}")]
    BrokenLinks { count: usize, details: String },
}

/// What one export produced. Printed by the binary, asserted by the tests.
#[derive(Debug)]
pub struct Report {
    pub dist: PathBuf,
    /// Documents written from the manifest, relative to `dist/`.
    pub pages: Vec<String>,
    /// Files copied out of `assets/`.
    pub assets: usize,
    /// Internal URL-valued attributes resolved by [`link_check`].
    pub links: usize,
}

impl Report {
    /// Every file in `dist/`, relative and sorted — the listing a human wants
    /// after a build, and the thing a deploy uploads.
    pub fn files(&self) -> Result<Vec<String>, ExportError> {
        let mut files: Vec<String> = walk(&self.dist)?
            .into_iter()
            .map(|path| relative(&self.dist, &path))
            .collect();
        files.sort();
        Ok(files)
    }
}

/// Render the whole site into `dist`.
pub fn export(state: &AppState, dist: &Path) -> Result<Report, ExportError> {
    // Everything below works on the *resolved* destination, not the one as
    // typed. Validating one path and then deleting another is how a final
    // component that happens to be a symlink gets unlinked and replaced by a
    // directory, with the real target left stale and the command reporting
    // success.
    let dist = &guard_destination(state, dist)?;
    prepare(dist)?;

    let mut pages = Vec::new();
    for route in routes::ROUTES {
        let Some(output) = route.output_path() else {
            // Fragments and the asset tree. Skipped by kind, not by omission:
            // `output_path` is a `match` over every `RouteKind`, so a new kind
            // has to decide whether it is a file before this compiles.
            continue;
        };
        let document = match route.kind {
            RouteKind::Page { render } => render(state)
                .map_err(|source| ExportError::Render {
                    path: route.path,
                    source,
                })?
                .into_string(),
            RouteKind::Redirect { to } => redirect_stub(to).into_string(),
            RouteKind::Fragment { .. } | RouteKind::Assets { .. } => {
                unreachable!("{} has an output path but is not a document", route.path)
            }
        };
        write(&dist.join(&output), document.as_bytes())?;
        pages.push(output);
    }

    // Not in the manifest, because it is not a route: no server ever matches
    // `/404.html`. GitHub Pages serves it for anything it cannot find, which is
    // the one page on the site whose whole job is to be reached by mistake.
    write(
        &dist.join("404.html"),
        not_found(state).into_string().as_bytes(),
    )?;
    pages.push("404.html".to_owned());

    let assets = copy_assets(state.assets_dir(), &dist.join(ASSETS_DIR))?;
    verify_resume(state.assets_dir(), &dist.join(ASSETS_DIR))?;

    // A trailing newline: it is a text file, every tool that writes one adds
    // it, and GitHub trims surrounding whitespace before comparing the host.
    write(&dist.join("CNAME"), format!("{CNAME}\n").as_bytes())?;
    // Empty on purpose. Its presence is the whole message: do not run Jekyll
    // over this directory, which would otherwise drop every path beginning
    // with an underscore.
    write(&dist.join(NOJEKYLL), b"")?;

    // Stamped last but one: every file the worker will cache is already on
    // disk, so the version names exactly this asset set and nothing else. A
    // fingerprint taken earlier would name a site that had not been written
    // yet, and two different builds could then share a cache.
    //
    // `link_check` reads `.html` files only, so the string literals in the
    // worker's precache list are never mistaken for links — they are URLs the
    // browser asks for, not references a file host has to resolve, and the
    // pages they name are asserted separately in
    // `the_service_worker_is_stamped_and_precaches_every_page`.
    let version = site_fingerprint(dist)?;
    write(
        &dist.join(SW_FILE),
        SW_TEMPLATE
            .replace(SW_VERSION_PLACEHOLDER, &version)
            .as_bytes(),
    )?;

    let links = link_check(dist)?;

    Ok(Report {
        dist: dist.to_path_buf(),
        pages,
        assets,
        links,
    })
}

/// Refuse a destination that overlaps any input.
///
/// Checked before anything is deleted, and through [`resolved`] rather than
/// lexically. Lexical comparison was the first version and it was not enough: a
/// symlinked ancestor — `/tmp/site -> …/ui-servo/site`, then `-- /tmp/site/assets/out`
/// — spells a path that shares no prefix with `assets/` and lands inside it
/// anyway, which is a recursive delete of the site's own stylesheet, tokens and
/// résumé authorised by a string comparison.
/// Returns the resolved destination, which is the one every later step uses.
fn guard_destination(state: &AppState, dist: &Path) -> Result<PathBuf, ExportError> {
    let dist = resolved(dist);
    for input in [
        state.assets_dir().to_path_buf(),
        state
            .promoted_root()
            .join(crate::fragments::promoted::PROMOTED_DIR),
    ] {
        let input = resolved(&input);
        // Either direction is fatal. `dist` inside an input publishes the
        // export into something it reads; an input inside `dist` means step one
        // deletes it.
        if dist.starts_with(&input) || input.starts_with(&dist) {
            return Err(ExportError::UnsafeDestination { dist, input });
        }
    }
    Ok(dist)
}

/// A path made absolute and symlink-free as far as it exists.
///
/// The destination usually does not exist yet, so `canonicalize` alone fails on
/// it. Canonicalise the nearest ancestor that *does* exist and re-append the
/// rest: that resolves every symlink actually in the path while still naming a
/// path that has not been created. (This is the same shape as `state::resolve`,
/// which learned it from a bug where the relative side stayed relative and the
/// overlap check therefore always passed.)
fn resolved(path: &Path) -> PathBuf {
    let absolute = if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()
            .unwrap_or_else(|_| PathBuf::from("."))
            .join(path)
    };

    // To a fixpoint, not in one pass. One pass canonicalises the nearest
    // existing ancestor and folds the rest on lexically — but folding a `..`
    // can *expose* a component that exists and is a symlink:
    // `scratch/missing/../alias/out` canonicalises only `scratch` (nothing
    // deeper exists as spelled), cancels `missing`, and would hand back
    // `alias` raw. The folded path has no `..` left in it, so the next pass
    // walks straight down to `alias`, canonicalises through it, and the pass
    // after that changes nothing. Each pass only removes dot components or
    // resolves links, so the bound is small; eight is paranoia, and running
    // out of it returns the last resolution rather than the original spelling.
    let mut current = absolute;
    for _ in 0..8 {
        let next = resolve_once(&current);
        if next == current {
            return next;
        }
        current = next;
    }
    current
}

/// One resolution pass: canonicalise the nearest existing ancestor, fold the
/// not-yet-existing suffix on lexically.
///
/// `.` and `..` inside the suffix are resolved by skipping and popping — the
/// pops land on an already-canonical base, never on the spelled path.
/// Re-appending them raw was the original hole, and bailing out through
/// `file_name()` (which is `None` for a trailing `..`) was the other half of
/// it: `alias/missing/../out` never resolved `alias` at all.
fn resolve_once(absolute: &Path) -> PathBuf {
    let mut missing: Vec<std::ffi::OsString> = Vec::new();
    let mut existing = absolute;
    loop {
        if let Ok(canonical) = existing.canonicalize() {
            let mut out = canonical;
            for part in missing.iter().rev() {
                if part == "." {
                    continue;
                }
                if part == ".." {
                    out.pop();
                } else {
                    out.push(part);
                }
            }
            return out;
        }
        match (existing.components().next_back(), existing.parent()) {
            (Some(component), Some(parent)) => {
                missing.push(component.as_os_str().to_owned());
                existing = parent;
            }
            _ => return normalize(absolute),
        }
    }
}

/// Empty the output directory, having first established that it is ours, and
/// claim the empty one immediately.
///
/// The marker is written *before* any content, not after. Writing it last
/// looks tidier and is worse: an export that fails halfway leaves a non-empty
/// unmarked directory, and every retry from then on refuses to touch it.
fn prepare(dist: &Path) -> Result<(), ExportError> {
    if dist.exists() {
        if !dist.is_dir() {
            return Err(ExportError::NotOurs(dist.to_path_buf()));
        }
        let empty = std::fs::read_dir(dist)
            .map_err(|source| ExportError::Read {
                path: dist.to_path_buf(),
                source,
            })?
            .next()
            .is_none();
        let ours = std::fs::read_to_string(dist.join(MARKER))
            .is_ok_and(|contents| contents == MARKER_MAGIC);
        if !empty && !ours {
            return Err(ExportError::NotOurs(dist.to_path_buf()));
        }
        std::fs::remove_dir_all(dist).map_err(|source| ExportError::Write {
            path: dist.to_path_buf(),
            source,
        })?;
    }
    std::fs::create_dir_all(dist).map_err(|source| ExportError::Write {
        path: dist.to_path_buf(),
        source,
    })?;
    write(&dist.join(MARKER), MARKER_MAGIC.as_bytes())
}

/// The stand-in for a 308 on a host that cannot send one.
///
/// A meta refresh with a zero delay, *plus* a canonical link, *plus* a visible
/// link. The refresh moves the browser; the canonical tells a crawler which URL
/// is the real one, which is the part a 308 was doing that a refresh does not;
/// the visible link is what is left for anyone whose browser honours neither.
/// `noindex` keeps the stub itself out of results — the redirect exists to
/// retire a URL, not to publish a new empty page at it.
fn redirect_stub(to: &str) -> Markup {
    html! {
        (maud::DOCTYPE)
        html lang="en" {
            head {
                meta charset="utf-8";
                meta http-equiv="refresh" content=(format!("0; url={to}"));
                meta name="robots" content="noindex";
                link rel="canonical" href=(to);
                title { "Moved — kmosoti" }
            }
            body {
                p { "This page has moved. " a href=(to) { "Continue to the index." } }
            }
        }
    }
}

/// The 404 page, in the site's own shell.
///
/// A file host's default 404 is a white page with a GitHub logo on it, which
/// tells a visitor they have left the site when they have not. This one keeps
/// the nav, so the answer to "wrong URL" is one click away.
pub fn not_found(state: &AppState) -> Markup {
    shell(
        state,
        PageMeta {
            title: "Not found",
            description: "That page is not here.",
            // No nav entry is current: this is not one of the pages.
            route: "",
        },
        html! {
            article class="my-xl" {
                h1 class="type-2xl" { "Not found" }
                p class="type-md" {
                    "There is no page at that address. There may never have been — this site "
                    "is two pages, and it says so on both of them."
                }
                p class="text-muted" {
                    "Try the " a href="/" { "index" } ", or the " a href="/about" { "about page" } "."
                }
            }
        },
    )
}

/// True for a toolchain or documentation artifact that lives under `assets/`
/// for the benefit of whoever next works on the islands crate, but that no
/// page or script ever fetches: its package manifest, its generated
/// TypeScript types, and its README. `motion.json` is in the same bucket for
/// a different reason — it is a *server-side* boot input read by `AppState`
/// (see `state::MOTION_JSON`), not a browser asset, so it has no business in
/// a tree nothing but a static file server will ever read. `dist/` is a
/// runtime tree, not a mirror of `assets/`, and this is the boundary between
/// the two.
///
/// Takes a file *name*, not a path, so the same name is caught wherever it
/// recurs under `assets/`. The `.d.ts` case is a suffix test rather than a
/// `Path::extension()` comparison because `Path::extension()` of `foo.d.ts`
/// returns `ts`, not `d.ts` — an extension-based match would silently miss
/// every one of these files.
fn is_dev_artifact(name: &str) -> bool {
    name == "README.md"
        || name == "package.json"
        || name == crate::state::MOTION_JSON
        || name.ends_with(".d.ts")
}

/// Copy `assets/` into `dist/assets/`, returning the count of files actually
/// copied — [`is_dev_artifact`] files are walked but skipped, so this is not
/// simply the size of `assets/`.
///
/// Every copied entry is checked for containment on the way, even though
/// `AppState` already refused to boot on an escaping symlink. That check ran
/// when the process started; this one runs at the moment the bytes are read,
/// and a symlink is precisely the thing that can be repointed in between. The
/// server learned this the same way — see `server::static_asset`, which
/// re-establishes containment per request for exactly this reason.
fn copy_assets(from: &Path, to: &Path) -> Result<usize, ExportError> {
    // Canonicalised once, up front: this is the tree every opened file must
    // prove it still lives in at the moment it is read.
    let root = from.canonicalize().map_err(|source| ExportError::Read {
        path: from.to_path_buf(),
        source,
    })?;
    let files = walk(from)?;
    let mut copied = 0usize;
    for file in &files {
        let name = file.file_name().and_then(|n| n.to_str()).unwrap_or("");
        if is_dev_artifact(name) {
            // Skipped, not merely uncopied: a skipped entry never reaches
            // `open_plain_file` below, so it is worth the one extra stat call
            // to keep this loop's own promise — a symlink is refused, never
            // silently waved through — for the handful of names that leave
            // the loop early. Not folded into `open_plain_file` itself,
            // because that function's job is opening a file this exporter
            // *will* publish; these never will.
            let is_symlink = std::fs::symlink_metadata(file)
                .map(|meta| meta.file_type().is_symlink())
                .unwrap_or(false);
            if is_symlink {
                return Err(ExportError::AssetSymlink { link: file.clone() });
            }
            continue;
        }

        // `strip_prefix` on the `Path`, not on a lossy string: a filename may
        // legitimately contain a backslash on Unix, and stringifying first
        // turned one file into two nested directories.
        let suffix = file.strip_prefix(from).unwrap_or(file);
        let destination = to.join(suffix);

        let mut source = open_plain_file(file, &root)?;
        if let Some(parent) = destination.parent() {
            std::fs::create_dir_all(parent).map_err(|source| ExportError::Write {
                path: parent.to_path_buf(),
                source,
            })?;
        }
        let mut sink =
            std::fs::File::create(&destination).map_err(|source| ExportError::Write {
                path: destination.clone(),
                source,
            })?;
        std::io::copy(&mut source, &mut sink).map_err(|source| ExportError::Write {
            path: destination.clone(),
            source,
        })?;
        copied += 1;
    }
    Ok(copied)
}

/// Open one asset, having established that it is a plain file and that the
/// handle refers to the very file that was checked.
///
/// The order matters and is the whole point. `symlink_metadata` names what is
/// at the path *now*; `File::open` opens what is at the path *then*; comparing
/// the two identities afterwards is what makes the gap between them harmless.
/// Checking first and calling `fs::copy` second — which reopens by path — would
/// leave exactly that gap, and a link swapped into it publishes any file this
/// process can read.
fn open_plain_file(path: &Path, root: &Path) -> Result<std::fs::File, ExportError> {
    let before = std::fs::symlink_metadata(path).map_err(|source| ExportError::Read {
        path: path.to_path_buf(),
        source,
    })?;
    if before.file_type().is_symlink() {
        return Err(ExportError::AssetSymlink {
            link: path.to_path_buf(),
        });
    }

    let file = std::fs::File::open(path).map_err(|source| ExportError::Read {
        path: path.to_path_buf(),
        source,
    })?;
    let after = file.metadata().map_err(|source| ExportError::Read {
        path: path.to_path_buf(),
        source,
    })?;
    if !same_file(&before, &after) {
        return Err(ExportError::AssetChanged {
            path: path.to_path_buf(),
        });
    }

    // The identity check above pins the *leaf*; this pins the *route to it*. A
    // parent directory swapped for a symlink after the walk hands both `before`
    // and `after` the same out-of-tree inode, in agreement with each other, so
    // the question "where does this open file actually live" has to be put to
    // the handle rather than to the path that reached it.
    let resolved = opened_location(&file, path)?;
    if !resolved.starts_with(root) {
        return Err(ExportError::AssetOutsideTree {
            path: path.to_path_buf(),
            resolved,
        });
    }
    Ok(file)
}

/// Where an open file actually is, answered by the handle.
///
/// On Linux this is read from the descriptor itself — `/proc/self/fd/N` names
/// the file the handle holds, however the path that opened it has been rewired
/// since. Off Linux there is no handle-based answer in std, and canonicalising
/// the spelled path was the first version here — it re-walks the directories
/// by name, so a parent swapped out for the open and back in for the check
/// passes containment while the descriptor still holds the outside file. A
/// gate that cannot ask its question fails closed instead: the deploy path
/// this exporter exists for is Linux CI, and an export on another OS refusing
/// to certify assets is better than one certifying what it cannot see.
#[cfg(target_os = "linux")]
fn opened_location(file: &std::fs::File, path: &Path) -> Result<PathBuf, ExportError> {
    use std::os::fd::AsRawFd;
    std::fs::read_link(format!("/proc/self/fd/{}", file.as_raw_fd())).map_err(|source| {
        ExportError::Read {
            path: path.to_path_buf(),
            source,
        }
    })
}

#[cfg(not(target_os = "linux"))]
fn opened_location(_file: &std::fs::File, path: &Path) -> Result<PathBuf, ExportError> {
    Err(ExportError::Read {
        path: path.to_path_buf(),
        source: std::io::Error::other(
            "asset containment is verified through the file descriptor, which needs Linux \
             (/proc/self/fd); refusing to publish assets this gate cannot check",
        ),
    })
}

/// Whether two `Metadata` describe the same file.
#[cfg(unix)]
fn same_file(before: &std::fs::Metadata, after: &std::fs::Metadata) -> bool {
    use std::os::unix::fs::MetadataExt;
    (before.dev(), before.ino()) == (after.dev(), after.ino())
}

/// Off Unix there is no cheap stable identity, so fall back to the properties
/// that would have to be forged together. Weaker, and said so rather than
/// implied: the deploy path this exporter exists for is Linux CI.
#[cfg(not(unix))]
fn same_file(before: &std::fs::Metadata, after: &std::fs::Metadata) -> bool {
    before.file_type() == after.file_type()
        && before.len() == after.len()
        && before.modified().ok() == after.modified().ok()
}

/// The résumé arrived intact, or the export fails.
///
/// Hashes rather than lengths: a copy that stops halfway usually has the wrong
/// length, but a copy that goes through a text-mode filter or a line-ending
/// rewrite does not, and that is the one that produces a PDF a browser opens to
/// a grey rectangle.
fn verify_resume(source_dir: &Path, target_dir: &Path) -> Result<(), ExportError> {
    let source = source_dir.join(RESUME_PDF);
    let copy = target_dir.join(RESUME_PDF);
    let source_hash = sha256_file(&source)?;
    let copy_hash = sha256_file(&copy)?;
    if source_hash != copy_hash {
        return Err(ExportError::AssetCorrupted {
            name: RESUME_PDF.to_owned(),
            source_hash,
            copy_hash,
        });
    }
    Ok(())
}

/// SHA-256 of a file, hex. Public so a deploy gate can restate the assertion
/// against whatever it actually uploaded.
pub fn sha256_file(path: &Path) -> Result<String, ExportError> {
    let bytes = std::fs::read(path).map_err(|source| ExportError::Read {
        path: path.to_path_buf(),
        source,
    })?;
    let mut hasher = Sha256::new();
    hasher.update(&bytes);
    Ok(format!("{:x}", hasher.finalize()))
}

/// One short hex string naming everything in `dist/`.
///
/// The service worker's cache is named after this, which is the whole reason it
/// exists: the cache is discarded when the name changes, and the name changes
/// with the build rather than with somebody remembering to bump a number. Paths
/// are fed in alongside their hashes, so a file renamed with its bytes intact —
/// which is a different site to a browser resolving URLs — is a different
/// fingerprint. [`walk`] returns a sorted list, so the answer does not depend on
/// the order the filesystem happened to hand entries back.
///
/// **This is not a reproducible-build hash, and should not be read as one.**
/// Every page carries a `data-span-id` minted per render, so two exports of an
/// unchanged tree fingerprint differently and a no-op redeploy costs returning
/// visitors the fonts and the wasm bundle again. That is the accepted price: a
/// version that is *never* wrong is worth more here than one that is sometimes
/// cheaper, because the failure it rules out — a new build served out of an old
/// cache — is silent, and the failure it accepts is a download.
///
/// Sixteen hex characters, not sixty-four: this names a build to a cache on one
/// origin, and nothing decides anything on the strength of it being hard to
/// forge.
pub fn site_fingerprint(dist: &Path) -> Result<String, ExportError> {
    let mut hasher = Sha256::new();
    for file in walk(dist)? {
        hasher.update(relative(dist, &file).as_bytes());
        hasher.update(b"\0");
        hasher.update(sha256_file(&file)?.as_bytes());
        hasher.update(b"\n");
    }
    Ok(format!("{:x}", hasher.finalize())[..16].to_owned())
}

/// Every internal `href` and `src` in the exported HTML resolves to a file.
///
/// Returns the number of internal references checked, which is worth printing:
/// a link check that silently found nothing to check is indistinguishable from
/// one that passed.
pub fn link_check(dist: &Path) -> Result<usize, ExportError> {
    let mut checked = 0;
    let mut broken = Vec::new();

    for page in walk(dist)? {
        if page.extension().and_then(|ext| ext.to_str()) != Some("html") {
            continue;
        }
        let source = std::fs::read_to_string(&page).map_err(|source| ExportError::Read {
            path: page.clone(),
            source,
        })?;
        for (attribute, reference) in references(&source) {
            let here = relative(dist, &page);
            // A write is broken on this site no matter where it points. Inside
            // the site — including a bare `#x` or `?x`, which address the
            // current document — the host serves GET and nothing else. And a
            // genuinely cross-origin write is equally dead: the bundled htmx
            // ships `selfRequestsOnly: true` and no page overrides it, so the
            // request is refused before it is sent. An allowlist for "another
            // origin might answer the POST" certified controls the runtime
            // itself declines; there is no reference a write attribute can
            // carry that works in production.
            if WRITE_ATTRIBUTES.contains(&attribute) {
                checked += 1;
                broken.push(format!(
                    "  {here} → {attribute}=\"{reference}\" is a write, and this site cannot \
                     make one: the host answers GET only, and the bundled htmx refuses \
                     cross-origin requests (selfRequestsOnly) — the control cannot work in \
                     production wherever it points"
                ));
                continue;
            }
            let target = resolve(dist, &page, &reference);
            match target {
                Target::External => continue,
                Target::Escapes(target) => {
                    checked += 1;
                    broken.push(format!(
                        "  {here} → {attribute}=\"{reference}\" climbs out of the export root \
                         (to {}), so it can only ever resolve on this machine",
                        target.display()
                    ));
                }
                Target::Inside(candidates) => {
                    checked += 1;
                    if !candidates.iter().any(|candidate| candidate.is_file()) {
                        broken.push(format!(
                            "  {here} → {attribute}=\"{reference}\" (tried {})",
                            candidates
                                .iter()
                                .map(|candidate| relative(dist, candidate))
                                .collect::<Vec<_>>()
                                .join(", ")
                        ));
                    }
                }
            }
        }
    }

    if !broken.is_empty() {
        broken.sort();
        broken.dedup();
        return Err(ExportError::BrokenLinks {
            count: broken.len(),
            details: broken.join("\n"),
        });
    }
    Ok(checked)
}

/// Attributes naming something a file host can answer: a GET for a file.
///
/// `hx-get` is here for a reason that is easy to miss: it is not a link, so no
/// link checker looks at it, and a static host cannot serve
/// `/fragments/dispatch` at all. A page exported with a swap button on it would
/// pass every check and then present a visitor with a control that silently
/// does nothing.
///
/// `srcset` is deliberately absent: its value is a comma-separated list with
/// width descriptors, not a URL, and nothing on this site emits one. `action`
/// is treated as a GET, which a bare `<form action>` is; a `method="post"` on
/// the same element is *not* caught, because that needs paired attributes.
/// Both omissions are recorded here rather than left to be discovered.
const URL_ATTRIBUTES: [&str; 5] = ["href", "src", "action", "poster", "hx-get"];

/// Attributes that name a *write*.
///
/// Every one of these fails when it points inside the site, and not because the
/// target is missing — because GitHub Pages answers exactly one method.
/// `hx-post="/"` resolves to `index.html` and would sail past a checker that
/// only asked whether the target exists, while the control it sits on can never
/// do anything in production.
const WRITE_ATTRIBUTES: [&str; 4] = ["hx-post", "hx-put", "hx-patch", "hx-delete"];

/// Every URL-valued attribute in a document, as `(attribute, value)`.
///
/// A substring scan rather than an HTML parser, deliberately. The input is not
/// arbitrary HTML — it is maud output, where every attribute is quoted with `"`
/// and every value is escaped — so the two failure modes a parser would buy
/// (unquoted attributes, `>` inside a value) cannot occur, and a parser
/// dependency would be a third-party crate in the trust path of a gate.
fn references(document: &str) -> Vec<(&'static str, String)> {
    let mut out = Vec::new();
    for attribute in URL_ATTRIBUTES.into_iter().chain(WRITE_ATTRIBUTES) {
        let needle = format!("{attribute}=\"");
        for (index, _) in document.match_indices(&needle) {
            let rest = &document[index + needle.len()..];
            let Some(end) = rest.find('"') else { continue };
            out.push((attribute, rest[..end].to_owned()));
        }
    }
    out
}

/// The files a reference could legitimately be satisfied by, or `None` if it
/// leaves the site.
///
/// More than one candidate because a file host resolves `/about` by looking for
/// `about/index.html` *and* `about.html`, and this exporter should not be the
/// thing that decides a link is broken because it picked the other convention.
#[derive(Debug)]
enum Target {
    /// Somebody else's problem: another origin, a mail address, an anchor.
    External,
    /// Any one of these files would satisfy the reference.
    Inside(Vec<PathBuf>),
    /// The reference climbs out of `dist/` with `..`.
    ///
    /// Its own case rather than "not found", because the file it names may well
    /// exist on this machine — sitting next to `dist/`, never uploaded — and
    /// then a checker that merely tested `is_file()` would certify a link that
    /// 404s the moment the site is somewhere else.
    Escapes(PathBuf),
}

fn resolve(dist: &Path, page: &Path, reference: &str) -> Target {
    let external = ["http://", "https://", "//", "mailto:", "tel:", "data:", "#"];
    if reference.is_empty() || external.iter().any(|prefix| reference.starts_with(prefix)) {
        return Target::External;
    }

    // A query string on a static host is decoration on the path; the fragment
    // is the browser's business. Neither changes which file is fetched.
    let path = reference
        .split(['#', '?'])
        .next()
        .unwrap_or_default()
        .trim();
    if path.is_empty() {
        return Target::External;
    }

    let joined = if let Some(rooted) = path.strip_prefix('/') {
        dist.join(rooted)
    } else {
        page.parent().unwrap_or(dist).join(path)
    };

    // Lexically, not through the filesystem: `..` in a URL is resolved by the
    // browser before the request is sent, so the question is what the *host*
    // will be asked for, not what this machine happens to have symlinked.
    let base = normalize(&joined);
    let root = normalize(dist);
    if !base.starts_with(&root) {
        return Target::Escapes(base);
    }

    if path.ends_with('/') || path == "/" {
        return Target::Inside(vec![base.join("index.html")]);
    }

    let mut candidates = vec![base.clone(), base.join("index.html")];
    // Only for an extensionless URL. `with_extension` *replaces*, so applying
    // it to `/feed.json` asks whether `feed.html` exists — and a missing feed
    // would then be certified by an unrelated page.
    if base.extension().is_none() {
        candidates.push(base.with_extension("html"));
    }
    Target::Inside(candidates)
}

/// Resolve `.` and `..` without touching the filesystem.
fn normalize(path: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for part in path.components() {
        match part {
            std::path::Component::CurDir => {}
            std::path::Component::ParentDir => {
                out.pop();
            }
            other => out.push(other),
        }
    }
    out
}

/// Every file under `root`, recursively. Empty if `root` is missing.
///
/// Descends into real directories only. `DirEntry::file_type` does not follow
/// symlinks, so a link to a directory is collected as a leaf rather than walked
/// into — which is what stops `assets/loop -> .` from recursing until the
/// process runs out of path. A symlinked file is still copied (through its
/// target, having been checked for containment in [`copy_assets`]); it is the
/// *traversal* that must not follow, because traversal is where a cycle lives.
fn walk(root: &Path) -> Result<Vec<PathBuf>, ExportError> {
    let mut files = Vec::new();
    let mut stack = vec![root.to_path_buf()];
    while let Some(dir) = stack.pop() {
        let entries = match std::fs::read_dir(&dir) {
            Ok(entries) => entries,
            Err(source) if source.kind() == std::io::ErrorKind::NotFound => continue,
            Err(source) => return Err(ExportError::Read { path: dir, source }),
        };
        for entry in entries {
            let entry = entry.map_err(|source| ExportError::Read {
                path: dir.clone(),
                source,
            })?;
            let path = entry.path();
            let real_dir = entry.file_type().map(|kind| kind.is_dir()).unwrap_or(false);
            if real_dir {
                stack.push(path);
            } else {
                files.push(path);
            }
        }
    }
    files.sort();
    Ok(files)
}

/// `path` relative to `root`, with forward slashes, for printing.
fn relative(root: &Path, path: &Path) -> String {
    path.strip_prefix(root)
        .unwrap_or(path)
        .to_string_lossy()
        .replace('\\', "/")
}

fn write(path: &Path, bytes: &[u8]) -> Result<(), ExportError> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|source| ExportError::Write {
            path: parent.to_path_buf(),
            source,
        })?;
    }
    std::fs::write(path, bytes).map_err(|source| ExportError::Write {
        path: path.to_path_buf(),
        source,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::span::new_span_id;

    /// One export into a scratch directory, cleaned up afterwards.
    ///
    /// Not `site/dist`: a test that writes there races the developer's own
    /// export and, worse, would pass because of files a previous run left.
    struct Scratch(PathBuf);

    impl Scratch {
        fn new() -> Self {
            Self(std::env::temp_dir().join(format!("ui-servo-export-{}", new_span_id())))
        }

        fn path(&self) -> &Path {
            &self.0
        }
    }

    impl Drop for Scratch {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    fn exported() -> (Scratch, Report) {
        let scratch = Scratch::new();
        let report = export(&AppState::for_tests(false), scratch.path()).expect("the export runs");
        (scratch, report)
    }

    fn read(dist: &Path, relative: &str) -> String {
        std::fs::read_to_string(dist.join(relative))
            .unwrap_or_else(|error| panic!("{relative} is not in dist: {error}"))
    }

    /// Every page route in the manifest arrives as a document at the path a
    /// file host will look for.
    #[test]
    fn every_manifest_route_lands_on_a_file() {
        let (scratch, _) = exported();
        for route in routes::ROUTES {
            match (route.kind, route.output_path()) {
                (RouteKind::Page { .. }, Some(relative)) => {
                    let body = read(scratch.path(), &relative);
                    assert!(
                        body.starts_with("<!DOCTYPE html>"),
                        "{relative} is not a document"
                    );
                    assert!(body.contains("data-span-id=\""), "{relative}");
                }
                (RouteKind::Redirect { to }, Some(relative)) => {
                    let body = read(scratch.path(), &relative);
                    assert!(
                        body.contains(&format!("content=\"0; url={to}\"")),
                        "{relative} is not a redirect stub: {body:.200}"
                    );
                }
                (_, None) => {}
                (kind, Some(relative)) => panic!("{kind:?} wrote {relative}"),
            }
        }
    }

    /// The exported document is the one the server serves.
    ///
    /// Span ids differ per render by design, so they are masked out rather than
    /// compared — everything else has to match, because the whole claim of this
    /// module is that the static site is not a second implementation.
    #[tokio::test]
    async fn the_exported_home_page_is_the_served_home_page() {
        use axum::body::Body;
        use axum::http::Request;
        use tower::ServiceExt;

        let (scratch, _) = exported();
        let served = crate::server::app(AppState::for_tests(false))
            .oneshot(
                Request::builder()
                    .uri("/")
                    .body(Body::empty())
                    .expect("valid request"),
            )
            .await
            .expect("router is infallible");
        let served = axum::body::to_bytes(served.into_body(), usize::MAX)
            .await
            .expect("body collects");
        let served = String::from_utf8_lossy(&served).into_owned();

        assert_eq!(
            mask_span_ids(&read(scratch.path(), "index.html")),
            mask_span_ids(&served)
        );
    }

    /// Replace every `data-span-id="…"` value with a fixed string.
    fn mask_span_ids(document: &str) -> String {
        let mut out = String::with_capacity(document.len());
        let mut rest = document;
        while let Some(index) = rest.find("data-span-id=\"") {
            let (before, after) = rest.split_at(index + "data-span-id=\"".len());
            out.push_str(before);
            out.push_str("SPAN");
            let end = after.find('"').expect("span id is quoted");
            rest = &after[end..];
        }
        out.push_str(rest);
        out
    }

    /// The résumé is the one asset where a silent corruption is plausible and
    /// invisible: a broken PDF still has a size and still returns 200.
    #[test]
    fn the_resume_pdf_is_byte_identical() {
        let (scratch, _) = exported();
        let source = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("assets")
            .join(RESUME_PDF);
        let copy = scratch.path().join(ASSETS_DIR).join(RESUME_PDF);
        assert_eq!(
            std::fs::read(&source).expect("the committed résumé"),
            std::fs::read(&copy).expect("the exported résumé"),
            "the exported résumé is not the committed one"
        );
        assert_eq!(
            sha256_file(&source).expect("hashable"),
            sha256_file(&copy).expect("hashable")
        );
    }

    #[test]
    fn the_pages_scaffolding_is_present() {
        let (scratch, report) = exported();
        assert_eq!(read(scratch.path(), "CNAME").trim_end(), CNAME);
        assert!(scratch.path().join(NOJEKYLL).is_file(), "no {NOJEKYLL}");
        assert_eq!(
            std::fs::metadata(scratch.path().join(NOJEKYLL))
                .expect("stat")
                .len(),
            0,
            "{NOJEKYLL} is supposed to be empty"
        );
        assert_eq!(read(scratch.path(), MARKER), MARKER_MAGIC);

        // The 404 wears the site's shell rather than the host's default.
        let not_found = read(scratch.path(), "404.html");
        assert!(not_found.starts_with("<!DOCTYPE html>"));
        assert!(not_found.contains("/assets/site.css"), "unstyled 404");
        assert!(
            not_found.contains("aria-label=\"Primary\""),
            "no nav on 404"
        );

        assert!(report.assets > 0, "no assets copied");
        assert!(report.links > 0, "the link check found nothing to check");
    }

    /// The URLs in the worker's `PRECACHE` array, read out of the template.
    ///
    /// Parsed from the array rather than searched for anywhere in the file: the
    /// header comment names several of these paths while explaining what is
    /// *not* precached, and a `contains` over the whole template would happily
    /// certify a page that only appears in prose.
    fn precache_urls() -> Vec<String> {
        let (_, rest) = SW_TEMPLATE
            .split_once("var PRECACHE = [")
            .expect("sw.js declares a PRECACHE array");
        let (list, _) = rest.split_once(']').expect("the array is closed");
        list.split(',')
            .map(str::trim)
            .filter(|entry| !entry.is_empty())
            .map(|entry| {
                entry
                    .strip_prefix('\'')
                    .and_then(|entry| entry.strip_suffix('\''))
                    .unwrap_or_else(|| panic!("{entry:?} is not a single-quoted URL"))
                    .to_owned()
            })
            .collect()
    }

    /// Every page the site publishes is in the worker's precache list, and the
    /// exported worker is stamped with a version.
    ///
    /// The first half is the one that decays. A page added to the manifest is
    /// exported, linked, and link-checked automatically — and would be silently
    /// absent from the offline site, because the precache list is a literal in
    /// a `.js` file that nothing else reads. So the list is compared against
    /// `ROUTES` here, in the URL form a browser asks for, which is not the
    /// `about/index.html` form the exporter writes.
    ///
    /// The second half is the stamp: an unstamped worker would name its cache
    /// `ui-servo-__UI_SERVO_SW_VERSION__` and every deploy would share it,
    /// which is exactly the eviction bug this design exists to avoid.
    #[test]
    fn the_service_worker_is_stamped_and_precaches_every_page() {
        let precached = precache_urls();
        for route in routes::ROUTES {
            let Some(output) = route.output_path() else {
                continue;
            };
            let url = match output.strip_suffix("index.html") {
                Some(prefix) => format!("/{}", prefix.trim_end_matches('/')),
                None => format!("/{output}"),
            };
            assert!(
                precached.contains(&url),
                "{} exports to {output}, which the service worker does not precache — add \
                 {url:?} to PRECACHE in src/sw.js, or the offline site is missing a page",
                route.path
            );
        }

        let (scratch, _) = exported();

        // `cache.addAll` is all-or-nothing: one URL that 404s rejects the whole
        // install and the site ships with no offline support at all, which
        // looks exactly like a site that was never given any. Nothing else
        // notices — `link_check` reads `.html` only, and these are string
        // literals in a `.js` file — so the assets are resolved here.
        for url in &precached {
            let Some(relative) = url.strip_prefix("/assets/") else {
                continue;
            };
            assert!(
                scratch.path().join(ASSETS_DIR).join(relative).is_file(),
                "the service worker precaches {url}, which the export does not write — the \
                 install would reject and the whole cache would be empty"
            );
        }

        let worker = read(scratch.path(), SW_FILE);
        assert!(
            !worker.contains(SW_VERSION_PLACEHOLDER),
            "the exported worker is unstamped"
        );
        assert!(
            worker.contains("ui-servo-"),
            "the exported worker names no cache"
        );
    }

    /// Nothing in the export carries the sensor runtime. A static page that
    /// booted the probe would beacon from a stranger's browser at a host with
    /// no ingest behind it.
    #[test]
    fn the_export_ships_no_sensors() {
        let (scratch, report) = exported();
        for page in report.pages {
            let body = read(scratch.path(), &page);
            assert!(
                !body.contains("__UI_SERVO__"),
                "{page} carries probe config"
            );
            assert!(!body.contains("probe.js"), "{page} loads the probe");
        }
    }

    /// `assets/` carries files for the benefit of whoever next works on the
    /// islands crate — its README, its `package.json`, its generated `.d.ts`
    /// types — and, separately, `motion.json`, a server-side boot input read
    /// by `AppState` rather than by any page. None of the five are linked
    /// from a page, fetched by a script, or belong in a tree a file host
    /// serves verbatim to strangers. The runtime files that *are* fetched
    /// must still make it across, so this checks both sides of the cut.
    #[test]
    fn the_export_ships_no_dev_artifacts() {
        let (scratch, report) = exported();
        let assets_dir = scratch.path().join(ASSETS_DIR);
        let copied = walk(&assets_dir).expect("the exported assets tree walks");

        // The four names actually committed under `site/assets/` today — a
        // regression here means the real files, not just a hypothetical
        // name, made it into the export.
        for pruned in [
            "README.md",
            "package.json",
            "motion.json",
            "ui_servo_islands.d.ts",
            "ui_servo_islands_bg.wasm.d.ts",
        ] {
            assert!(
                !copied
                    .iter()
                    .any(|file| file.file_name().and_then(|n| n.to_str()) == Some(pruned)),
                "{pruned} shipped in the export"
            );
        }

        // The predicate itself, walked against the exported tree: this is
        // what keeps this test in sync with `copy_assets` if the skip list
        // ever grows or narrows, rather than re-deriving its own copy of it.
        for file in &copied {
            let name = file.file_name().and_then(|n| n.to_str()).unwrap_or("");
            assert!(
                !is_dev_artifact(name),
                "{} shipped in the export",
                file.display()
            );
        }

        for runtime_file in [
            "islands/ui_servo_islands_bg.wasm",
            "islands/ui_servo_islands.js",
            "islands/loader.js",
            "fonts/fonts.css",
        ] {
            assert!(
                assets_dir.join(runtime_file).is_file(),
                "{runtime_file} is missing from the export"
            );
        }

        assert!(report.links > 0, "the link check found nothing to check");
    }

    /// A file named like a pruned dev artifact but that is actually a symlink
    /// is refused loudly, the same as any other symlink in `assets/` — being
    /// skipped from the copy must not also mean being waved through
    /// unchecked. See `is_dev_artifact` and the skip in `copy_assets`.
    #[test]
    fn a_symlink_named_like_a_pruned_artifact_is_refused() {
        let scratch = Scratch::new();
        let assets = scratch.path().join("assets");
        let outside = scratch.path().join("outside.txt");
        std::fs::create_dir_all(&assets).expect("scratch is creatable");
        write(&outside, b"not for publication").expect("scratch is writable");
        std::os::unix::fs::symlink(&outside, assets.join("motion.json")).expect("symlink");

        match copy_assets(&assets, &scratch.path().join("out")) {
            Err(ExportError::AssetSymlink { link }) => {
                assert_eq!(link, assets.join("motion.json"))
            }
            other => panic!("a symlink named motion.json was not refused: {other:?}"),
        }
        assert!(
            !scratch.path().join("out").join("motion.json").exists(),
            "the symlinked file reached the export"
        );
    }

    /// The link check is the gate, so it has to be able to fail.
    #[test]
    fn the_link_check_catches_a_dangling_reference() {
        let (scratch, _) = exported();
        write(
            &scratch.path().join("broken/index.html"),
            b"<a href=\"/nowhere\">gone</a>",
        )
        .expect("scratch is writable");

        match link_check(scratch.path()) {
            Err(ExportError::BrokenLinks { count, details }) => {
                assert_eq!(count, 1, "{details}");
                assert!(details.contains("/nowhere"), "{details}");
            }
            other => panic!("the link check passed a dangling reference: {other:?}"),
        }
    }

    /// An htmx swap against a fragment endpoint fails the gate.
    ///
    /// Nothing on the site does this today, and that is the point: a fragment
    /// slot added to a page later is exactly the change that would ship a dead
    /// button to mosoti.dev without a single link checker noticing, because
    /// `hx-get` is not a link.
    #[test]
    fn the_link_check_catches_an_htmx_swap_a_file_host_cannot_serve() {
        let (scratch, _) = exported();
        write(
            &scratch.path().join("swap/index.html"),
            b"<button hx-get=\"/fragments/dispatch\">again</button>",
        )
        .expect("scratch is writable");

        match link_check(scratch.path()) {
            Err(ExportError::BrokenLinks { count, details }) => {
                assert_eq!(count, 1, "{details}");
                assert!(
                    details.contains("hx-get=\"/fragments/dispatch\""),
                    "{details}"
                );
            }
            other => panic!("the link check passed an unservable swap: {other:?}"),
        }
    }

    /// A relative reference resolves against the page that made it, not the
    /// root. Nothing in the site emits one today, which is exactly why this is
    /// asserted rather than assumed.
    #[test]
    fn a_relative_reference_resolves_against_its_page() {
        let dist = Path::new("/dist");
        let page = Path::new("/dist/about/index.html");
        match resolve(dist, page, "sibling.html") {
            Target::Inside(candidates) => assert!(
                candidates.contains(&PathBuf::from("/dist/about/sibling.html")),
                "{candidates:?}"
            ),
            other => panic!("{other:?}"),
        }
    }

    /// A reference that climbs out of the export root is broken even when the
    /// file it names exists — it exists *here*, and here is not the web.
    #[test]
    fn a_reference_that_escapes_the_export_root_is_broken() {
        let dist = Path::new("/dist");
        let page = Path::new("/dist/about/index.html");
        for reference in ["../../secret.html", "/../secret.html"] {
            assert!(
                matches!(resolve(dist, page, reference), Target::Escapes(_)),
                "{reference:?} was not caught climbing out of the root"
            );
        }
        // …while a `..` that stays inside is ordinary and must still resolve.
        assert!(matches!(
            resolve(dist, page, "../index.html"),
            Target::Inside(_)
        ));
    }

    /// The `.html` fallback is for extensionless URLs only. `with_extension`
    /// *replaces*, so applied to `/feed.json` it would ask about `feed.html`
    /// and let a missing feed through on the strength of an unrelated page.
    #[test]
    fn the_html_fallback_does_not_rewrite_an_existing_extension() {
        let dist = Path::new("/dist");
        let page = Path::new("/dist/index.html");
        match resolve(dist, page, "/feed.json") {
            Target::Inside(candidates) => assert!(
                !candidates.contains(&PathBuf::from("/dist/feed.html")),
                "{candidates:?}"
            ),
            other => panic!("{other:?}"),
        }
    }

    #[test]
    fn external_and_in_page_references_are_left_alone() {
        let dist = Path::new("/dist");
        let page = Path::new("/dist/index.html");
        for reference in [
            "https://github.com/kmosoti",
            "mailto:kennedy.rmosoti@gmail.com",
            "#main",
            "",
        ] {
            assert!(
                matches!(resolve(dist, page, reference), Target::External),
                "{reference:?} was treated as an internal path"
            );
        }
    }

    /// The export refuses to recursively delete a directory it did not write.
    #[test]
    fn an_unrecognised_output_directory_is_refused() {
        let scratch = Scratch::new();
        write(&scratch.path().join("someone-elses-work.txt"), b"important")
            .expect("scratch is writable");

        match export(&AppState::for_tests(false), scratch.path()) {
            Err(ExportError::NotOurs(path)) => assert_eq!(path, scratch.path()),
            other => panic!("the export was willing to delete a stranger's directory: {other:?}"),
        }
        assert!(scratch.path().join("someone-elses-work.txt").is_file());
    }

    /// A `.nojekyll` is not proof of ownership — every GitHub Pages directory
    /// in the world has one, and this exporter's first version accepted it as
    /// permission to `remove_dir_all` somebody else's published site.
    #[test]
    fn a_foreign_pages_directory_is_not_mistaken_for_ours() {
        let scratch = Scratch::new();
        write(&scratch.path().join(NOJEKYLL), b"").expect("scratch is writable");
        write(
            &scratch.path().join("index.html"),
            b"<p>somebody else's site</p>",
        )
        .expect("scratch is writable");
        // Right name, wrong content: a coincidence must not authorise a delete.
        write(&scratch.path().join(MARKER), b"not really").expect("scratch is writable");

        match export(&AppState::for_tests(false), scratch.path()) {
            Err(ExportError::NotOurs(_)) => {}
            other => panic!("a foreign Pages directory was accepted as ours: {other:?}"),
        }
        assert!(scratch.path().join("index.html").is_file());
    }

    /// A destination that overlaps an input is refused before anything is
    /// deleted. The reachable version of this is an empty argument, which is
    /// the working directory.
    ///
    /// `promoted/` is in the list for a failure that does not look like this
    /// one: exporting into it succeeds, and then the *server* refuses to start,
    /// because every file the export wrote there reads as a promoted fragment
    /// that cannot prove it was gated.
    #[test]
    fn a_destination_that_overlaps_an_input_is_refused() {
        let state = AppState::for_tests(false);
        let crate_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        for destination in [
            crate_root.clone(),                       // contains assets/
            crate_root.join("assets"),                // is assets/
            crate_root.join("assets").join("nested"), // inside assets/
            crate_root.join("promoted"),              // the promotion directory
            crate_root.join("promoted").join("out"),  // inside it
        ] {
            match export(&state, &destination) {
                Err(ExportError::UnsafeDestination { .. }) => {}
                other => panic!("{} was accepted: {other:?}", destination.display()),
            }
        }
        // The committed sources are untouched.
        assert!(crate_root.join("assets").join(RESUME_PDF).is_file());
        assert!(crate_root.join("promoted").is_dir());
    }

    /// The overlap check resolves symlinks rather than comparing strings.
    ///
    /// A link to the crate root spells a destination that shares no prefix with
    /// `assets/` and lands inside it anyway. Comparing the paths as written
    /// approves that, and the approval is a recursive delete of the site's own
    /// stylesheet, tokens and résumé.
    #[test]
    #[cfg(unix)]
    fn the_overlap_check_sees_through_a_symlinked_ancestor() {
        let scratch = Scratch::new();
        std::fs::create_dir_all(scratch.path()).expect("scratch is creatable");
        let alias = scratch.path().join("alias");
        std::os::unix::fs::symlink(env!("CARGO_MANIFEST_DIR"), &alias).expect("symlink");

        let destination = alias.join("assets").join("out");
        match export(&AppState::for_tests(false), &destination) {
            Err(ExportError::UnsafeDestination { .. }) => {}
            other => panic!("a symlinked path into assets/ was accepted: {other:?}"),
        }
        assert!(
            !destination.exists(),
            "the export created something inside assets/"
        );
    }

    /// A symlink in the asset tree is refused rather than followed.
    ///
    /// Both directions matter and only one of them is obvious. A link *out* of
    /// the tree would publish a file nobody meant to publish; a link *inside*
    /// it is refused too, because "check the target, then copy by path" is two
    /// operations with a gap between them, and the gap is the bug.
    #[test]
    #[cfg(unix)]
    fn a_symlink_in_the_assets_tree_is_not_copied() {
        let scratch = Scratch::new();
        let assets = scratch.path().join("assets");
        let outside = scratch.path().join("outside.txt");
        std::fs::create_dir_all(&assets).expect("scratch is creatable");
        write(&outside, b"not for publication").expect("scratch is writable");
        write(&assets.join("real.txt"), b"content").expect("scratch is writable");
        std::os::unix::fs::symlink(&outside, assets.join("leak.txt")).expect("symlink");

        match copy_assets(&assets, &scratch.path().join("out")) {
            Err(ExportError::AssetSymlink { link }) => {
                assert_eq!(link, assets.join("leak.txt"))
            }
            other => panic!("a symlink was copied into the published site: {other:?}"),
        }
        assert!(
            !scratch.path().join("out").join("leak.txt").exists(),
            "the escaping file reached the export"
        );
    }

    /// A symlink cycle inside `assets/` terminates instead of walking forever.
    #[test]
    #[cfg(unix)]
    fn a_symlink_cycle_does_not_walk_forever() {
        let scratch = Scratch::new();
        let assets = scratch.path().join("assets");
        std::fs::create_dir_all(&assets).expect("scratch is creatable");
        write(&assets.join("real.txt"), b"content").expect("scratch is writable");
        std::os::unix::fs::symlink(".", assets.join("loop")).expect("symlink");

        // The traversal does not follow the link, so this returns rather than
        // recursing until the process runs out of path.
        let files = walk(&assets).expect("the walk terminates");
        assert!(files.len() < 10, "the walk followed the cycle: {files:?}");
        // …and the link itself is then refused by the copy, so a directory
        // symlink can never become a confusing `fs::copy` failure either.
        assert!(matches!(
            copy_assets(&assets, &scratch.path().join("out")),
            Err(ExportError::AssetSymlink { .. })
        ));
    }

    /// A write aimed at the site fails the gate even though the path resolves.
    #[test]
    fn the_link_check_rejects_a_write_against_a_file_host() {
        let (scratch, _) = exported();
        write(
            &scratch.path().join("form/index.html"),
            b"<button hx-post=\"/\">save</button>",
        )
        .expect("scratch is writable");

        match link_check(scratch.path()) {
            Err(ExportError::BrokenLinks { count, details }) => {
                assert_eq!(count, 1, "{details}");
                assert!(details.contains("GET only"), "{details}");
            }
            other => panic!("a POST against a file host was certified: {other:?}"),
        }
    }

    /// A write that names no path at all still names the current document.
    ///
    /// `hx-post="#save"` and `hx-post="?save=1"` address the page they sit on
    /// — htmx POSTs to the current URL — and both used to pass the gate,
    /// because `resolve` files a bare fragment or query under "external". The
    /// write check no longer consults `resolve` at all.
    #[test]
    fn the_link_check_rejects_a_write_with_only_a_fragment_or_query() {
        let (scratch, _) = exported();
        write(
            &scratch.path().join("form/index.html"),
            b"<button hx-post=\"#save\">a</button><button hx-post=\"?save=1\">b</button>",
        )
        .expect("scratch is writable");

        match link_check(scratch.path()) {
            Err(ExportError::BrokenLinks { count, details }) => {
                assert_eq!(count, 2, "{details}");
            }
            other => panic!("a self-addressed POST was certified: {other:?}"),
        }
    }

    /// `..` in the not-yet-existing suffix cannot skip a symlinked ancestor.
    ///
    /// `alias/missing/../out` used to fall back to a lexical normalisation of
    /// the unresolved path — `file_name()` is `None` on a trailing `..` — so
    /// `alias` was never resolved and the overlap guard compared a spelling,
    /// not a place. The folded result must name the link's target.
    #[test]
    #[cfg(unix)]
    fn resolved_folds_dotdot_onto_the_canonical_base() {
        let scratch = Scratch::new();
        let real = scratch.path().join("real");
        std::fs::create_dir_all(&real).expect("scratch is creatable");
        std::os::unix::fs::symlink(&real, scratch.path().join("alias")).expect("symlink");

        let spelled = scratch.path().join("alias/missing/../out");
        let folded = resolved(&spelled);
        let canonical_real = real.canonicalize().expect("real exists");
        assert_eq!(
            folded,
            canonical_real.join("out"),
            "the symlinked ancestor was not resolved"
        );
        assert!(
            !folded
                .components()
                .any(|c| c == std::path::Component::ParentDir),
            "a literal .. survived into the resolved path: {folded:?}"
        );
    }

    /// Folding `..` can *expose* a symlink that one resolution pass then hands
    /// back raw: `scratch/missing/../alias/out` canonicalises only `scratch`
    /// (nothing deeper exists as spelled), cancels `missing`, and a
    /// single-pass version appended `alias` unresolved — approving a spelling
    /// whose real place is inside a protected input. Resolution must run to a
    /// fixpoint.
    #[test]
    #[cfg(unix)]
    fn resolved_resolves_a_symlink_exposed_by_folding() {
        let scratch = Scratch::new();
        let real = scratch.path().join("real");
        std::fs::create_dir_all(&real).expect("scratch is creatable");
        std::os::unix::fs::symlink(&real, scratch.path().join("alias")).expect("symlink");

        let spelled = scratch.path().join("missing/../alias/out");
        let folded = resolved(&spelled);
        let canonical_real = real.canonicalize().expect("real exists");
        assert_eq!(
            folded,
            canonical_real.join("out"),
            "the symlink exposed by the fold was not resolved"
        );
    }

    /// A regular file reached through an already-symlinked parent is refused,
    /// deterministically — no race needed. The walk never descends through a
    /// link, but `open_plain_file` must hold its own line: the descriptor's
    /// real location is outside the root it was asked to stay in.
    #[test]
    #[cfg(target_os = "linux")]
    fn a_file_reached_through_a_symlinked_parent_is_refused() {
        let scratch = Scratch::new();
        let root = scratch.path().join("assets");
        let outside = scratch.path().join("outside");
        std::fs::create_dir_all(&root).expect("scratch is creatable");
        std::fs::create_dir_all(&outside).expect("scratch is creatable");
        write(&outside.join("secret.txt"), b"not an asset").expect("scratch is writable");
        std::os::unix::fs::symlink(&outside, root.join("sub")).expect("symlink");

        let root = root.canonicalize().expect("root exists");
        match open_plain_file(&root.join("sub/secret.txt"), &root) {
            Err(ExportError::AssetOutsideTree { resolved, .. }) => {
                assert!(resolved.starts_with(outside.canonicalize().expect("outside exists")));
            }
            other => panic!("an out-of-tree file was certified: {other:?}"),
        }
    }

    /// A second export over the first one is a clean rebuild, not a merge: a
    /// page deleted from the manifest must disappear from `dist/`.
    #[test]
    fn re_exporting_clears_what_the_last_run_left() {
        let (scratch, _) = exported();
        write(&scratch.path().join("stale/index.html"), b"<p>old</p>")
            .expect("scratch is writable");
        export(&AppState::for_tests(false), scratch.path()).expect("the second export runs");
        assert!(
            !scratch.path().join("stale").exists(),
            "a stale page survived a rebuild"
        );
    }
}
