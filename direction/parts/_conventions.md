# Shared conventions for every v2 part (read before any part-spec)

These rules bind every part below; individual specs only add to them.

**Source design (superseded 2026-08-09).** The source of record is now the
golden path: `direction/references/golden/Kennedy Mosoti - Portfolio.dc.html`
— the owner's own build, reproduced verbatim in `site/`. Its README explains
what that means for these specs: copy and facts below still hold, but where
any spec, the v2 contract, or the stitched experiment disagrees with the
golden file, **the golden file wins, including on values**. These specs go
back into effect only after a v3 contract is derived from the golden path.

**Mechanical rules (gate-enforced):**
- One `<section>` root via `fragments::frame` — never hand-rolled.
- Allowlisted classes only (96, derived from the v2 tokens). Structural
  styling goes on semantic elements in `site.css`, not on new classes.
- Motion through `var(--motion-duration-*)` / `var(--motion-ease-*)` on
  `transform`/`opacity` only. Quantise the prototype's ad-hoc timings:
  log-line and reveal entrances → `slow`, token travel → `deliberate`.
- No `height` animation. Disclosure is a `<details>` element: layout snaps
  open, the revealed content may enter with an opacity/translate animation.
- State colour changes (match/dim, pass/fail) snap — no colour transitions.
- Reduced motion is a branch, not an afterthought: every animation has a
  no-motion path and the content is complete without JS.

**Colour semantics:**
- `accent` (ember) is the only colour that may ask for attention.
- `accent-2` (amber) is warning/status — badges like "pre-alpha", "alpha".
- `critical` (red) appears only when something has failed (deep-dive fail
  states). A page with nothing failing on it shows no red.

**The tag vocabulary.** Skills, jobs and projects share one tag vocabulary
(the prototype's `data-tag` keys: `splunk`, `kafka`, `python`, `rust`, …).
Every job entry and project card carries a `data-tags="…"` attribute listing
its space-separated tag keys, and every rendered tag chip carries
`data-tag="<key>"`. That attribute contract is what the page-level
cross-reference behaviour reads; a part that renders tags without it breaks
the highlight feature invisibly. Tag *labels* come from the prototype's
`labelFor` table; keys are the identity.

**Voice.** Plain speech, no ceremony. The owner has explicitly rejected
thesis-line gravitas: an idea may appear as a person talking about their work
in passing, never as scripture. The terminal aesthetic is a register, not a
costume (see the `hacker-theatre` anti-reference).
