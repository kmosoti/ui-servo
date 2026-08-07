//! Promoted fragments: the only path from a human pick to the live site.
//!
//! The gauntlet ends with a person choosing one variant off the frontier report.
//! That choice is written to `site/assets/fragments/<part>.html` and served from
//! here, which is what makes the loop closed rather than advisory — a pick that
//! only ever lived in `evidence/` would change nothing anyone can visit.
//!
//! **Provenance is mandatory, and is integrity rather than authorship.** Every
//! promoted file must carry `<!-- ui-servo: gated round=<n> sha256=<hash> -->`
//! as written by the promotion step, over a preimage that includes the part and
//! the round. A file without it — hand-dropped, half-copied, or written by
//! something that skipped the loop by mistake — is refused, and so is one whose
//! body or metadata changed after promotion.
//!
//! What it does *not* do is establish that somebody was authorised to write the
//! file. The digest is unkeyed and sits inside the file it describes, so a
//! deliberate forger recomputes it in one line; that was demonstrated in review.
//! The check is worth having because the failures it catches are the ones that
//! actually occur — a stale pick, a hand edit, a bad merge — and because every
//! one of them would otherwise be silent. It is not worth overstating, and
//! earlier versions of this comment did.
//!
//! The refusal is loud on purpose. In dev it is a 500 and a logged violation; a
//! silent fallback to the placeholder would let an ungated fragment look exactly
//! like a page nobody has picked for yet.

use std::fs;
use std::path::{Path, PathBuf};

use maud::{Markup, PreEscaped, html};
use sha2::{Digest, Sha256};
use tracing::warn;

/// Where promoted picks live. Tracked in git: a pick is a decision, not a cache.
///
/// **Deliberately outside `assets/`.** They used to live at `assets/fragments/`
/// and were kept unreachable by deny routes on `/assets/fragments/{*rest}`.
/// That did not hold: axum matches the *raw* path while `ServeDir` percent-
/// decodes, so `/assets/%66ragments/hero.html` missed the deny route, decoded
/// back to the real file, and served the raw body — no provenance check, no
/// hash check, no frame. Verified against the running binary before this move.
///
/// Route-based protection of a file inside the static root is a denylist over an
/// attacker-controlled encoding, and the fix for a denylist is usually to stop
/// needing one. A file that must never be served statically does not belong in
/// the directory that serves files statically.
pub const PROMOTED_DIR: &str = "promoted";

const PROVENANCE_PREFIX: &str = "<!-- ui-servo: gated";
/// The provenance line, split into the three literals around its two values.
/// Mirrors `PROVENANCE_TEMPLATE` in `ui_servo/control/promote.py`.
const PROVENANCE_HEAD: &str = "<!-- ui-servo: gated round=";
const PROVENANCE_MID: &str = " sha256=";
const PROVENANCE_TAIL: &str = " -->";

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
    #[error(
        "promoted fragment {0:?} has a provenance line the promoter would not have \
         written: {1:?}. The body hash covers the markup but not the comment, so the \
         comment is pinned to its exact canonical form instead."
    )]
    ForgedProvenance(String, String),
}

/// A promoted fragment that has proven where it came from.
///
/// The fields are private so the invariant is real rather than documented: the
/// only way to obtain one is [`load`], which verifies provenance and hash. With
/// public fields, any code in the crate could assemble arbitrary markup and hand
/// it to [`render_verified`], and the type would be vouching for nothing.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Promoted {
    part: String,
    round: String,
    markup: String,
}

impl Promoted {
    pub fn part(&self) -> &str {
        &self.part
    }

    pub fn round(&self) -> &str {
        &self.round
    }

