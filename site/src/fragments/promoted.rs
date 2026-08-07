//! Promoted fragments: the only path from a human pick to the live site.
//!
//! The gauntlet ends with a person choosing one variant off the frontier report.
//! That choice is written to `site/assets/fragments/<part>.html` and served from
//! here, which is what makes the loop closed rather than advisory — a pick that
//! only ever lived in `evidence/` would change nothing anyone can visit.
//!
//! **Provenance is mandatory.** Every promoted file must carry
//! `<!-- ui-servo: gated round=<n> sha256=<hash> -->` as written by the
//! promotion step. The class-0 sanitiser is Python and runs at promotion time,
//! so this comment is the only evidence the running site has that the markup it
//! is about to serve was ever gated at all. A file without it — hand-dropped,
//! half-copied, or written by something that skipped the loop — is refused. The
//! hash is checked too: an edit after promotion is an ungated edit.
//!
//! The refusal is loud on purpose. In dev it is a 500 and a logged violation; a
//! silent fallback to the placeholder would let an ungated fragment look exactly
//! like a page nobody has picked for yet.

use std::fs;
use std::path::{Path, PathBuf};

use maud::{Markup, PreEscaped, html};
use sha2::{Digest, Sha256};
use tracing::warn;

/// Where promoted picks live, relative to the assets dir. Tracked in git:
/// a pick is a decision, not a cache.
pub const PROMOTED_DIR: &str = "fragments";

const PROVENANCE_PREFIX: &str = "<!-- ui-servo: gated";

#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum PromotionError {
    #[error("no promoted fragment for part {0:?}")]
    NotPromoted(String),
    #[error(
        "part {0:?} is not a slug; axum percent-decodes path captures, so a part \
         carrying a slash, a dot-dot or an absolute prefix would read outside the \
         promotion directory"
    )]
    NotASlug(String),
    #[error("promoted fragment {0:?} is unreadable: {1}")]
    Unreadable(String, String),
    #[error(
        "promoted fragment {0:?} carries no ui-servo provenance comment; it was never gated \
         (promote through the gauntlet instead of writing this file by hand)"
    )]
    MissingProvenance(String),
    #[error(
        "promoted fragment {0:?} was edited after promotion: provenance records sha256 {1}, \
         the markup on disk hashes to {2}"
    )]
    HashMismatch(String, String, String),
}

/// A promoted fragment that has proven where it came from.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Promoted {
    pub part: String,
    pub round: String,
    pub markup: String,
}

/// The same grammar the writer enforces (`ui_servo/control/promote.py`): a part
/// is a slug or it is refused. Checked here rather than trusted from the URL,
/// because this is the side that touches the filesystem.
fn is_slug(part: &str) -> bool {
    !part.is_empty()
        && part.len() <= 64
        && part
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-'))
        && part != "."
        && part != ".."
}

fn promoted_path(assets_dir: &Path, part: &str) -> Result<PathBuf, PromotionError> {
    if !is_slug(part) {
        return Err(PromotionError::NotASlug(part.to_owned()));
    }
    Ok(assets_dir.join(PROMOTED_DIR).join(format!("{part}.html")))
}

/// Read the provenance comment's `round=` and `sha256=` values, if both are there.
fn parse_provenance(markup: &str) -> Option<(String, String)> {
    let line = markup
        .lines()
        .find(|line| line.trim_start().starts_with(PROVENANCE_PREFIX))?;
    let round = field(line, "round=")?;
    let sha = field(line, "sha256=")?;
    Some((round, sha))
}

fn field(line: &str, key: &str) -> Option<String> {
    let rest = &line[line.find(key)? + key.len()..];
    let value: String = rest
        .chars()
        .take_while(|c| !c.is_whitespace() && *c != '-' && *c != '>')
        .collect();
    (!value.is_empty()).then_some(value)
}

/// The body a hash covers: everything after the provenance line. The comment
/// cannot cover itself, and anchoring on "the rest of the file" means adding a
/// second comment is also a mismatch.
fn hashed_body(markup: &str) -> &str {
    match markup.find('\n') {
        Some(index) if markup.trim_start().starts_with(PROVENANCE_PREFIX) => &markup[index + 1..],
        _ => markup,
    }
}

pub fn sha256_of(body: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(body.trim().as_bytes());
    format!("{:x}", hasher.finalize())
}

/// Load a promoted fragment, verifying it was gated and unedited since.
pub fn load(assets_dir: &Path, part: &str) -> Result<Promoted, PromotionError> {
    let path = promoted_path(assets_dir, part)?;
    if !path.is_file() {
        return Err(PromotionError::NotPromoted(part.to_owned()));
    }
    let raw = fs::read_to_string(&path)
        .map_err(|error| PromotionError::Unreadable(part.to_owned(), error.to_string()))?;

    let (round, recorded) =
        parse_provenance(&raw).ok_or_else(|| PromotionError::MissingProvenance(part.to_owned()))?;
    let actual = sha256_of(hashed_body(&raw));
    if actual != recorded {
        return Err(PromotionError::HashMismatch(
            part.to_owned(),
            recorded,
            actual,
        ));
    }
    Ok(Promoted {
        part: part.to_owned(),
        round,
        markup: hashed_body(&raw).trim().to_owned(),
    })
}

