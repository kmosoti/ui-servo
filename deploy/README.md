# Deploy ui-servo to a droplet

Directions for getting the site live and keeping it there. Task-oriented: no
explanation, no measurements, no reasoning. Those live elsewhere and are linked
where they become relevant.

- **Why it is built this way** — [explanation](../docs/explanation/deployment-architecture.md)
- **What every value is** — [reference](../docs/reference/deployment.md) (generated; always current)

Target: `s-1vcpu-512mb-10gb`, Ubuntu 24.04, one domain, one write endpoint.

**Two accounts, and the difference matters for every command below.** `admin`
is you: a normal shell with full sudo, holding your key. `deploy` is CI: its
key is pinned to a forced command, it has no shell, and its one sudo verb stops
the ingest. Administer as `admin`; `DEPLOY_USER` in the CI secrets stays
`deploy`. Root login is disabled once the init script runs.

---

## Before you start

The target is **`kennedy.mosoti.dev`**, which already resolves to the droplet
(`142.93.206.223`) — confirmed against Cloudflare, Google and Quad9, so Let's
Encrypt will be able to resolve it too. `portfolio.mosoti.dev` points at the
same droplet; delete it, or add it to `SITE_EXTRA_NAMES` if you want both to
serve. A name Caddy is told about but that does not resolve is a certificate it
retries forever. The apex `mosoti.dev` has been deliberately retired — it is
no longer in the DNS zone and nothing serves it. That removes the whole
migration problem: there is no live traffic to protect, no TTL to wait out and
no cutover window. Deploy straight to the subdomain.

You need:

- The droplet, Ubuntu 24.04, with your SSH key from the DO panel.
- `gh` authenticated against `kmosoti/ui-servo`.
- The `production` environment:
  `gh api --method PUT repos/kmosoti/ui-servo/environments/production`

### Open 80 and 443 first

This is the one thing that blocks everything else. The cloud firewall currently
allows only SSH; there are no inbound rules for HTTP at all.

| Type | Protocol | Port | Source |
| --- | --- | --- | --- |
| HTTP | TCP | 80 | **All IPv4, All IPv6** |
| HTTPS | TCP | 443 | **All IPv4, All IPv6** |
| SSH | TCP | 22 | your IP — leave as is |

Port 80 cannot be restricted to your address. The ACME HTTP-01 challenge is
Let's Encrypt connecting *inbound* on port 80 from a set of addresses it does
not publish, so a source restriction there means the certificate never issues
and Caddy retries forever.

> **A freshly created record can look broken locally.** A resolver that was
> asked for the name *before* it existed caches the NXDOMAIN for the zone's SOA
> minimum — 1800 s here — and under RFC 8020 may return NXDOMAIN for everything
> beneath the domain. If `curl` says "could not resolve" while public resolvers
> answer fine, wait it out rather than changing DNS again.

> **The DO zone has no MX records.** DigitalOcean's own banner warns about
> this: mail for `mosoti.dev` is unrouted until MX records are recreated. Only
> a problem if the domain was carrying email.

---

## First deploy

About 15 minutes. The DNS is already correct, so nothing here waits on it.

### 1. Make the CI key

On a machine you trust, and never reuse a personal key:

```sh
ssh-keygen -t ed25519 -C 'github-actions ui-servo deploy' -f ci_deploy -N ''
gh secret set DEPLOY_SSH_KEY < ci_deploy
cat ci_deploy.pub            # you will paste this into the startup script
shred -u ci_deploy           # keep no local copy
```

### 2. Build the startup script

```sh
./deploy/build-cloud-init.sh > cloud-init.generated.sh
```

Set the values at the top: `DEPLOY_PUBKEY` (the `.pub` from step 1),
`SITE_DOMAIN=kennedy.mosoti.dev`, `ACME_EMAIL`. Leave `SITE_EXTRA_NAMES`
empty — `www.kennedy.mosoti.dev` does not exist, and listing a name that
does not resolve gives Caddy a certificate it can never obtain.

It refuses to run with the placeholder key still in place.

**If the droplet already exists** and you did not paste this at creation, run
it by hand instead — an initialization script only runs on first boot:

```sh
scp cloud-init.generated.sh root@142.93.206.223:/tmp/
ssh root@142.93.206.223 'bash /tmp/cloud-init.generated.sh'
```

Check first, so you do not run it twice:
`ssh root@142.93.206.223 'ls -l /var/log/ui-servo-init.log'`

### 3. Provision the droplet

For a new droplet: Ubuntu 24.04 LTS, Basic / Regular, `s-1vcpu-512mb-10gb`,
your SSH key from the DO panel, and `cloud-init.generated.sh` pasted into **Add
Initialization scripts**. For the existing one, see the manual run above.

Either way, read the log — this is the step people skip:

```sh
ssh admin@142.93.206.223 'sudo tail -30 /var/log/ui-servo-init.log'
```

It ends by printing the host-key line you need next. If it ends anywhere else,
stop and fix that before continuing.

### 4. Finish the secrets

