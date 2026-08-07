//! Startup configuration: where the assets live, what the motion contract
//! says, and whether this process is wearing sensors.
//!
//! Nothing in here is read per-request. The motion table and the colour scheme
//! are lifted off disk once, at boot, so that a page render is a pure function
//! of `AppState` and cannot drift halfway through a run.

use crate::error::StartupError;
use crate::fragments::promoted::{self, Promoted, PromotionError};
use serde_json::Value;
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;

/// The generated token sheet. Emitted by `ui_servo.domain.contract`.
pub const TOKENS_CSS: &str = "tokens.css";
/// The flattened motion table the probe compares `getAnimations()` against.
pub const MOTION_JSON: &str = "motion.json";

#[derive(Debug, Clone)]
pub struct AppState(Arc<Inner>);

#[derive(Debug)]
struct Inner {
    assets_dir: PathBuf,
    /// The parent of `promoted/`. Deliberately *not* `assets_dir`: see
    /// [`promoted::PROMOTED_DIR`].
    promoted_root: PathBuf,
    probe_path: Option<PathBuf>,
    dev: bool,
    beacon_url: String,
    turn_id: String,
    color_scheme: String,
    motion: Value,
    /// Promoted picks, verified once at boot. Empty in dev, where they are
    /// re-read per request instead. See [`AppState::promoted`].
    promoted: BTreeMap<String, Promoted>,
}

impl AppState {
    /// Read the environment and the generated artefacts, or refuse to start.
    pub fn from_env() -> Result<Self, StartupError> {
        let assets_dir = match std::env::var_os("UI_SERVO_ASSETS") {
            Some(dir) => PathBuf::from(dir),
            None => PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("assets"),
        };
        let promoted_root = match std::env::var_os("UI_SERVO_PROMOTED_ROOT") {
            Some(dir) => PathBuf::from(dir),
            None => PathBuf::from(env!("CARGO_MANIFEST_DIR")),
        };
        let dev = std::env::var("UI_SERVO_DEV").is_ok_and(|value| value == "1");
        let beacon_url = std::env::var("UI_SERVO_BEACON")
            .unwrap_or_else(|_| "http://localhost:8700/beacon".to_owned());
        let turn_id =
            std::env::var("UI_SERVO_TURN_ID").unwrap_or_else(|_| crate::span::new_span_id());

        Self::load(assets_dir, promoted_root, dev, beacon_url, turn_id)
    }

    /// The environment-free half of [`AppState::from_env`], so that tests can
    /// build a dev-mode state without mutating process environment.
    fn load(
        assets_dir: PathBuf,
        promoted_root: PathBuf,
        dev: bool,
        beacon_url: String,
        turn_id: String,
    ) -> Result<Self, StartupError> {
        if !assets_dir.is_dir() {
            return Err(StartupError::MissingAssets(assets_dir));
        }

        // Structural, so verified rather than trusted: if the promotion
        // directory has been configured back inside the served root, every
        // provenance guarantee in this binary is one static GET away from
        // irrelevant.
        let promoted_dir = promoted_root.join(promoted::PROMOTED_DIR);
        if resolve(&promoted_dir).starts_with(resolve(&assets_dir)) {
            return Err(StartupError::PromotedInsideAssets {
                promoted: promoted_dir,
                assets: assets_dir,
            });
        }
        // A link inside the served root can reach back out of it. `ServeDir`
        // follows symlinks, so `assets/picks -> ../promoted` reinstates exactly
        // the hole the separation exists to close, and passes a comparison of
        // the two roots because the roots really are disjoint.
        if let Some(escape) = symlink_escaping(&assets_dir) {
            return Err(StartupError::AssetSymlinkEscapes {
                link: escape.0,
                target: escape.1,
                assets: assets_dir,
            });
        }

        let color_scheme = color_scheme(
            &assets_dir.join(TOKENS_CSS),
            &read(&assets_dir.join(TOKENS_CSS))?,
        )?;
        let motion = motion_table(&assets_dir.join(MOTION_JSON))?;
        let probe_path = locate_probe(&assets_dir);
        let promoted = if dev {
            BTreeMap::new()
        } else {
            verify_promotions(&promoted_root)?
        };

        Ok(Self(Arc::new(Inner {
            assets_dir,
            promoted_root,
            probe_path,
            dev,
            beacon_url,
            turn_id,
            color_scheme,
            motion,
            promoted,
        })))
    }

