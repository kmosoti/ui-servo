# Why the deployment is shaped this way

Background reading, not instructions. If you are mid-deploy you want
[the how-to](../../deploy/README.md); if you need a value you want
[the reference](../reference/deployment.md). This is the document for
understanding *why* those two say what they say — the kind of thing worth
reading away from the machine.

## The site does not need a server

ui-servo has two runnable shapes, and choosing between them turned out to be
the single most consequential decision in the deployment.

The **axum origin** renders pages per request. The **static export**
(`cargo run --bin export`) writes the whole site to disk once. Both serve the
same bytes — there is an export gate asserting exactly that. So the question
looks like a preference. It isn't, for two reasons.

**The origin cannot be offline-capable.** `site/src/server.rs` serves the
service-worker template with its version token replaced by the literal string
`"dev"`, and `sw.js` sets `EPHEMERAL = VERSION === 'dev'`, which makes the
install handler skip precaching entirely. That is *correct* for a development
server: a cache-first worker on a fixed version would hide every asset edit
behind it. But it is unconditional, so it is also what a deployed binary
serves. Measured in Chromium: the origin yields a registered, active worker
with an empty cache; the export yields fifteen entries and survives an offline
reload. The exporter additionally ships `/404.html`, `/projects` and `/writing`,
which the origin answers 404 and 308 for — and since `cache.addAll()` rejects
wholesale on any non-2xx, that 404 alone would empty the cache.

**The origin is four to eight times slower**, because it brotli-compresses
every response on every request. Precompressing at deploy time is not only
free at request time, it produces *smaller* files: quality 11 is affordable
when you pay for it once, so `portfolio.js` lands at 14 514 bytes instead of
17 258.

Put together: the static shape is faster, smaller, offline-capable, and it
removes a long-lived process from the box. The origin stays useful for local
development, where its ephemeral worker is the behaviour you want.

The corollary is that **wasm has nothing to offer here**. Compiling the server
to `wasm32-wasip1` fails at the dependency level — tokio refuses to build `net`
on wasm, and axum needs a `TcpListener` — but that error is beside the point.
The static export already removed all per-request work; there is nothing left
to move to a faster runtime. Wasm earns its place in this project exactly where
it already is: `site/islands/`, doing client-side work on `/about`.

## Disk is the constrained resource, not RAM or CPU

The intuition for a 512 MB droplet is that memory will be tight. It isn't. The
whole stack peaks at 127.7 MB under synthetic saturation, and one vCPU sustains
around 1 900 requests per second of precompressed static content — orders of
magnitude beyond what a portfolio sees.

The pressure is on disk, and it comes from the one endpoint that accepts
writes. `POST /beacon` is unauthenticated by design: the probe is
fire-and-forget and has no credential to offer. The router caps body size,
event count, payload depth and field length, and validates the turn id because
it becomes a filename. What none of that caps is *stored bytes*, and the store
amplifies: a body of minimal events becomes 132.8 bytes per stored signal, so
roughly ten bytes on disk for every byte accepted.

That turns the body cap and the write rate into the real disk policy. At a
32 KB cap and twelve writes a minute, one address can write 4.3 GiB a day and
fill a 10 GB droplet in under two days. At 8 KB and six writes a minute it is
0.67 GiB a day. Neither number is safe on its own — which is why retention
prunes by age *and* by a size ceiling, hourly rather than daily, so the worst
case between runs is about 28 MB rather than most of a gigabyte.

Amplification is actually *worse* below 32 KB (10.2× against 8.2×), because
`MAX_EVENTS` truncation stops capping the output. What falls is the absolute
rate, and that is the number that matters.

## The ingest costs nothing when nobody is beaconing

A telemetry sink on a 512 MB box spends almost all its life idle. Left running
it holds **76–84 MB** — about 15% of the droplet — to do nothing at all, and it
does not give that back: after a 15-second burst at ~890 requests per second,
it returned half a megabyte and kept the rest.

The instinct is to cap it harder. That does not work, and the way it fails is
worth knowing. `MemoryHigh` is a *soft* limit: above it the kernel puts the
cgroup under sustained reclaim pressure rather than killing anything. Set below
the real working set — 48 MB against a 76 MB baseline — the process does not
become smaller, it becomes wedged. It never finished starting, the port never
opened, and the memory reading sat at a plausible-looking 57 MB that was in
fact a process thrashing rather than serving. A cap can bound a workload; it
cannot shrink one.

The workable answer is not to run it. systemd holds the port; the first beacon
starts the app; five minutes after the last one it stops again:

