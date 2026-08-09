# Part: experience-log

Read `_conventions.md` first. Work history rendered as an append-only log —
each job is an entry with an `offset N` chip, newest at the top, framed by
the eyebrow "experience — an append-only log".

Content: the prototype's `jobs` array is the source of record — four
entries (Netbuilder/JPMC LogA Platform; Data Annotation Tech; AWS; NCR
Voyix/ServiceLink), with their roles, date ranges, body copy and tag lists.
Dates and employers are facts; the bodies are owner voice ("Neither one
glamorous. Both load-bearing for everything after.") and may be compressed
but not flattened into résumé-speak.

Structure per entry:

- A `<details>` element: the `<summary>` is the row head — offset chip,
  company (bold) over role (muted), date range, a caret glyph that rotates
  via `transform` on the contract's `base` duration.
- The body: the job's paragraph, then its tag chips
  (`button.tag[data-tag]`, labels from the shared table).
- The entry carries `data-tags="…"` with all its tag keys — the crossref
  behaviour highlights (`match`, accent border) or dims entries by reading
  this attribute. Both states snap.

Rules:

- The log metaphor stays structural: offsets count from 0 at the top entry,
  the entries are visually uniform, and nothing is starred, badged or
  "featured". A log does not editorialise.
- Include the no-match line ("— no job or project backs this one yet. …")
  as a hidden element the behaviour can show; its honesty is part of the
  design.
- No timeline artwork, no connector lines, no company logos.