    /// A state rooted at this crate's committed `assets/`, for tests.
    #[cfg(test)]
    pub fn for_tests(dev: bool) -> Self {
        Self::load(
            PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("assets"),
            PathBuf::from(env!("CARGO_MANIFEST_DIR")),
            dev,
            "http://localhost:8700/beacon".to_owned(),
            "turn-test".to_owned(),
        )
        .expect("the committed assets are valid")
    }

    pub fn assets_dir(&self) -> &Path {
        &self.0.assets_dir
    }

    /// Where promoted picks live. Not under [`AppState::assets_dir`], on purpose.
    pub fn promoted_root(&self) -> &Path {
        &self.0.promoted_root
    }

    /// A verified promoted pick, or `NotPromoted` if nobody has chosen one.
    ///
    /// The two modes differ deliberately. In release the map was built at boot
    /// and every entry in it has already proven its provenance, so serving is a
    /// lookup: no file read, no SHA-256 over the markup, nothing per-request
    /// that can fail. In dev the file is read fresh each time, because the whole
    /// point of dev is that somebody is editing the pick and wants to see the
    /// consequence — including the 500 when they edit it by hand.
    pub fn promoted(&self, part: &str) -> Result<Promoted, PromotionError> {
        if self.0.dev {
            return promoted::load(self.promoted_root(), part);
        }
        self.0
            .promoted
            .get(part)
            .cloned()
            .ok_or_else(|| PromotionError::NotPromoted(part.to_owned()))
    }

    /// `probe.js` on disk, if the sensor runtime has been built yet.
    pub fn probe_path(&self) -> Option<&Path> {
        self.0.probe_path.as_deref()
    }

    /// Dev mode is what decides whether the page carries sensors at all.
    pub fn dev(&self) -> bool {
        self.0.dev
    }

    pub fn turn_id(&self) -> &str {
        &self.0.turn_id
    }

    /// The `<meta name="color-scheme">` value, taken from the token sheet so
    /// the contract stays the single source of truth for it.
    pub fn color_scheme(&self) -> &str {
        &self.0.color_scheme
    }

    /// The `window.__UI_SERVO__` payload injected in dev.
    ///
    /// The `motion` object is `DirectionContract.motion_table()` flattened, and
    /// it must stay *complete*: the probe treats a missing
    /// `reducedMotionRequired` as `false` and therefore stops reporting
    /// animations that run while `prefers-reduced-motion: reduce` matches. A
    /// dropped boolean here is a silently disabled sensor, not a cosmetic
    /// omission, which is why [`motion_table`] refuses to boot without it.
    pub fn probe_config(&self) -> String {
        let config = serde_json::json!({
            "beacon": self.0.beacon_url,
            "turnId": self.0.turn_id,
            "motion": {
                "durations": self.0.motion.get("durations"),
                "easings": self.0.motion.get("easings"),
                "properties": self.0.motion.get("properties"),
                "reducedMotionRequired": self.0.motion.get(REDUCED_MOTION_KEY),
            },
        });
        // serde_json escapes `<` and `/` as literal characters, not as HTML, so
        // close the one hole that matters inside an inline <script>.
        config.to_string().replace("</", "<\\/")
    }
}

/// Verify every promoted fragment on disk, or refuse to start.
///
/// This is the release-mode half of the provenance contract. Verifying lazily
/// means an ungated or tampered pick is discovered by the first visitor to load
/// the page that uses it, long after the process reported itself healthy; doing
/// it here turns that into a failed boot, which is the event a deploy already
/// knows how to react to. It is also what makes [`AppState::promoted`] cheap:
/// having checked the hash once, the request path never has to.
///
/// A missing directory is not a failure — a repo where nothing has been promoted
/// yet is a legitimate state, and the pages fall back to their placeholders.
fn verify_promotions(root: &Path) -> Result<BTreeMap<String, Promoted>, StartupError> {
    let dir = root.join(promoted::PROMOTED_DIR);
    let entries = match std::fs::read_dir(&dir) {
        Ok(entries) => entries,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Ok(BTreeMap::new());
        }
        Err(source) => return Err(StartupError::Unreadable { path: dir, source }),
    };

    let mut verified = BTreeMap::new();
    for entry in entries {
        let path = entry
            .map_err(|source| StartupError::Unreadable {
                path: dir.clone(),
                source,
            })?
            .path();
        if path.extension().is_none_or(|ext| ext != "html") {
            continue;
        }
        // `load` re-derives the path from the part name, which also re-runs the
        // slug check — a file named `..html` or with a non-slug stem is refused
        // here rather than becoming an unreachable entry in the map.
        let Some(part) = path.file_stem().and_then(|stem| stem.to_str()) else {
            continue;
        };
        let promotion = promoted::load(root, part)
            .map_err(|error| StartupError::UngatedPromotion(error.to_string()))?;
        verified.insert(part.to_owned(), promotion);
    }
    Ok(verified)
}

