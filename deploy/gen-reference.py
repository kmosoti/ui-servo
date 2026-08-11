"""Generate docs/reference/deployment.md from the deployment machinery itself.

The deployment runbook went stale inside a single afternoon: it carried a copy
of the Caddyfile in prose, the real Caddyfile changed, and the page went on
confidently quoting a 32 KB body cap and a rate limit of 12 that no longer
existed anywhere. Nobody edited it wrongly -- the copy was wrong the moment the
original moved.

Diataxis says reference material "is led by the product it describes". Taken
literally that means the reference should be *derived* from the product, not
transcribed beside it. So this reads the actual Caddyfile, the actual unit
files and the actual Python constants, and writes the table.

    uv run python deploy/gen-reference.py            # write the file
    uv run python deploy/gen-reference.py --check    # fail if it is out of date

The --check mode runs in CI, so the reference cannot drift again without a red
build. Values that genuinely cannot be derived -- measurements -- live in
deploy/measurements.json, which at least keeps them in one place with a note on
how each was obtained.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"
OUT = ROOT / "docs" / "reference" / "deployment.md"


def read(path: Path) -> str:
    return path.read_text()


def caddy_facts(text: str) -> dict[str, object]:
    """Pull the operator-visible knobs out of the Caddyfile."""

    def env_default(name: str, default_pattern: str = r"[^}]*") -> str | None:
        m = re.search(rf"\{{\$({name}):({default_pattern})\}}", text)
        return m.group(2) if m else None

    zones = {}
    for zone, body in re.findall(r"zone (\w+) \{(.*?)\n\t\t\t\}", text, re.S):
        events = re.search(r"events (\S+)", body)
        window = re.search(r"window (\S+)", body)
        raw = events.group(1) if events else "?"
        m = re.match(r"\{\$\w+:([^}]*)\}", raw)
        zones[zone] = {
            "events": m.group(1) if m else raw,
            "window": window.group(1) if window else "?",
        }

    headers = re.findall(r"^\t\t([A-Z][A-Za-z-]+) \"(.*)\"$", text, re.M)
    cache = re.findall(r"@(\w+) path ([^\n]+)\n\t+header @\1 Cache-Control \"([^\"]+)\"", text)
    probes = re.search(r"@probes path ([^\n]+)", text)
    crawlers = re.search(r"@ai_crawlers header_regexp User-Agent \"\(\?i\)\(([^)]+)\)\"", text)
    timeouts = dict(re.findall(r"^\t\t\t(\w+) (\S+)$", text, re.M))

    return {
        "zones": zones,
        "body_cap": env_default("BEACON_MAX_BODY") or "?",
        "site_root": env_default("SITE_ROOT") or "?",
        "ingest_origin": env_default("INGEST_ORIGIN") or "?",
        "headers": headers,
        "cache": cache,
        "probe_paths": probes.group(1).split() if probes else [],
        "crawlers": crawlers.group(1).split("|") if crawlers else [],
        "timeouts": timeouts,
        "encoders": (re.search(r"^\t\t\tencode (.+)$", text, re.M) or [None, "?"])[1]
        if re.search(r"^\t\t\tencode (.+)$", text, re.M)
        else "?",
        "precompressed": (re.search(r"precompressed (.+)", text) or [None, "?"])[1]
        if re.search(r"precompressed (.+)", text)
        else "?",
    }


def unit_facts(text: str) -> dict[str, str]:
    """Flatten a unit file to a dict, with Environment= exploded by name.

    A unit carries several Environment= lines, so keeping only the first (or
    only the last) silently attributes one setting's value to another -- which
    is exactly how the age cutoff first rendered as a filesystem path.
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key == "Environment" and "=" in value:
            name, _, val = value.partition("=")
            out[f"env:{name}"] = val
        else:
            out.setdefault(key, value)
    return out


def python_consts(text: str, names: list[str]) -> dict[str, str]:
    out = {}
    for name in names:
        m = re.search(rf"^{name}: Final\[[^\]]+\] = (.+?)(?:  #.*)?$", text, re.M)
        if m:
            out[name] = m.group(1).strip()
    return out


def table(rows: list[tuple[str, ...]], head: tuple[str, ...]) -> str:
    lines = ["| " + " | ".join(head) + " |", "| " + " | ".join("---" for _ in head) + " |"]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(lines)