/// Frame a promotion that has already proven itself, so a pick is measurable on
/// exactly the same terms as a hand-written fragment.
///
/// Takes a [`Promoted`] rather than a path because that type cannot be
/// constructed without passing [`load`] — the verification is in the type, and
/// this function has no way to render something unverified even by mistake.
pub fn render_verified(promoted: &Promoted) -> Markup {
    super::frame(
        &format!("promoted-{}", promoted.part),
        &format!("Promoted {} (round {})", promoted.part, promoted.round),
        // Gated by the class-0 sanitiser at promotion and unchanged since; that
        // is the whole reason the provenance check exists.
        html! { (PreEscaped(promoted.markup.clone())) },
    )
}

/// Load and frame in one step. Used by the dev path and the tests; the release
/// path goes through `AppState::promoted`, which verified at boot.
pub fn render(assets_dir: &Path, part: &str) -> Result<Markup, PromotionError> {
    Ok(render_verified(&load(assets_dir, part)?))
}

/// Render the promotion if there is one, else `None` so the caller can fall back
/// to its built-in placeholder. A *broken* promotion is not `None` — it is
/// logged and still `None`, because refusing to serve is the point, but the page
/// should say so rather than silently pretending nothing was ever picked.
///
/// The lookup is a parameter so the same fallback logic covers both modes: in
/// release it is `AppState::promoted`, reading a map verified at boot; in dev
/// and in tests it is [`load`], hitting the disk. The policy — what counts as
/// absent versus broken — must not differ between them.
pub fn render_or_placeholder(
    lookup: impl FnOnce(&str) -> Result<Promoted, PromotionError>,
    part: &str,
) -> Result<Option<Markup>, PromotionError> {
    match lookup(part) {
        Ok(promoted) => Ok(Some(render_verified(&promoted))),
        // Nothing has been picked yet. The placeholder is the honest answer.
        Err(PromotionError::NotPromoted(_)) => Ok(None),
        // A file exists and cannot prove itself. Falling back here would let a
        // tampered hero render as a page that merely looks unfinished, which is
        // the one outcome the provenance check exists to prevent -- so the
        // caller is handed the error and the page fails loudly instead.
        Err(error) => {
            warn!(%error, part, "refusing to serve a promoted fragment");
            Err(error)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    fn write(root: &Path, part: &str, contents: &str) {
        let dir = root.join(PROMOTED_DIR);
        fs::create_dir_all(&dir).unwrap();
        fs::write(dir.join(format!("{part}.html")), contents).unwrap();
    }

    fn temp() -> PathBuf {
        let base = std::env::temp_dir().join(format!(
            "ui-servo-promoted-{}-{:?}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&base).unwrap();
        base
    }

    fn promoted_file(body: &str, round: &str) -> String {
        format!(
            "<!-- ui-servo: gated round={round} sha256={} -->\n{body}",
            sha256_of(body)
        )
    }

    #[test]
    fn a_gated_pick_is_served() {
        let root = temp();
        let body = "<p class=\"text-md\">picked</p>";
        write(&root, "hero", &promoted_file(body, "1"));
        let loaded = load(&root, "hero").unwrap();
        assert_eq!(loaded.round, "1");
        assert_eq!(loaded.markup, body);
        let markup = render(&root, "hero").unwrap().into_string();
        assert!(markup.contains("data-span-id="), "{markup}");
        assert!(markup.contains("picked"), "{markup}");
    }

    #[test]
    fn an_unpromoted_part_is_absent_not_broken() {
        let root = temp();
        assert_eq!(
            load(&root, "hero").unwrap_err(),
            PromotionError::NotPromoted("hero".into())
        );
        assert!(render_or_placeholder(|part| load(&root, part), "hero").unwrap().is_none());
    }

    #[test]
    fn a_hand_dropped_fragment_is_refused() {
        let root = temp();
        write(&root, "hero", "<p class=\"text-md\">never gated</p>");
        assert_eq!(
            load(&root, "hero").unwrap_err(),
            PromotionError::MissingProvenance("hero".into())
        );
        assert!(render(&root, "hero").is_err());
    }

    #[test]
    fn a_corrupt_promotion_is_an_error_not_a_placeholder() {
        // The distinction the review caught: "nobody picked yet" is None,
        // "someone edited the pick" is an error the page must not paper over.
        let root = temp();
        write(&root, "hero", "<p class=\"text-md\">never gated</p>");
        assert!(render_or_placeholder(|part| load(&root, part), "hero").is_err());
    }

    #[test]
    fn a_part_that_is_not_a_slug_is_refused_before_touching_the_disk() {
        let root = temp();
        for hostile in ["../../etc/passwd", "a/b", "..", "", "with space"] {
            assert!(
                matches!(load(&root, hostile), Err(PromotionError::NotASlug(_))),
                "{hostile:?} was not refused"
            );
        }
    }

    #[test]
    fn an_edit_after_promotion_is_refused() {
        let root = temp();
        let body = "<p class=\"text-md\">picked</p>";
        let mut file = promoted_file(body, "1");
        file.push_str("<p class=\"text-md\">smuggled in after the gate</p>");
        write(&root, "hero", &file);
        assert!(matches!(
            load(&root, "hero").unwrap_err(),
            PromotionError::HashMismatch(..)
        ));
    }
}