/// An absolute, symlink-free path, for paths that may not exist yet.
///
/// `canonicalize` fails on a missing path, and the first version of this check
/// fell back to the path as written. That was the bug: with a *relative*
/// `UI_SERVO_PROMOTED_ROOT` and no promotion directory created yet, the promoted
/// side stayed relative while the asset side resolved to absolute, the two could
/// not possibly share a prefix, and the overlap check passed. The first
/// promotion then created the directory *inside* the served root, and the raw
/// file was a plain GET away. Confirmed against the running binary.
///
/// So: make it absolute first, then canonicalise the nearest existing ancestor
/// and re-append the rest.
fn resolve(path: &Path) -> PathBuf {
    let absolute = if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()
            .unwrap_or_else(|_| PathBuf::from("."))
            .join(path)
    };

    let mut missing = Vec::new();
    let mut existing = absolute.as_path();
    loop {
        if let Ok(canonical) = existing.canonicalize() {
            let mut resolved = canonical;
            for part in missing.iter().rev() {
                resolved.push(part);
            }
            return resolved;
        }
        match (existing.file_name(), existing.parent()) {
            (Some(name), Some(parent)) => {
                missing.push(name.to_owned());
                existing = parent;
            }
            _ => return absolute,
        }
    }
}

/// The first symlink under `assets_dir` whose target leaves it, if any.
///
/// Walked at boot rather than defended per-request: `ServeDir` resolves links
/// itself, so by the time a request arrives the decision has been made.
fn symlink_escaping(assets_dir: &Path) -> Option<(PathBuf, PathBuf)> {
    let root = resolve(assets_dir);
    let mut stack = vec![assets_dir.to_path_buf()];
    while let Some(dir) = stack.pop() {
        let Ok(entries) = std::fs::read_dir(&dir) else {
            continue;
        };
        for entry in entries.flatten() {
            let path = entry.path();
            let is_link = std::fs::symlink_metadata(&path)
                .map(|meta| meta.file_type().is_symlink())
                .unwrap_or(false);
            if is_link {
                let target = resolve(&path);
                if !target.starts_with(&root) {
                    return Some((path, target));
                }
                continue;
            }
            if path.is_dir() {
                stack.push(path);
            }
        }
    }
    None
}

fn read(path: &Path) -> Result<String, StartupError> {
    std::fs::read_to_string(path).map_err(|source| StartupError::Unreadable {
        path: path.to_path_buf(),
        source,
    })
}

/// Pull `color-scheme: dark;` out of the generated token sheet, having first
/// established that the sheet is a token sheet at all.
///
/// An empty or truncated `tokens.css` would render a colourless page that still
/// returns 200, and a server whose whole job is measuring conformance must not
/// serve that. One `--color-*` declaration is the cheapest proof that the emit
/// step actually ran. `color-scheme` itself stays lenient — it is a hint, and
/// the contract may legitimately omit it.
fn color_scheme(path: &Path, tokens: &str) -> Result<String, StartupError> {
    if !tokens.contains("--color-") {
        return Err(StartupError::TokensNotEmitted {
            path: path.to_path_buf(),
        });
    }
    Ok(tokens
        .lines()
        .find_map(|line| line.trim().strip_prefix("color-scheme:"))
        .map(|value| value.trim_end_matches(';').trim().to_owned())
        .unwrap_or_else(|| "dark light".to_owned()))
}

/// The probe's name for the reduced-motion flag (it also accepts the dataclass's
/// snake_case `reduced_motion_required`; we emit the camelCase spelling).
const REDUCED_MOTION_KEY: &str = "reducedMotionRequired";

