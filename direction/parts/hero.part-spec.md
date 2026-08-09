# Part: hero (v2 — terminal hero; supersedes the 2026-08-05 spec)

Read `_conventions.md` first. The first thing on `/` at mosoti.dev: a
terminal frame whose prompt line answers `$ whoami`.

What it renders, from the prototype's own copy:

- A prompt line: `$ whoami` in accent, followed by a caret. The caret is a
  **static block** — the v2 contract has no 1s duration, so it does not
  blink. Do not fake a blink with JS style toggles; that is evading the
  motion table, not satisfying it.
- The answer, display-scale: **kennedy.mosoti — observability platform
  engineer, branching into agentic engineering**.
- One thesis line, muted, plain: *Building the thing is easy. Knowing if
  it's working is the actual job.* This is the owner's own sentence in their
  own prototype — it stays conversational in size and weight; it must not be
  set apart as display-serif scripture (there is no serif anymore, and no
  pullquote treatment here either).

Progressive enhancement: the full text is server-rendered and complete with
JS off. A typing effect may replay it on load (JS `textContent`, not CSS
animation), and must render everything instantly when
`prefers-reduced-motion` matches.

Hard rules carried over from v1, still in force:

- **No tool or vendor names in the hero.** Certifications live in the
  cred-row part, not here. The typed title above contains none; keep it so.
- No invented employers, titles, or locations. "Nairobi" appearing anywhere
  is an automatic gate failure — that was demo copy once, and this line is
  the reason the rule is written down.
- Kennedy Mosoti is in Dallas–Fort Worth, TX. The hero may say so or stay
  silent; it may not say otherwise.
- One `<section>` root with a span id; a hero that needs three paragraphs is
  an about page.
- The terminal frame may sit on `bg-surface` with a `border-border` hairline
  — that is the one card-shaped thing on the page, and it is a *terminal*,
  not a product card. Nothing else in the hero gets a box.