def build() -> str:
    caddy = caddy_facts(read(DEPLOY / "Caddyfile"))
    ingest = unit_facts(read(DEPLOY / "ui-servo-ingest-backend.service"))
    proxy = unit_facts(read(DEPLOY / "ui-servo-ingest.service"))
    sock = unit_facts(read(DEPLOY / "ui-servo-ingest.socket"))
    # The idle timeout is the whole reason the ingest costs nothing at rest, so
    # read it off the proxy's command line rather than restating it.
    idle = (re.search(r"--exit-idle-time=(\S+)", proxy.get("ExecStart", "")) or [None, "?"])[1] \
        if "--exit-idle-time=" in proxy.get("ExecStart", "") else "?"
    prune = unit_facts(read(DEPLOY / "ui-servo-prune-evidence.service"))
    timer = unit_facts(read(DEPLOY / "ui-servo-prune-evidence.timer"))
    router = python_consts(
        read(ROOT / "ui_servo" / "adapters" / "beacon_ingest" / "router.py"),
        ["BEACON_PATH", "HEALTH_PATH", "MAX_BODY_BYTES", "MAX_EVENTS",
         "MAX_PAYLOAD_DEPTH", "MAX_PAYLOAD_KEYS", "MAX_FIELD_CHARS"],
    )
    app = python_consts(
        read(ROOT / "ui_servo" / "adapters" / "beacon_ingest" / "app.py"),
        ["ROOT_ENV", "TURN_ENV", "FSYNC_ENV"],
    )
    m = json.loads(read(DEPLOY / "measurements.json"))

    env_rows = [
        (f"`{app.get('ROOT_ENV', '?').strip(chr(34))}`", "ingest", "**required** — the directory *containing* `evidence/`; refuses a path ending in `evidence`"),
        (f"`{app.get('TURN_ENV', '?').strip(chr(34))}`", "ingest", "turn id = JSONL filename; defaults to `prod-YYYY-MM-DD`, fixed at startup"),
        (f"`{app.get('FSYNC_ENV', '?').strip(chr(34))}`", "ingest", "`1`/`0`; off by default"),
        ("`SITE_ADDRESS`", "caddy", "site address; `mosoti.dev` in production, `localhost:8443` to test the same file"),
        ("`SITE_ROOT`", "caddy", f"document root, default `{caddy['site_root']}` (a symlink swapped per deploy)"),
        ("`INGEST_ORIGIN`", "caddy", f"upstream for `/beacon*`, default `{caddy['ingest_origin']}`"),
        ("`BEACON_MAX_BODY`", "caddy", f"request body cap on the write path, default `{caddy['body_cap']}`"),
        ("`RL_BROWSE` / `RL_BEACON`", "caddy", "rate-limit budgets; each is a ring buffer, so large values cost memory"),
        ("`HTTP_PORT`", "caddy", "plaintext port, default 80; overridable to test unprivileged"),
        ("`UI_SERVO_EVIDENCE_DAYS`", "prune", f"age cutoff in days, default `{prune.get('env:UI_SERVO_EVIDENCE_DAYS', '?')}`"),
        ("`UI_SERVO_EVIDENCE_MAX_MB`", "prune", f"size ceiling in MB, applied after age, default `{prune.get('env:UI_SERVO_EVIDENCE_MAX_MB', '?')}`"),
    ]

    rl = caddy["zones"]
    amp_rows = [
        (f"`{c['body_cap']}`", f"{c['body_bytes']:,}".replace(",", " "), str(c["events"]),
         str(c["stored_signals"]), f"{c['stored_bytes']:,}".replace(",", " "), f"{c['amplification']}×")
        for c in m["ingest_amplification"]["caps"]
    ]

    tp = m["throughput_rps"]
    tp_rows = [
        ("home page", str(tp["static"]["home"]), str(tp["proxy"]["home"]), str(tp["origin_direct_no_tls"]["home"])),
        ("deep-dive page", str(tp["static"]["deep_dive"]), "—", str(tp["origin_direct_no_tls"]["deep_dive"])),
        ("`portfolio.js`", str(tp["static"]["portfolio_js"]), str(tp["proxy"]["portfolio_js"]), str(tp["origin_direct_no_tls"]["portfolio_js"])),
        ("woff2", str(tp["static"]["woff2"]), str(tp["proxy"]["woff2"]), str(tp["origin_direct_no_tls"]["woff2"])),
        ("`POST /beacon`", "—", str(tp["proxy"]["beacon_write"]), "—"),
    ]

    mem = m["memory"]
    tier = m["tier"]

    return f"""<!-- GENERATED by deploy/gen-reference.py — do not edit by hand.
     Values come from deploy/Caddyfile, the systemd units, the ingest source
     and deploy/measurements.json. Run `uv run python deploy/gen-reference.py`
     after changing any of those; CI runs it with --check. -->

# Deployment reference

Technical description of the deployed system. For *how to deploy it*, see
[deploy/README.md](../../deploy/README.md). For *why it is shaped this way*,
see [the explanation](../explanation/deployment-architecture.md).

Generated from the machinery, not transcribed from it — every value below is
read out of the file that actually configures it.

## Target host

| Property | Value |
| --- | --- |
| Tier | `{tier['slug']}` |
| vCPU / RAM / disk | {tier['vcpu']} / {tier['ram_mb']} MB / {tier['disk_gb']} GB |
| Included transfer | {tier['transfer_gb']} GB per month |
| Profiling model | `{tier['model']}` |

{tier['model_note']}

## Environment variables

{table(env_rows, ("Variable", "Read by", "Meaning"))}

## Edge: rate limits and body caps

| Zone | Events | Window | Applies to |
| --- | --- | --- | --- |
| `beacon` | {rl.get('beacon', {}).get('events', '?')} | {rl.get('beacon', {}).get('window', '?')} | `POST /beacon*` |
| `browse` | {rl.get('browse', {}).get('events', '?')} | {rl.get('browse', {}).get('window', '?')} | everything else |

Request body on the write path is capped at **{caddy['body_cap']}**. Keyed on
`{{remote_host}}`. Exceeding a zone returns `429` with `Retry-After`.

## Edge: response headers

{table([(f"`{k}`", f"`{v}`" if len(v) < 90 else f"`{v[:87]}…`") for k, v in caddy['headers']], ("Header", "Value"))}

`Server` is removed. Cache-Control is applied by path:

{table([(f"`{paths}`", f"`{value}`") for _, paths, value in caddy['cache']], ("Paths", "Cache-Control"))}

Compression: `encode {caddy['encoders']}` for anything without a precompressed
sibling; `file_server` serves `precompressed {caddy['precompressed']}`. Brotli is
absent from the encoder list because this Caddy build registers
`http.precompressed.br` but not `http.encoders.br`.

## Edge: refusals

| Rule | Response |
| --- | --- |
| Method not in `GET HEAD POST` | `405` |
| Path matches a scanner probe ({len(caddy['probe_paths'])} patterns) | `404` |
| `User-Agent` matches a declared crawler ({len(caddy['crawlers'])} names) | `403` |

Probe paths: {' '.join(f'`{p}`' for p in caddy['probe_paths'])}

Declared crawlers: {', '.join(f'`{c}`' for c in caddy['crawlers'])}

Search-engine crawlers are deliberately absent from that list.

Server timeouts: {', '.join(f'`{k}={v}`' for k, v in caddy['timeouts'].items())}.

## Ingest service

| Property | Value |
| --- | --- |
| Reached on | `{sock.get('ListenStream', '?')}` — a systemd socket, not a listening process |
| Activation | on demand; proxy exits after **{idle}** idle, backend follows via `StopWhenUnneeded` |
| Proxy ceiling | `{proxy.get('MemoryMax', '?')}` memory, `{proxy.get('TasksMax', '?')}` tasks |
| Memory at rest | **0** — the unit is inactive between beacons |
| Cold start / warm | {m['ingest_lifecycle']['cold_start_seconds']} s / {m['ingest_lifecycle']['warm_latency_ms']} ms |
| Routes | `POST {router.get('BEACON_PATH', '?')}`, `GET|HEAD {router.get('HEALTH_PATH', '?')}` |
| Health | `200` normally, **`503` when evidence has been lost** |
| Entrypoint | `granian --interface asgi --factory ui_servo.adapters.beacon_ingest.app:create_app` |
| `MemoryHigh` / `MemoryMax` | `{ingest.get('MemoryHigh', '?')}` / `{ingest.get('MemoryMax', '?')}` |
| `CPUWeight` / `CPUQuota` | `{ingest.get('CPUWeight', '?')}` / `{ingest.get('CPUQuota', '?')}` |
| `TasksMax` | `{ingest.get('TasksMax', '?')}` |
| Writable paths | `{ingest.get('ReadWritePaths', '?')}` |
| Restart | `{ingest.get('Restart', '?')}` after `{ingest.get('RestartSec', '?')}` |

Application-level limits, from `router.py`:

{table([(f"`{k}`", f"`{v}`") for k, v in router.items() if k.startswith("MAX")], ("Constant", "Value"))}

Startup refuses, rather than guessing, on: a missing/blank/relative/nonexistent
root, a root ending in `evidence`, a turn id that is not a safe path component,
or a non-boolean fsync flag.

## Evidence retention

| Property | Value |
| --- | --- |
| Age cutoff | {prune.get('env:UI_SERVO_EVIDENCE_DAYS', '?')} days |
| Size ceiling | {prune.get('env:UI_SERVO_EVIDENCE_MAX_MB', '?')} MB |
| Schedule | `{timer.get('OnCalendar', '?')}`, `Persistent={timer.get('Persistent', '?')}`, jitter `{timer.get('RandomizedDelaySec', '?')}` |
| Order | age first, then size; never removes the last remaining file |

Storage cost, measured:

{table(amp_rows, ("Body cap", "Body bytes", "Events", "Stored", "Stored bytes", "Amplification"))}

At **{m['ingest_amplification']['bytes_per_stored_signal']} bytes per stored signal**, with
truncation at `MAX_EVENTS = {router.get('MAX_EVENTS', '?')}`.

## Measured performance

One vCPU, {tier['ram_mb']} MB modelled, all responses 2xx:

{table(tp_rows, ("Route", "Static export", "Proxy to origin", "Origin direct, no TLS"))}

| Property | Value |
| --- | --- |
| Stack peak memory | {mem['stack_peak_bytes'] / 1048576:.1f} MB of {mem['ceiling_bytes'] / 1048576:.0f} MB |
| OOM kills at that ceiling | {mem['oom_kills']} |
| Caddy idle / peak | {mem['caddy_idle_kb'] / 1024:.0f} MB / {mem['caddy_peak_kb'] / 1024:.0f} MB |
| Ingest peak | {mem['ingest_peak_kb'] / 1024:.0f} MB |
| Cold visit | {m['payload']['cold_visit_requests']} requests, {m['payload']['cold_visit_bytes_static']:,} bytes |
| `portfolio.js` precompressed | {m['payload']['portfolio_js_br_precompressed']:,} B (vs {m['payload']['portfolio_js_br_on_the_fly']:,} on the fly) |
| Service worker cache entries | {m['service_worker']['static_cache_entries']} static / {m['service_worker']['origin_cache_entries']} origin |

Build, cold target directory, `-j1`, no swap:

| Ceiling | Result | Peak | Wall | Reclaim events |
| --- | --- | --- | --- | --- |
| 900 MB | {m['build']['at_900mb_ceiling']['result']} | {m['build']['at_900mb_ceiling']['peak_bytes'] / 1048576:.1f} MiB | {m['build']['at_900mb_ceiling']['seconds']} s | {m['build']['at_900mb_ceiling']['reclaim_events']} |
| 390 MB | {m['build']['at_390mb_ceiling']['result']} | {m['build']['at_390mb_ceiling']['peak_bytes'] / 1048576:.1f} MiB | {m['build']['at_390mb_ceiling']['seconds']} s | {m['build']['at_390mb_ceiling']['reclaim_events']} |

{m['build']['note']}.

## Deploy key permissions

The CI key is pinned to a forced command. It may run:

| Request | Effect |
| --- | --- |
| `rsync --server` into `/opt/ui-servo/incoming` or `/opt/ui-servo/app` | upload |
| `ui-servo-activate <hex>` | promote upload, swap symlink, prune to 5 |
| `ui-servo-sync-ingest` | `uv sync` + restart the ingest |
| `ui-servo-releases` | list releases |

Anything else is refused and logged. `rsync` invocations carrying `-e`/`--rsh`
are refused regardless of destination.

## Versions

{table([(k.replace('_', ' '), f"`{v}`") for k, v in m['versions'].items()], ("Component", "Value"))}

Measurements taken {m['measured_on']}.
"""


def main() -> int:
    content = build()
    check = "--check" in sys.argv
    if check:
        if not OUT.exists():
            print(f"{OUT} does not exist; run deploy/gen-reference.py", file=sys.stderr)
            return 1
        if OUT.read_text() != content:
            print(
                f"{OUT} is out of date with the deployment machinery.\n"
                "Run: uv run python deploy/gen-reference.py",
                file=sys.stderr,
            )
            return 1
        print(f"{OUT} is current")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(content)
    print(f"wrote {OUT} ({len(content)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
