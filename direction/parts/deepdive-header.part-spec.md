# Part: deepdive-header

Read `_conventions.md` first. The shared masthead of a project deep-dive
page (`/projects/blackcell`, `/projects/splunk-dashboard-studio`). One part,
two instantiations — it renders from data, and the two pages must not drift
apart stylistically.

Structure, top to bottom:

- Eyebrow: `projects / <slug>` in accent micro-type.
- Title row: the project name at display scale beside a status badge
  (`pre-alpha` / `alpha`) in accent-2 (amber) — status is a warning-class
  signal, not decoration.
- Lede: one sentence, text colour, slightly larger than body ("Local-first,
  evidence-gated control runtime for coding agents." / "Pydantic 2 compiler
  for Splunk Dashboard Studio — typed Python in, version-targeted JSON
  out.").
- One or two body paragraphs from the prototype, muted, contract measure.
  The BlackCell paragraph ("BlackCell doesn't take an agent's word that
  something worked — it makes the agent prove it, then writes the proof
  down…") is the register the whole page follows.

Rules:

- Inside a deep-dive page, naming the project's own stack is fine — that is
  documentation, not tool-dropping.
- No hero imagery, no logo, no badges beyond the one status chip.
- The interactive simulator below this header is a separate unit (the
  `<flow-sim>` island), not part of this fragment; the header must stand
  alone and read complete without it.
