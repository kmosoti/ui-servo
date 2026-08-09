# Part: skills-matrix

Read `_conventions.md` first. Six category cards in a 2-up grid (1-up
narrow): Observability & Telemetry, Programming, Architecture, Configuration
Management, Infrastructure & Reliability, AI-Assisted Engineering.

Content: the prototype's `skillCategories` array is the source of record —
titles, subtitles, blurbs, group labels, and the tag key→label table. The
blurbs are the owner's voice at its best ("This is the actual job, not a
bullet point"); compress carefully or not at all. The AI category's honesty
("still learning, not yet claiming mastery") must survive.

Structure per card:

- Title, then a muted subtitle ("how I know it's working").
- A `<details>` disclosure ("+ dive in" / "− less") containing the category
  blurb. Layout snaps open; the blurb may fade/translate in on the contract's
  `slow` duration. No height animation.
- Tag groups: an uppercase micro-label per group, then tag chips. Every chip
  is a `<button class="tag" data-tag="<key>">` — the crossref behaviour
  depends on exactly this shape.

Rules:

- Tags are the interactive currency of the whole page; the active state is
  accent-filled, the dimmed state drops opacity. Both states **snap**.
- Cards sit on `bg-surface` with hairline borders. This is a grid of data,
  not a feature-card row; equal visual weight across all six, no icons.
- The card is not a link and has no hover lift. The only affordances are the
  disclosure and the tags.
