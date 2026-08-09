# Part: project-ledger

Read `_conventions.md` first. The personal project grid, framed by the
eyebrow "projects — the personal ledger" and the heading "What's actually
running". 3-up grid, 1-up narrow.

Content: the prototype's `projects` array is the source of record — six
projects (BlackCell, PraxisLedger, splunk-dashboard-studio, SAI,
learning-os, Kernform) with descriptions, statuses, repo links and tags.

Structure per card:

- Name (bold) with a status badge on the same row. Status strings are
  facts: `pre-alpha`, `early bootstrap`, `alpha`, `shipped`, `active`.
  `shipped` renders in accent; everything else is muted with a hairline
  border. Statuses may only change when reality does — a ledger that
  rounds "alpha" up to "shipped" is cooking the books.
- Description (muted, small), then tag chips (`button.tag[data-tag]`),
  then `view repo →` when a link exists. SAI and Kernform have no public
  repo: render no link, and never a dead `#` placeholder.
- The card carries `data-tags="…"`; match/dim behaviour and snap rules as
  in the experience-log.

Rules:

- Cards are `bg-surface` + hairline, equal weight; the ledger framing means
  no hero project, no screenshots, no github stars.
- BlackCell and splunk-dashboard-studio will later deep-link to their
  `/projects/*` pages in addition to their repos; the card layout should not
  need to change to accommodate that second link.