```sh
gh secret set DEPLOY_KNOWN_HOSTS   # the line the init log printed
gh secret set DEPLOY_HOST          # 142.93.206.223
gh secret set DEPLOY_USER          # deploy  <- the CI account, not admin
```

### 5. Ship it

`kennedy.mosoti.dev` already points at the droplet, so there is no cutover.
Set the last secret and deploy:

```sh
gh secret set DEPLOY_SITE_URL      # https://kennedy.mosoti.dev
```

Push to `main`, or Actions → **deploy** → *Run workflow*. The job exports,
precompresses, uploads, swaps the symlink and then verifies the live site by
comparing the served service-worker version against the one it just built.

Caddy requests the certificate on the first request. Watch it:

```sh
ssh admin@142.93.206.223 'sudo journalctl -u caddy -f'
```

### 6. Confirm the resting state

```sh
curl -sI https://kennedy.mosoti.dev/ | head -1
ssh admin@142.93.206.223 'systemctl is-active ui-servo-ingest.socket ui-servo-ingest-backend.service'
# active
# inactive   <- correct: the ingest starts on the first beacon
```

Lower the `kennedy.mosoti.dev` TTL from 3600 to 300 while you are still
iterating; an hour is a long time to be stuck with a record you want back.

---

## Deploy a change

Push to `main`. That is the whole procedure.

The job only touches the ingest service if `ui_servo/`, `pyproject.toml` or
`uv.lock` changed; a site-only change restarts nothing.

To deploy without pushing — Actions → **deploy** → *Run workflow*, optionally
with a commit SHA.

## Roll back

Releases stay on disk, so this needs no rebuild.

```sh
ssh admin@142.93.206.223 'sudo -u deploy ui-servo-releases'
ssh admin@142.93.206.223 'sudo -u deploy ui-servo-activate <old-sha>'
```

Or Actions → **deploy** → *Run workflow* with the older SHA.

## Change what the edge allows

Edit `deploy/Caddyfile`, then:

```sh
uv run python deploy/gen-reference.py    # keep the reference in step
scp deploy/Caddyfile admin@142.93.206.223:/tmp/Caddyfile
ssh admin@142.93.206.223 'sudo install -m 644 /tmp/Caddyfile /etc/caddy/Caddyfile \
  && sudo caddy validate --config /etc/caddy/Caddyfile \
  && sudo systemctl reload caddy'
```

`caddy validate` before `reload`, always: an invalid config fails the reload
and leaves the old one running, but only if you gave it the chance to.

CI fails if you change a value here without regenerating the reference.

> **If you add a refusal, test it with the backend stopped.** A rule that never
> fires still looks like it works, because the backend returns similar status
> codes on its own. The explanation has the story.

## Investigate a filling disk

```sh
ssh admin@142.93.206.223 '
  du -sh /var/lib/ui-servo/evidence
  ls -lt /var/lib/ui-servo/evidence | head
  systemctl list-timers ui-servo-prune-evidence
  curl -s localhost:8111/beacon/health
'
```

In order of likelihood: the prune timer stopped; someone is writing at the rate
limit; the ingest is losing evidence (health returns **503**, not 200).

The ingest being `inactive` is not a fault — it starts on the next beacon.

Prune immediately: `sudo systemctl start ui-servo-prune-evidence`.

To tighten the intake, lower `BEACON_MAX_BODY` and the `beacon` zone's `events`
in the Caddyfile — those two set the ceiling on how fast anyone can fill the
disk. The [explanation](../docs/explanation/deployment-architecture.md) has the
arithmetic.

## Update the droplet's helper scripts

`cloud-init.sh` installs the `/usr/local/bin/ui-servo-*` helpers **once**, at
provisioning. Editing them here changes nothing on a droplet that already
exists — the deploy uploads only the site and the ingest app, and the SSH gate
runs whatever copy was installed. That is how a droplet kept running
`uv sync --frozen` after `--no-dev` had been committed.

```sh
./deploy/update-helpers.sh admin@142.93.206.223
```

It compares before installing, so a no-op run changes nothing and says so.

> **This is deliberately not part of the deploy workflow.** The deploy key is
> pinned to a forced command precisely so CI cannot run arbitrary code as
> `deploy`; a workflow step that rewrote `/usr/local/bin` could replace
> `ui-servo-ssh-gate` itself, which is the file the whole arrangement rests on.
> Helper updates are an administrator action, over the admin account.

## Rotate the deploy key

```sh
ssh-keygen -t ed25519 -C 'github-actions ui-servo deploy' -f ci_deploy -N ''
gh secret set DEPLOY_SSH_KEY < ci_deploy
ssh admin@142.93.206.223   # the CI key cannot get a shell; yours can
```

Replace the `command="..."` line in `~deploy/.ssh/authorized_keys`, keeping the
prefix exactly:

```
restrict,command="/usr/local/bin/ui-servo-ssh-gate" ssh-ed25519 AAAA... ci-deploy
```

Then `shred -u ci_deploy` and re-run the workflow to confirm.

## Locked out — every port refuses instantly