/// Load `motion.json` and insist on every key the probe needs.
fn motion_table(path: &Path) -> Result<Value, StartupError> {
    let value: Value =
        serde_json::from_str(&read(path)?).map_err(|source| StartupError::MalformedJson {
            path: path.to_path_buf(),
            source,
        })?;
    for key in ["durations", "easings", "properties"] {
        if !value.get(key).is_some_and(Value::is_array) {
            return Err(StartupError::MotionTableIncomplete {
                path: path.to_path_buf(),
                key,
            });
        }
    }
    if !value.get(REDUCED_MOTION_KEY).is_some_and(Value::is_boolean) {
        return Err(StartupError::MotionTableIncomplete {
            path: path.to_path_buf(),
            key: REDUCED_MOTION_KEY,
        });
    }
    Ok(value)
}

/// `probe.js` may be vendored into `assets/`, sitting in the sibling `probe/`
/// crate-of-one during development, or pointed at explicitly. Resolve once.
fn locate_probe(assets_dir: &Path) -> Option<PathBuf> {
    let candidates = [
        std::env::var_os("UI_SERVO_PROBE").map(PathBuf::from),
        Some(assets_dir.join("probe.js")),
        Some(
            PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .join("..")
                .join("probe")
                .join("probe.js"),
        ),
    ];
    candidates
        .into_iter()
        .flatten()
        .find(|candidate| candidate.is_file())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn asset(name: &str) -> PathBuf {
        Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("assets")
            .join(name)
    }

    #[test]
    fn color_scheme_comes_from_the_token_sheet() {
        let sheet = ":root {\n  color-scheme: dark;\n  --color-text: oklch(1 0 0);\n}\n";
        assert_eq!(color_scheme(Path::new("t.css"), sheet).unwrap(), "dark");
    }

    #[test]
    fn color_scheme_falls_back_when_absent() {
        let sheet = ":root { --color-text: oklch(1 0 0); }";
        assert_eq!(
            color_scheme(Path::new("t.css"), sheet).unwrap(),
            "dark light"
        );
    }

    /// The README promises the server refuses to start on a malformed token
    /// sheet. This is that promise.
    #[test]
    fn an_unemitted_token_sheet_is_a_startup_failure() {
        for sheet in ["", "\n", ":root {}", "/* only a comment */"] {
            assert!(
                matches!(
                    color_scheme(Path::new("t.css"), sheet),
                    Err(StartupError::TokensNotEmitted { .. })
                ),
                "{sheet:?} was accepted"
            );
        }
    }

    #[test]
    fn the_shipped_token_sheet_declares_a_scheme() {
        let path = asset(TOKENS_CSS);
        let tokens = std::fs::read_to_string(&path).expect("tokens.css is generated and committed");
        assert_ne!(color_scheme(&path, &tokens).unwrap(), "dark light");
    }

    #[test]
    fn the_shipped_motion_table_is_complete() {
        let table =
            motion_table(&asset(MOTION_JSON)).expect("motion.json is generated and committed");
        assert!(!table["durations"].as_array().unwrap().is_empty());
        assert!(table[REDUCED_MOTION_KEY].is_boolean());
    }

    /// A motion table without the reduced-motion flag disables a whole class of
    /// probe violation silently, so it must not be loadable.
    #[test]
    fn a_motion_table_without_reduced_motion_is_rejected() {
        let dir =
            std::env::temp_dir().join(format!("ui-servo-motion-{}", crate::span::new_span_id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join(MOTION_JSON);
        std::fs::write(
            &path,
            r#"{"durations":[90],"easings":["linear"],"properties":["opacity"]}"#,
        )
        .unwrap();

        let outcome = motion_table(&path);
        std::fs::remove_dir_all(&dir).ok();

        assert!(
            matches!(
                outcome,
                Err(StartupError::MotionTableIncomplete { key, .. }) if key == REDUCED_MOTION_KEY
            ),
            "{outcome:?}"
        );
    }

    /// htmx applies these itself during a swap. If the stylesheet does not
    /// declare them, the probe's CSSOM check reports an `unknown-class`
    /// violation on every swap the site ever performs.
    #[test]
    fn the_stylesheet_declares_the_htmx_lifecycle_classes() {
        let css = std::fs::read_to_string(asset("site.css")).expect("site.css is committed");
        for class in [
            "htmx-request",
            "htmx-swapping",
            "htmx-settling",
            "htmx-added",
            "htmx-indicator",
        ] {
            assert!(
                css.contains(&format!(".{class} {{")),
                "site.css does not declare .{class}; every swap would emit a false unknown-class"
            );
        }
    }

    /// The gate reads classes in fragment markup. Anything the stylesheet hangs
    /// off `[data-fragment]` or `[data-span-id]` is therefore invisible to it —
    /// so those selectors must not confer visual identity, or the loop judges
    /// one appearance and the site serves another. This is the CSS-side twin of
    /// `fragments::tests::the_frame_imposes_no_visual_chrome`.
    #[test]
    fn the_sensor_attributes_carry_no_visual_identity() {
        let css = std::fs::read_to_string(asset("site.css")).expect("site.css is committed");
        // Every rule whose selector mentions a sensor attribute, not just the
        // two bare ones: `[data-fragment] > *`, `section[data-span-id]:hover` and
        // friends are the same hole with a longer selector.
        let mut checked = 0;
        for rule in css.split('}') {
            // `rsplit_once`, not `split_once`: in `@media (…) { [data-fragment] { … `
            // the first `{` belongs to the at-rule, so splitting there puts the
            // real selector inside the *block* half and the rule is skipped as
            // unrecognisable — which is how chrome inside a media query walked
            // straight past the first version of this guard.
            let Some((selector, block)) = rule.rsplit_once('{') else {
                continue;
            };
            // The selector of a rule nested in `@media`/`@supports` arrives with
            // the at-rule prelude still attached to its front; take the part
            // after the last `{` boundary so those rules are checked too rather
            // than skipped for having an unrecognisable selector.
            let selector = selector.rsplit('{').next().unwrap_or(selector).trim();
            if !selector.contains("[data-fragment") && !selector.contains("[data-span-id") {
                continue;
            }
            checked += 1;
            // An allowlist, not a denylist: naming the forbidden properties means
            // the next one nobody thought of (`filter`, `backdrop-filter`,
            // `mask`…) walks straight in. Layout and behaviour are the only
            // things a sensor attribute may carry.
            for declaration in block.split(';') {
                let Some((property, _)) = declaration.split_once(':') else {
                    continue;
                };
                let property = property.trim();
                if property.is_empty() {
                    continue;
                }
                // Custom properties are *not* skipped: `--card-bg` set on a
                // sensor selector is inherited by everything inside it, which is
                // visual identity taking the scenic route.

                assert!(
                    matches!(
                        property,
                        "animation"
                            | "animation-name"
                            | "animation-duration"
                            | "animation-timing-function"
                            | "animation-fill-mode"
                            | "max-width"
                            | "min-width"
                            | "width"
                            | "display"
                            | "margin-block-end"
                            | "contain"
                            | "content-visibility"
                    ),
                    "{selector} sets {property:?}. The class-0 gate reads classes in fragment \
                     markup, so anything hung off a sensor attribute is invisible to it — the \
                     loop would judge one appearance and the site would serve another. If this \
                     property is genuinely layout or behaviour, add it to the allowlist here, \
                     in a diff, once."
                );
            }
        }
        assert!(
            checked >= 2,
            "no rules matched the sensor attributes; the selectors were renamed and this \
             guard silently stopped guarding"
        );
    }

    /// tokens.css converts px to rem against `rem_base_px = 16`. Restating
    /// `font-size` on the root element rescales every rem token at once — an
    /// off-contract site that every gate still reads as on-contract.
    #[test]
    fn the_stylesheet_does_not_restate_the_root_font_size() {
        let css = std::fs::read_to_string(asset("site.css")).expect("site.css is committed");
        let root_block = css
            .split_once("\nhtml {")
            .and_then(|(_, rest)| rest.split_once('}'))
            .map(|(block, _)| block)
            .expect("site.css styles the html element");
        assert!(
            !root_block.contains("font-size"),
            "html restates font-size, so every rem token drifts:\n{root_block}"
        );

        let tokens = std::fs::read_to_string(asset(TOKENS_CSS)).expect("tokens.css is committed");
        assert!(
            tokens.contains("--space-base: 0.5rem;"),
            "the spacing base is no longer 0.5rem (8px at the 16px conversion base)"
        );
    }

    /// Configuration must not be able to undo the structural fix.
    ///
    /// Promoted markup is unservable-without-verification *because* no
    /// `ServeDir` can reach it. Deny routes were tried first and lost to
    /// percent-encoding. If `UI_SERVO_ASSETS` is ever pointed at a parent of the
    /// promotion directory, that whole argument evaporates silently — the server
    /// still starts, still serves, and the file is a plain GET away.
    #[test]
    fn a_promotion_directory_inside_the_asset_root_is_refused() {
        let site = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        // The misconfiguration: serve the crate root, which contains promoted/.
        let outcome = AppState::load(site.clone(), site.clone(), false, "b".into(), "t".into());
        assert!(
            matches!(outcome, Err(StartupError::PromotedInsideAssets { .. })),
            "a server whose static root contains the promotion directory started: {outcome:?}"
        );

        // The real layout is fine, in both modes.
        for dev in [true, false] {
            let ok = AppState::load(site.join("assets"), site.clone(), dev, "b".into(), "t".into());
            assert!(ok.is_ok(), "the committed layout was refused: {ok:?}");
        }
    }

    /// The overlap check must hold for a promotion directory that does not exist
    /// yet — which is every repo before its first promotion.
    ///
    /// `canonicalize` fails on a missing path. The first version fell back to
    /// the path as written, so a relative promoted root stayed relative while
    /// the asset root resolved to absolute; the two could not share a prefix,
    /// the check passed, and the first promotion created the directory inside
    /// the served root.
    #[test]
    fn a_relative_root_whose_promotion_directory_is_missing_is_still_checked() {
        let dir = std::env::temp_dir().join(format!("ui-servo-rel-{}", crate::span::new_span_id()));
        std::fs::create_dir_all(dir.join("assets")).unwrap();
        for name in [TOKENS_CSS, MOTION_JSON] {
            std::fs::copy(asset(name), dir.join("assets").join(name)).unwrap();
        }
        // No `promoted/` yet, and both roots given relative to `dir`.
        let previous = std::env::current_dir().unwrap();
        std::env::set_current_dir(&dir).unwrap();
        let outcome = AppState::load(
            PathBuf::from("assets"),
            PathBuf::from("assets"),
            false,
            "b".into(),
            "t".into(),
        );
        std::env::set_current_dir(previous).unwrap();
        std::fs::remove_dir_all(&dir).ok();

        assert!(
            matches!(outcome, Err(StartupError::PromotedInsideAssets { .. })),
            "overlap went undetected because the promotion directory did not exist yet: \
             {outcome:?}"
        );
    }

    /// A link inside the served root defeats disjoint roots entirely.
    #[test]
    fn a_symlink_out_of_the_asset_root_is_refused() {
        let dir = std::env::temp_dir().join(format!("ui-servo-link-{}", crate::span::new_span_id()));
        std::fs::create_dir_all(dir.join("assets")).unwrap();
        std::fs::create_dir_all(dir.join(promoted::PROMOTED_DIR)).unwrap();
        for name in [TOKENS_CSS, MOTION_JSON] {
            std::fs::copy(asset(name), dir.join("assets").join(name)).unwrap();
        }
        std::os::unix::fs::symlink("../promoted", dir.join("assets").join("picks")).unwrap();

        let outcome =
            AppState::load(dir.join("assets"), dir.clone(), false, "b".into(), "t".into());
        std::fs::remove_dir_all(&dir).ok();

        assert!(
            matches!(outcome, Err(StartupError::AssetSymlinkEscapes { .. })),
            "a symlink reaching out of the served root was accepted: {outcome:?}"
        );
    }

    /// A release server must not come up carrying a fragment that cannot prove
    /// it was gated. If it does, the deploy reports success and the breakage
    /// waits for a visitor.
    #[test]
    fn a_tampered_promotion_stops_the_server_from_starting() {
        let dir = std::env::temp_dir().join(format!("ui-servo-boot-{}", crate::span::new_span_id()));
        std::fs::create_dir_all(dir.join(promoted::PROMOTED_DIR)).unwrap();
        std::fs::create_dir_all(dir.join("assets")).unwrap();
        for name in [TOKENS_CSS, MOTION_JSON] {
            std::fs::copy(asset(name), dir.join("assets").join(name)).unwrap();
        }
        std::fs::write(
            dir.join(promoted::PROMOTED_DIR).join("hero.html"),
            "<!-- ui-servo: gated round=1 sha256=deadbeef -->\n<section class=\"my-lg\">x</section>\n",
        )
        .unwrap();

        let release =
            AppState::load(dir.join("assets"), dir.clone(), false, "b".into(), "t".into());
        // Dev is deliberately permissive: the pick is under active edit, and the
        // per-request 500 is the feedback the author wants.
        let dev = AppState::load(dir.join("assets"), dir.clone(), true, "b".into(), "t".into());
        std::fs::remove_dir_all(&dir).ok();

        assert!(
            matches!(release, Err(StartupError::UngatedPromotion(_))),
            "release mode booted with an ungated hero: {release:?}"
        );
        assert!(dev.is_ok(), "dev mode should still start: {dev:?}");
    }

    /// The committed pick has to survive its own server's boot check, or the
    /// site in this repo is one `cargo run --release` away from not starting.
    #[test]
    fn the_committed_promotions_all_verify_at_boot() {
        let verified = verify_promotions(Path::new(env!("CARGO_MANIFEST_DIR")))
            .expect("the committed promotions verify");
        assert!(
            verified.contains_key("hero"),
            "no hero is promoted; the demo round's pick is not committed"
        );
    }

    /// In release the markup is served straight out of the boot-time map, so a
    /// file that changes underneath a running process must not change what it
    /// serves — that would be an ungated edit taking effect without a restart.
    #[test]
    fn release_mode_serves_the_verified_copy_not_the_file() {
        // Build a throwaway site so the file can actually be tampered with. The
        // previous version of this test called `promoted()` twice on the real
        // repo and compared the results, which is a test of `BTreeMap::get` --
        // it would have passed just as happily with no cache at all.
        let dir = std::env::temp_dir().join(format!("ui-servo-cache-{}", crate::span::new_span_id()));
        std::fs::create_dir_all(dir.join("assets")).unwrap();
        std::fs::create_dir_all(dir.join(promoted::PROMOTED_DIR)).unwrap();
        for name in [TOKENS_CSS, MOTION_JSON] {
            std::fs::copy(asset(name), dir.join("assets").join(name)).unwrap();
        }
        let promoted_file = dir.join(promoted::PROMOTED_DIR).join("hero.html");
        let honest = std::fs::read_to_string(
            Path::new(env!("CARGO_MANIFEST_DIR")).join(promoted::PROMOTED_DIR).join("hero.html"),
        )
        .expect("the repo has a promoted hero");
        std::fs::write(&promoted_file, &honest).unwrap();

        let release =
            AppState::load(dir.join("assets"), dir.clone(), false, "b".into(), "t".into()).unwrap();
        let dev =
            AppState::load(dir.join("assets"), dir.clone(), true, "b".into(), "t".into()).unwrap();
        let before = release.promoted("hero").expect("hero is promoted");

        // Now edit the file out from under both running servers.
        std::fs::write(&promoted_file, format!("{honest}<p>smuggled</p>\n")).unwrap();

        let after_release = release.promoted("hero");
        let after_dev = dev.promoted("hero");
        std::fs::remove_dir_all(&dir).ok();

        // Release keeps serving what it verified at boot: the edit is not
        // refused at request time, it is simply not visible, which is the point
        // of verifying once.
        assert_eq!(
            after_release.as_ref().ok(),
            Some(&before),
            "release served post-boot disk content instead of the copy it verified"
        );
        assert!(
            !after_release.unwrap().markup().contains("smuggled"),
            "an ungated edit reached a release response"
        );
        // Dev reads through, so the same edit is caught immediately.
        assert!(
            matches!(after_dev, Err(PromotionError::HashMismatch(..))),
            "dev did not notice the file changing: {after_dev:?}"
        );
        assert!(
            !before.markup().contains("data-span-id"),
            "the promoted body kept a candidate span id; live evidence would file under it"
        );
    }

    /// The probe reads `motion.reducedMotionRequired`; if the injected config
    /// omits it the probe defaults to `false` and stops reporting animations
    /// that run under `prefers-reduced-motion: reduce`.
    #[test]
    fn the_injected_config_carries_every_motion_key() {
        let config = AppState::for_tests(true).probe_config();
        let parsed: Value = serde_json::from_str(&config).expect("config is valid JSON");

        assert_eq!(parsed["motion"][REDUCED_MOTION_KEY], Value::Bool(true));
        for key in ["durations", "easings", "properties"] {
            assert!(
                parsed["motion"][key].is_array(),
                "{key} missing from {config}"
            );
        }
        assert!(parsed["beacon"].is_string());
        assert!(parsed["turnId"].is_string());
    }
}
