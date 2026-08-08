# Part: hero (real content — supersedes demo/hero.part-spec.md)

The first thing on `/` at mosoti.dev. It has to say who this is and what they
do, in the ember-terminal direction: warm dark ground, one amber primary, one
cyan counterweight, editorial serif display against plain-spoken prose.

Who this is, factually — copy may compress but not contradict:

- **Kennedy Mosoti**, Dallas–Fort Worth, TX.
- Observability platform engineer; infrastructure automation; small tools and
  agent tooling that make messy systems easier to inspect.
- Owner's rule on tools: **do not name specific tools** (no "Salt", no vendor
  names). A tool is incidental; the skill — designing automation and telemetry
  that can be inspected and trusted — is what the copy highlights.
- The site is a personal portfolio, not a brochure and not a manifesto.
- Owner's explicit voice direction: **don't make it sound ceremonial.** The old
  site staged "Make the machine admit what it is doing" as a standing thesis;
  the owner has read that framing and pushed back on the ceremony. The idea may
  survive only if it reads like a person talking about their work in passing —
  never set apart as a thesis line, a motto, or display-serif scripture. Plain
  speech beats gravitas on every axis.
- The transformation ledger (`confusion -> structure`, `hidden state ->
  telemetry`, …) is raw material, optional, and carries the same risk: as a
  monument it is ceremony; as a small aside it can work.

Requirements:
- One `<section>` root carrying `data-span-id`.
- Allowlisted utility classes only; motion through tokens or not at all.
- A display-scale name, one line of position that places him (city and craft),
  and no more than two supporting paragraphs. A hero that needs three
  paragraphs is an about page.
- It must not read as a product landing page. `bg-surface` + `border-border`
  framing is the anti-reference: a card is a component, not a person.
- No invented employers, titles, or locations. "Nairobi" appearing anywhere is
  an automatic gate failure — that was demo copy, and this part is the reason
  the round is being re-run.