Symptom: 22, 80 and 443 all give **"Connection refused" in under a tenth of a
second**, from your machine, on a droplet that worked minutes ago.

That combination is diagnostic. A stopped service refuses one port; a cloud
firewall *drops*, so you would wait for a timeout. An instant refusal on every
port is a host firewall rejecting your address — fail2ban, whose default action
is REJECT rather than DROP. A few mistyped passphrases or a script probing keys
is enough to trip the sshd jail.

You cannot SSH in to fix it. Use **Droplet → Access → Launch Droplet Console**,
which is out-of-band:

```sh
fail2ban-client status sshd
fail2ban-client set sshd unbanip <your-ip>
```

Or wait: the default `bantime` is 10 minutes and it lifts itself.

Then stop it recurring — `ADMIN_IP` in `cloud-init.sh` does this on new
droplets, but an existing one needs it by hand:

```sh
printf '[DEFAULT]\nignoreip = 127.0.0.1/8 ::1 <your-ip>\n' > /etc/fail2ban/jail.local
systemctl restart fail2ban
```

> **Log in as `deploy`, not `root`.** The init script sets
> `PermitRootLogin no`, so once it has run, `ssh root@…` fails with "Permission
> denied (publickey)" *even though your key is in root's `authorized_keys`* —
> which reads exactly like a key problem and is not one. Repeatedly retrying
> `root@` is itself a good way to get banned.

## Check the box is healthy

```sh
ssh admin@142.93.206.223 '
  systemctl is-active caddy ui-servo-ingest
  systemctl list-timers ui-servo-prune-evidence --no-pager
  free -m; df -h /
  curl -s localhost:8111/beacon/health
'
curl -sI https://<domain>/ | head -1
```

---

## What is in this directory

| File | Goes to | Purpose |
| --- | --- | --- |
| `cloud-init.sh` + `build-cloud-init.sh` | DO init scripts | one-shot droplet build-out |
| `Caddyfile` | `/etc/caddy/Caddyfile` | the edge. **Use this one.** |
| `Caddyfile.origin-proxy` | `/etc/caddy/Caddyfile` | proxies the axum binary instead — **loses offline support** |
| `ui-servo-ssh-gate` | `/usr/local/bin/` | forced command; the CI key can do nothing else |
| `ui-servo-activate` | `/usr/local/bin/` | promote upload, swap symlink, prune; also the rollback tool |
| `ui-servo-sync-ingest` | `/usr/local/bin/` | `uv sync` + restart the ingest |
| `ui-servo-prune-evidence` + `.service`/`.timer` | `/usr/local/bin/`, `/etc/systemd/system/` | retention, hourly |
| `ui-servo-ingest.socket` | `/etc/systemd/system/` | holds port 8111 for ~nothing |
| `ui-servo-ingest.service` | `/etc/systemd/system/` | socket proxy; exits 5 min after the last beacon |
| `ui-servo-ingest-backend.service` | `/etc/systemd/system/` | the app; started on demand, stops with the proxy |
| `precompress.py` | run in CI | `.br` siblings at quality 11 |
| `e2e-deploy-test.sh` | run in CI + locally | boots a droplet-shaped container and runs the deploy path through a real sshd |
| `update-helpers.sh` | run by an admin | refreshes the installed `/usr/local/bin` helpers and systemd units |
| `gen-reference.py` + `measurements.json` | run locally / in CI | generates the reference doc |

## Setting it up by hand

If you would rather not use the startup script, `cloud-init.sh` is the
authoritative list of steps — read it top to bottom. The parts that are easy to
get wrong:

- `UI_SERVO_INGEST_ROOT` is the directory *containing* `evidence/`. The service
  refuses to start on a path ending in `evidence`.
- The deploy key needs the `restrict,command="..."` prefix, or it is an
  unrestricted shell.
- `admin` and `deploy` are separate. Giving your key to `deploy` instead
  leaves you with login but no sudo — administrable only from the serial
  console. The script aborts rather than disable root login with no admin key.
- `deploy` needs exactly one sudo verb:
  `deploy ALL=(root) NOPASSWD: /usr/bin/systemctl stop ui-servo-ingest-backend.service`
- Enable the **socket**, never the backend. `systemctl enable ui-servo-ingest-backend`
  would defeat the whole arrangement by keeping ~80 MB resident permanently.
- `StopWhenUnneeded=yes` belongs in `[Unit]`. In `[Service]` systemd logs
  "Unknown key name … ignoring" and the backend simply never stops.
- `/var/log/caddy` must be owned by `caddy` — **and so must every file already
  in it**. `install -d -o caddy` fixes the directory and silently leaves a
  root-owned `ui-servo.log` behind, after which Caddy exits 1 at startup and
  the box answers ECONNREFUSED on 80 and 443. `caddy validate` passes
  throughout: validation never opens the log writer.
- The `caddy` user's home must be `/var/lib/caddy`, not `/nonexistent`.
  Certificates live under `$HOME/.local/share/caddy`, so a `--no-create-home`
  user serves happily on 80 and 443 while every ACME attempt fails with
  `failed storage check` — an up site that can never get a certificate.