    /// The verified markup. Read-only: a caller may serve it, not amend it.
    pub fn markup(&self) -> &str {
        &self.markup
    }
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

fn promoted_path(root: &Path, part: &str) -> Result<PathBuf, PromotionError> {
    if !is_slug(part) {
        return Err(PromotionError::NotASlug(part.to_owned()));
    }
    Ok(root.join(PROMOTED_DIR).join(format!("{part}.html")))
}

/// Read the provenance comment, which must be exactly what the promoter writes.
///
/// Parsed by stripping the three known literals rather than by scanning for
/// `key=` and guessing where each value ends. Scanning needed a terminator set,
/// and every terminator set was wrong for some value the *writer* permits: with
/// `-` in it, `round=round-4` silently became `round`; without it, an id ending
/// in `-` swallowed the closing `-->`. The writer's grammar is
/// `[A-Za-z0-9._-]{1,64}` and this reader now accepts exactly that, because the
/// two sides disagreeing about what a round id is has already caused one bug.
///
/// Matching the whole line is also what pins the metadata. The SHA-256 covers
/// the body, so without this, `round=4` could be edited to `round=99` and every
/// other check would still pass — the file would serve, and lie about where it
/// came from.
fn parse_provenance(line: &str) -> Option<(String, String)> {
    let inner = line
        .trim()
        .strip_prefix(PROVENANCE_HEAD)?
        .strip_suffix(PROVENANCE_TAIL)?;
    let (round, sha) = inner.split_once(PROVENANCE_MID)?;
    (is_token(round) && is_digest(sha)).then(|| (round.to_owned(), sha.to_owned()))
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

/// Format tag in the hash preimage. Mirrors `DIGEST_VERSION` in
/// `ui_servo/control/promote.py`; if one side changes it, every file stops
/// verifying loudly rather than one side silently accepting the other's.
const DIGEST_VERSION: &str = "ui-servo/1";

/// The digest over everything the provenance comment asserts.
///
/// Body-only was not enough. The comment claims *which round produced this*, and
/// with a body-only hash that claim was editable at will: `round=4` -> `round=99`
/// left the body hash valid, so the file loaded, cached at boot, and served a
/// page attributing itself to a round that never produced it. Pinning the
/// comment to a "canonical form" did not help either, because the canonical form
/// was computed *from the parsed values* — it could detect a reordered field and
/// never a changed one. Circular, and it took an adversarial reviewer to say so.
///
/// So the values are bound into the preimage instead. Newline-separated with a
/// version tag; every field is a validated token, so none can contain a
/// separator and shift the boundary between two others.
pub fn sha256_of(body: &str, part: &str, round: &str) -> String {
    // Normalised before hashing, matching `_normalise_newlines` on the writing
    // side. Without it the reader strips `\r` from the header (via `lines()`)
    // but hashes every interior one, so a CRLF checkout of a multi-line fragment
    // is refused as tampering -- a false accusation, with "re-promote it"
    // as the unhelpful advice.
    let body = body.replace("\r\n", "\n").replace('\r', "\n");
    let body = body.as_str();
    let mut hasher = Sha256::new();
    hasher.update(DIGEST_VERSION.as_bytes());
    hasher.update(b"\n");
    hasher.update(part.as_bytes());
    hasher.update(b"\n");
    hasher.update(round.as_bytes());
    hasher.update(b"\n");
    hasher.update(body.trim().as_bytes());
    format!("{:x}", hasher.finalize())
}

/// The writer's grammar (`_SAFE_TOKEN` in `ui_servo/control/promote.py`), applied
/// on the reading side too.
///
/// Without it this reader accepted any non-empty round — Unicode, punctuation,
/// arbitrary length — so a file could claim provenance the promoter is incapable
/// of writing. Two implementations of one format have to agree about what the
/// format *is*, or the stricter one is decorative.
fn is_token(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && value != "."
        && value != ".."
        && value
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-'))
}

/// A sha256 as the writer spells it: 64 lowercase hex digits.
fn is_digest(value: &str) -> bool {
    value.len() == 64 && value.chars().all(|c| c.is_ascii_hexdigit() && !c.is_uppercase())
}

/// Load a promoted fragment, verifying it was gated and unedited since.
pub fn load(root: &Path, part: &str) -> Result<Promoted, PromotionError> {
    let path = promoted_path(root, part)?;
    if !path.is_file() {
        return Err(PromotionError::NotPromoted(part.to_owned()));
    }
    let raw = fs::read_to_string(&path)
        .map_err(|error| PromotionError::Unreadable(part.to_owned(), error.to_string()))?;

    let header = raw.lines().next().unwrap_or_default().trim();
    let (round, recorded) = match parse_provenance(header) {
        Some(parsed) => parsed,
        // Nothing that even looks like provenance: hand-dropped or half-copied.
        None if !header.starts_with(PROVENANCE_PREFIX) => {
            return Err(PromotionError::MissingProvenance(part.to_owned()));
        }
        // It looks like provenance and is not: an edited or hand-built comment.
        None => {
            return Err(PromotionError::ForgedProvenance(
                part.to_owned(),
                header.to_owned(),
            ));
        }
    };
    let actual = sha256_of(hashed_body(&raw), part, &round);
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
        &format!("promoted-{}", promoted.part()),
        &format!("Promoted {} (round {})", promoted.part(), promoted.round()),
        // Gated by the class-0 sanitiser at promotion and unchanged since; that
        // is the whole reason the provenance check exists.
        html! { (PreEscaped(promoted.markup().to_owned())) },
    )
}

/// Load and frame in one step. Test-only: every serving path goes through
/// `AppState::promoted`, so that dev and release cannot disagree about policy.
#[cfg(test)]
pub fn render(root: &Path, part: &str) -> Result<Markup, PromotionError> {
    Ok(render_verified(&load(root, part)?))
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

    /// The file the promoter would write for this part, round and body.
    fn promoted_file_for(part: &str, body: &str, round: &str) -> String {
        format!(
            "<!-- ui-servo: gated round={round} sha256={} -->\n{body}",
            sha256_of(body, part, round)
        )
    }

    fn promoted_file(body: &str, round: &str) -> String {
        promoted_file_for("hero", body, round)
    }

    /// The digest binds the metadata, so editing it is an edit like any other.
    ///
    /// This is the case a "canonical form" check could never catch: the
    /// canonical form was derived from the parsed values, so a changed value
    /// simply produced a different, equally canonical line.
    #[test]
    fn a_doctored_round_is_refused() {
        let root = temp();
        let body = "<p class=\"text-md\">picked</p>";
        let honest = promoted_file(body, "4");
        write(&root, "hero", &honest.replace("round=4 ", "round=99 "));

        assert!(
            matches!(load(&root, "hero"), Err(PromotionError::HashMismatch(..))),
            "a promotion claiming a round it did not come from was accepted"
        );
    }

    /// The same body promoted as a different part is a different file.
    #[test]
    fn a_pick_moved_to_another_part_is_refused() {
        let root = temp();
        let body = "<p class=\"text-md\">picked</p>";
        write(&root, "footer", &promoted_file_for("hero", body, "4"));
        assert!(matches!(
            load(&root, "footer"),
            Err(PromotionError::HashMismatch(..))
        ));
    }

    /// The digest field has a grammar too, and it had no test at all: replacing
    /// the whole of `is_digest` with `!value.is_empty()` left every test green.
    /// Half of "the two sides agree about the format" was unverified.
    #[test]
    fn a_digest_outside_the_writers_grammar_is_refused() {
        let root = temp();
        let body = "<p class=\"text-md\">picked</p>";
        let honest = sha256_of(body, "hero", "4");
        for bad in [
            honest[..63].to_owned(),                       // too short
            format!("{honest}0"),                          // too long
            honest.to_uppercase(),                         // the writer emits lowercase
            honest.replace('a', "g"),                      // not hex
            "0".repeat(64),                                // well-formed, wrong
        ] {
            write(
                &root,
                "hero",
                &format!("<!-- ui-servo: gated round=4 sha256={bad} -->\n{body}"),
            );
            let outcome = load(&root, "hero");
            let refused = matches!(
                outcome,
                Err(PromotionError::ForgedProvenance(..)) | Err(PromotionError::HashMismatch(..))
            );
            assert!(refused, "digest {bad:?} was accepted: {outcome:?}");
        }
    }

    /// A provenance line the promoter could not have written is not provenance.
    #[test]
    fn a_round_outside_the_writers_grammar_is_refused() {
        let root = temp();
        let body = "<p class=\"text-md\">picked</p>";
        for round in ["a round", "røund", &"x".repeat(65), "..", ""] {
            let digest = sha256_of(body, "hero", round);
            write(
                &root,
                "hero",
                &format!("<!-- ui-servo: gated round={round} sha256={digest} -->\n{body}"),
            );
            let outcome = load(&root, "hero");
            assert!(
                matches!(outcome, Err(PromotionError::ForgedProvenance(..))),
                "round {round:?} was accepted: {outcome:?}"
            );
        }
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
