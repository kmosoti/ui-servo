# Part: telemetry (the "live dummy dashboard")

Read `_conventions.md` first. A four-tile dashboard strip under the
cred-row: rate, errors, p95, session uptime.

The joke is load-bearing and must survive: three tiles are fabricated for
the aesthetic and the copy **says so in place** — the prototype's note reads
"three of these are fabricated for the aesthetic; session uptime is real,
computed from when you loaded this page". A version of this part that drops
the disclosure is dishonest telemetry on a site about honest telemetry, and
fails on voice regardless of how it looks.

Rendering:

- The fragment server-renders the panel, the note, and four tiles with
  static placeholder values (`—` for the fabricated three, `00:00` for
  uptime). Live updating is a follow-up island (`<live-dash>`); the fragment
  is its no-JS fallback and must stand alone.
- Tile labels are uppercase muted micro-type; values are tabular-nums.
  The one real value (uptime) is accent; the fabricated three are text.
- Tiles sit in a 4-up grid separated by hairlines (2-up on narrow
  viewports), on `bg-surface`.
- No sparklines, no charts, no invented history. Four numbers and a
  confession.