| State | Cost |
| --- | --- |
| Idle (the usual case) | **0** — the unit is inactive, the socket holds the port |
| Serving | 76–84 MB, bounded by `MemoryHigh=96M` / `MemoryMax=128M` |
| First beacon after idle | ~1.3 s |
| Subsequent beacons | ~19 ms |

The cold start is the price, and it is the right thing to trade: a beacon is
fire-and-forget, so nothing is waiting on it, while the memory it frees is
memory the site can use. `MemoryHigh` still has a job — it sits *above* the
working set now, so it bites on genuine growth instead of on normal operation.

Two details are load-bearing and both fail silently. `StopWhenUnneeded=` is a
`[Unit]` option; in `[Service]` systemd logs "Unknown key name … ignoring" and
carries on, so the backend never stops and the arrangement quietly reverts to
always-on. And granian does not implement `sd_notify`, so without an
`ExecStartPost` that waits for the health endpoint, the proxy can hand a
connection to a port that is not open yet.

## A deploy key should not be a shell

The default way to give CI access to a server is an SSH key in a secret store.
That key can then run anything the account can run — which means the secret
store now holds a shell on the production host, reachable by anyone who can
compromise a workflow run.

The alternative costs one line in `authorized_keys`. A forced command
(`command="..."` plus `restrict`) discards whatever the client asked for and
runs a gate instead, passing the original request in `$SSH_ORIGINAL_COMMAND`.
The gate allows one rsync shape into two known directories and three named
verbs, and refuses everything else with a log line.

The deploy job therefore never runs `ln`, `mv` or `rm` on the droplet. It
uploads to a staging directory and asks for `ui-servo-activate <sha>`. If the
job were compromised, the attacker can publish a site and flip a symlink; they
cannot execute commands as the deploy user.

Two details carry more weight than they look like they do. The gate refuses any
`rsync --server` carrying `-e`/`--rsh`, because that is precisely how a
restricted rsync is talked into spawning a shell. And the destination must
match a known directory literally, which is what stops
`/opt/ui-servo/../../root/`.

The residual risk is honest and structural: anyone with write access to the
repository can dispatch a deploy, and the gate will faithfully publish whatever
they staged. Automation cannot remove that; required reviewers on the
`production` environment is the control for it.

## Why releases are directories and `current` is a symlink

A deploy that rsyncs over the live directory serves a half-written site for the
duration of the transfer. Uploading to a staging directory and swapping a
symlink makes the change instantaneous — but only if the swap itself is atomic,
and `ln -sfn` onto an existing symlink is not. `rename(2)` is, so the swap is
`ln` into a temporary name followed by `mv -T`. Two hundred requests fired
across a live swap produced zero failures.

Keeping the last five releases on disk means rollback is a symlink move rather
than a rebuild, which matters most at the moment you least want to be waiting
for a compiler.

## What kept going wrong, and the pattern in it

Five claims in earlier drafts of this documentation were confidently wrong, and
they failed the same way: a plausible number that nobody had made the system
actually produce.

- **"A 1 GB droplet cannot compile this."** Asserted from habit. The build
  completes at a 900 MB ceiling with 432 MiB to spare — and, later, at a 390 MB
  ceiling too. The follow-up correction matters as much: `memory.peak` counts
  reclaimable page cache, so even the measured 467.7 MiB was an upper bound on
  appetite rather than a requirement.
- **The Caddy refusals never executed.** Caddy sorts directives by its own
  precedence, not file order, and a catch-all `handle` outranked every
  `respond` and the rate limiter. Every curl check passed anyway, because the
  origin returns its own 404 for `/.env` and its own 405 for `PUT`. The tests
  now kill the origin and re-run: a refusal that does not survive its backend
  was never a refusal.
- **The first throughput figures.** Measured against a rate limiter lifted to
  100 000 000 events, which allocates a ring buffer per key — costing 500 MB of
  RSS and half the throughput. The load driver also counted 429s as successful
  responses, which is why the numbers looked plausible.
- **The ingest documented at 34.7 MB.** Measured on granian's supervisor PID,
  which forks a worker and holds nothing itself. The figure never moved under
  any load — which should have been the tell — and the real number is 76–84 MB.
  The stack total was always a cgroup measurement and was never affected.
- **The runbook quoting a config it no longer matched.** It carried a copy of
  the Caddyfile in prose; the real file changed; the page went on quoting a
  32 KB cap and a rate limit of 12 that existed nowhere. Nobody edited it
  wrongly — the copy was wrong the moment the original moved.

The first four are arguments for testing the specific claim rather than a
neighbouring one. The fifth is an argument about documentation architecture,
and it is why [the reference](../reference/deployment.md) is now generated from
the deployment files rather than written alongside them, with a `--check` mode
in CI so it cannot drift again quietly.
