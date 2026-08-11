#!/bin/bash
# DigitalOcean user-data / startup script for ui-servo.
#
#   Tier this is written for: s-1vcpu-512mb-10gb, Ubuntu 24.04 LTS.
#   Paste into "Add Initialization scripts (free)" at droplet creation.
#
# It brings the box to the point where CI can deploy to it and stops. It does
# NOT fetch the site: the GitHub Actions workflow ships that, and until it runs
# the site serves a holding page rather than a 502.
#
# Runs once, as root, before you can log in. Progress goes to
# /var/log/ui-servo-init.log and the whole run is also in
# /var/log/cloud-init-output.log.
#
# ---------------------------------------------------------------------------
# SET THESE THREE BEFORE PASTING
# ---------------------------------------------------------------------------
DEPLOY_PUBKEY='ssh-ed25519 AAAA_REPLACE_ME ci-deploy'   # public half of the CI key
ADMIN_PUBKEY=''      # optional: your own key, if not already added via the DO UI
# Your own address, exempted from fail2ban. Leave empty to skip. A handful of
# mistyped-passphrase attempts is enough to trip the default sshd jail, and the
# ban REJECTs every port -- so the box appears to vanish, and the only way back
# in is the provider's web console.
# Leave empty here and fill it in on the copy you actually paste -- this file
# is public, and "the admin logs in from this address" is not something to
# publish next to the host it opens.
ADMIN_IP=''
# Must be a name that ALREADY resolves to this droplet. Caddy asks Let's
# Encrypt for a certificate on the first request, and the HTTP-01 challenge is
# Let's Encrypt connecting back to this host on port 80 -- so a name that does
# not point here yet cannot be certified, and neither can a `www.` variant that
# was never created. Deploying to a subdomain first is the safe order.
SITE_DOMAIN='kennedy.mosoti.dev'
# Extra names for the same site, space separated. Leave empty for a subdomain;
# set to "www.example.com" only when that record exists.
SITE_EXTRA_NAMES=''
ACME_EMAIL='admin@mosoti.dev'
# ---------------------------------------------------------------------------

set -euo pipefail
exec > >(tee -a /var/log/ui-servo-init.log) 2>&1
echo "=== ui-servo init starting $(date -Is)"

if [ "${DEPLOY_PUBKEY}" = 'ssh-ed25519 AAAA_REPLACE_ME ci-deploy' ]; then
	echo "FATAL: DEPLOY_PUBKEY is still the placeholder. Nothing was changed."
	exit 1
fi

# One site block, listing only names that resolve here.
SITE_NAMES="${SITE_DOMAIN}"
for extra in ${SITE_EXTRA_NAMES}; do
	SITE_NAMES="${SITE_NAMES}, ${extra}"
done
echo "serving: ${SITE_NAMES}"

export DEBIAN_FRONTEND=noninteractive

# --- 1. swap ---------------------------------------------------------------
# 512 MB with no swap has no margin for a spike. 1 GB of swap on a 10 GB disk
# is cheap insurance; swappiness=10 keeps it as a safety net rather than a
# thing the kernel reaches for during normal serving.
if ! swapon --show | grep -q .; then
	fallocate -l 1G /swapfile
	chmod 600 /swapfile
	mkswap /swapfile
	swapon /swapfile
	echo '/swapfile none swap sw 0 0' >> /etc/fstab
	sysctl -w vm.swappiness=10
	echo 'vm.swappiness=10' > /etc/sysctl.d/99-ui-servo.conf
fi

# --- 2. packages -----------------------------------------------------------
apt-get update
apt-get install -y --no-install-recommends \
	ufw fail2ban unattended-upgrades rsync curl ca-certificates \
	debian-keyring debian-archive-keyring apt-transport-https gnupg

# --- 3. users --------------------------------------------------------------
# `deploy` receives uploads; `ui-servo` runs the ingest and can never log in.
id deploy   >/dev/null 2>&1 || adduser --disabled-password --gecos "" deploy
id ui-servo >/dev/null 2>&1 || adduser --system --group --no-create-home \
	--shell /usr/sbin/nologin ui-servo

# Two accounts, because they have opposite needs. `admin` is you: a shell, full
# sudo, no restrictions. `deploy` is CI: a forced command, no shell, one sudo
# verb. Sharing one account means either the human cannot administer the box or
# the robot's key can — and the first of those is not obviously the safe
# failure, because it leaves the provider's serial console as the only way to
# fix anything.
# useradd, not adduser: an `admin` group already exists on some images, and
# adduser treats "the group I was about to create is taken" as fatal rather
# than joining it. Both branches were exercised on ubuntu:24.04.
if ! id admin >/dev/null 2>&1; then
	if getent group admin >/dev/null 2>&1; then
		useradd --create-home --shell /bin/bash --gid admin admin
	else
		useradd --create-home --shell /bin/bash --user-group admin
	fi
fi
usermod -aG sudo admin
# NOPASSWD is not laziness here, it is the only combination that works. The
# account has no password -- it logs in by key, and password auth is disabled
# below -- so plain sudo-group membership would prompt for a password that does
# not exist and can never be typed. That yields an account with full sudo on
# paper and none in practice, administrable only from the serial console.
cat > /etc/sudoers.d/ui-servo-admin <<'EOF'
admin ALL=(ALL) NOPASSWD: ALL
EOF
chmod 440 /etc/sudoers.d/ui-servo-admin
visudo -c -f /etc/sudoers.d/ui-servo-admin
install -d -m 700 -o admin -g admin /home/admin/.ssh

if [ -n "$ADMIN_PUBKEY" ]; then
	printf '%s\n' "$ADMIN_PUBKEY" > /home/admin/.ssh/authorized_keys
elif [ -s /root/.ssh/authorized_keys ]; then
	# Whatever DigitalOcean injected into root at creation — the key from the
	# control panel. Section 4 disables root login, so this copy is the only
	# thing between you and a box reachable solely by serial console.
	cat /root/.ssh/authorized_keys > /home/admin/.ssh/authorized_keys
	echo "admin: carried over $(grep -c '^ssh-' /root/.ssh/authorized_keys || echo 0) key(s) from root"
else
	echo "FATAL: no ADMIN_PUBKEY, and root has no authorized_keys. Continuing"
	echo "would disable root login with nothing able to log in. Nothing changed."
	exit 1
fi
chown admin:admin /home/admin/.ssh/authorized_keys
chmod 600 /home/admin/.ssh/authorized_keys

install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
: > /home/deploy/.ssh/authorized_keys

# The CI key is pinned to a forced command. `restrict` drops port forwarding,
# agent forwarding, X11 and pty; `command=` replaces whatever the client asked
# for with the gate, which allows rsync into two directories and three verbs.
# A deploy key that can run arbitrary commands is a shell living in a secret
# store; this is that same key with its blast radius removed.
printf 'restrict,command="/usr/local/bin/ui-servo-ssh-gate" %s\n' "$DEPLOY_PUBKEY" \
	>> /home/deploy/.ssh/authorized_keys

chown deploy:deploy /home/deploy/.ssh/authorized_keys
chmod 600 /home/deploy/.ssh/authorized_keys

# --- 4. firewall and ssh ---------------------------------------------------
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin no/'               /etc/ssh/sshd_config
sed -i 's/^#\?KbdInteractiveAuthentication.*/KbdInteractiveAuthentication no/' /etc/ssh/sshd_config
systemctl restart ssh

if [ -n "${ADMIN_IP}" ]; then
	printf '[DEFAULT]\nignoreip = 127.0.0.1/8 ::1 %s\n' "${ADMIN_IP}" > /etc/fail2ban/jail.local
	echo "fail2ban: ${ADMIN_IP} exempted"
fi
systemctl enable --now fail2ban
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF

# --- 5. directories --------------------------------------------------------
install -d -o deploy   -g deploy   -m 755 /opt/ui-servo /opt/ui-servo/releases \
	/opt/ui-servo/incoming /opt/ui-servo/app
install -d -o ui-servo -g ui-servo -m 750 /var/lib/ui-servo

# A holding page, so the site answers 200 from the moment DNS resolves rather
# than 502 until the first deploy lands.
install -d -o deploy -g deploy -m 755 /opt/ui-servo/releases/bootstrap
cat > /opt/ui-servo/releases/bootstrap/index.html <<'EOF'
<!doctype html><meta charset=utf-8><title>ui-servo</title>
<body style="background:#0a0b0d;color:#eae5db;font:16px ui-monospace,monospace;padding:3rem">
<p>ui-servo: host ready, awaiting first deploy.</p>
EOF
ln -sfn /opt/ui-servo/releases/bootstrap /opt/ui-servo/current
chown -h deploy:deploy /opt/ui-servo/current

# --- 6. caddy --------------------------------------------------------------
# The standard package has no rate limiter, so take the build with the plugin
# compiled in. Pinned by version, not "latest", so a rebuild is a decision.
CADDY_VERSION=2.11.4
curl -fsSL -o /usr/local/bin/caddy \
	"https://caddyserver.com/api/download?os=linux&arch=amd64&p=github.com/mholt/caddy-ratelimit&version=v${CADDY_VERSION}"
chmod +x /usr/local/bin/caddy
# Assert rather than print. The download endpoint accepts a `version` argument
# and returns 200, but whether it honours the pin or quietly serves latest is
# not observable from the request — so check what actually landed on disk.
got=$(/usr/local/bin/caddy version | awk '{print $1}')
if [ "$got" != "v${CADDY_VERSION}" ]; then
	echo "FATAL: asked for v${CADDY_VERSION}, got ${got}"
	exit 1
fi
/usr/local/bin/caddy list-modules | grep -qx 'http.handlers.rate_limit' || {
	echo "FATAL: this caddy build has no rate limiter; the Caddyfile will not load"
	exit 1
}

id caddy >/dev/null 2>&1 || adduser --system --group --no-create-home \
	--shell /usr/sbin/nologin caddy
install -d -o caddy -g caddy -m 755 /var/lib/caddy /var/log/caddy
# Caddy keeps issued certificates under $HOME/.local/share/caddy. A system user
# made with --no-create-home gets HOME=/nonexistent, and the failure is nasty:
# the server starts and listens on 80 and 443, but every ACME attempt dies with
# "failed storage check: mkdir /nonexistent: permission denied", so the site is
# up and permanently without a certificate. Upstream's package points HOME at
# the state directory; do the same.
# usermod refuses outright while the user owns a running process, so a re-run
# over a live box fails here unless caddy is stopped first. Guarded, because on
# a fresh boot there is nothing to stop.
if [ "$(getent passwd caddy | cut -d: -f6)" != /var/lib/caddy ]; then
	systemctl stop caddy 2>/dev/null || true
	usermod -d /var/lib/caddy caddy
fi
# chown -R as well as install -d: install fixes the *directory* but leaves any
# file already inside it alone. A re-run over a box where the log file was
# created as root therefore left caddy unable to open its own log -- it exits
# 1 at startup, nothing listens on 80/443, and the box answers ECONNREFUSED
# in a way that looks like a firewall problem.
chown -R caddy:caddy /var/lib/caddy /var/log/caddy
install -d -m 755 /etc/caddy

cat > /etc/caddy/Caddyfile <<CADDYFILE
{
	admin off
	http_port {\$HTTP_PORT:80}
	email ${ACME_EMAIL}
	order rate_limit before reverse_proxy
	servers {
		max_header_size 16KB
		timeouts {
			read_body 10s
			read_header 5s
			write 30s
			idle 2m
		}
	}
}

${SITE_NAMES} {
	log {
		output file /var/log/caddy/ui-servo.log {
			roll_size 10MB
			roll_keep 5
			roll_keep_for 336h
		}
	}

	header {
		Strict-Transport-Security "max-age=31536000; includeSubDomains; preload"
		X-Content-Type-Options "nosniff"
		X-Frame-Options "DENY"
		Referrer-Policy "strict-origin-when-cross-origin"
		Permissions-Policy "geolocation=(), microphone=(), camera=(), payment=(), usb=()"
		Cross-Origin-Opener-Policy "same-origin"
		Cross-Origin-Resource-Policy "same-origin"
		Content-Security-Policy "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; worker-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'; object-src 'none'"
		-Server
	}

	# Written order, not Caddy's directive precedence. Without \`route\`,
	# \`handle\` outranks \`respond\` and \`rate_limit\` and every refusal below
	# becomes dead config that still passes a naive curl check, because the
	# backend returns the same status codes on its own.
	route {
		@bad_method not method GET HEAD POST
		respond @bad_method "method not allowed" 405

		@probes path /wp-admin* /wp-login.php /xmlrpc.php /.env* /.git/* /.aws/* /vendor/* /phpmyadmin* /phpinfo.php /server-status /actuator/* /.ssh/* /config.json /telescope*
		respond @probes "not found" 404

		@ai_crawlers header_regexp User-Agent "(?i)(GPTBot|ChatGPT-User|OAI-SearchBot|CCBot|ClaudeBot|anthropic-ai|Claude-Web|Bytespider|Amazonbot|FacebookBot|meta-externalagent|Diffbot|ImagesiftBot|Omgilibot|PerplexityBot|Applebot-Extended|cohere-ai)"
		respond @ai_crawlers "no" 403

		rate_limit {
			zone beacon {
				match {
					method POST
					path /beacon*
				}
				key {remote_host}
				events 6
				window 1m
			}
			zone browse {
				key {remote_host}
				events 240
				window 1m
			}
		}

		handle /beacon* {
			request_body {
				max_size 8KB
			}
			reverse_proxy 127.0.0.1:8111 {
				header_up X-Real-IP {remote_host}
			}
		}

		handle {
			root * /opt/ui-servo/current

			@immutable path /assets/fonts/*
			header @immutable Cache-Control "public, max-age=604800"
			@assets path /assets/*
			header @assets Cache-Control "public, max-age=300"
			@sw path /sw.js
			header @sw Cache-Control "no-store"
			@docs path / *.html /about/* /projects/* /writing/*
			header @docs Cache-Control "no-cache"

			# br is absent on purpose: this build serves precompressed .br but
			# cannot produce it. \`encode ... br ...\` fails at config-adapt time.
			encode zstd gzip
			file_server {
				precompressed br zstd gzip
				index index.html
			}
		}
	}

	handle_errors {
		@notfound expression {err.status_code} == 404
		rewrite @notfound /404.html
		file_server
	}
}
CADDYFILE

cat > /etc/systemd/system/caddy.service <<'EOF'
[Unit]
Description=Caddy
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
User=caddy
Group=caddy
ExecStart=/usr/local/bin/caddy run --environ --config /etc/caddy/Caddyfile
ExecReload=/usr/local/bin/caddy reload --config /etc/caddy/Caddyfile --force
TimeoutStopSec=5s
LimitNOFILE=1048576
PrivateTmp=true
ProtectSystem=full
# 80 and 443 without running as root.
AmbientCapabilities=CAP_NET_BIND_SERVICE
# Measured peak was 66 MB serving 1 900 rps; 192M leaves room and still means
# a leak restarts Caddy instead of taking the droplet with it.
MemoryMax=192M

[Install]
WantedBy=multi-user.target
EOF

# --- 7. ui-servo scripts ---------------------------------------------------
cat > /usr/local/bin/ui-servo-activate <<'EOF'
__ACTIVATE__
EOF
cat > /usr/local/bin/ui-servo-ssh-gate <<'EOF'
__GATE__
EOF
cat > /usr/local/bin/ui-servo-prune-evidence <<'EOF'
__PRUNE__
EOF
cat > /usr/local/bin/ui-servo-sync-ingest <<'EOF'
__SYNC__
EOF
chmod 755 /usr/local/bin/ui-servo-activate /usr/local/bin/ui-servo-ssh-gate \
	/usr/local/bin/ui-servo-prune-evidence /usr/local/bin/ui-servo-sync-ingest

# One privileged verb for the deploy user, and no other.
cat > /etc/sudoers.d/ui-servo-deploy <<'EOF'
deploy ALL=(root) NOPASSWD: /usr/bin/systemctl stop ui-servo-ingest-backend.service
EOF
chmod 440 /etc/sudoers.d/ui-servo-deploy
visudo -c -f /etc/sudoers.d/ui-servo-deploy

# --- 7b. the ingest, socket-activated --------------------------------------
# Three units, not one. The socket holds the port for nothing; the proxy starts
# the app on the first beacon and exits when the traffic stops; the app stops
# with it. Idle cost is zero rather than ~80 MB on a 512 MB box.
cat > /etc/systemd/system/ui-servo-ingest.socket <<'EOF'
__INGEST_SOCKET__
EOF
cat > /etc/systemd/system/ui-servo-ingest.service <<'EOF'
__INGEST_PROXY__
EOF
cat > /etc/systemd/system/ui-servo-ingest-backend.service <<'EOF'
__INGEST_BACKEND__
EOF

# --- 8. evidence retention -------------------------------------------------
cat > /etc/systemd/system/ui-servo-prune-evidence.service <<'EOF'
[Unit]
Description=Prune ui-servo beacon evidence

[Service]
Type=oneshot
User=ui-servo
Group=ui-servo
Environment=UI_SERVO_INGEST_ROOT=/var/lib/ui-servo
Environment=UI_SERVO_EVIDENCE_DAYS=7
Environment=UI_SERVO_EVIDENCE_MAX_MB=256
ExecStart=/usr/local/bin/ui-servo-prune-evidence
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/ui-servo
EOF

cat > /etc/systemd/system/ui-servo-prune-evidence.timer <<'EOF'
[Unit]
Description=Prune ui-servo beacon evidence hourly

[Timer]
# Hourly on a 10 GB disk: the number that matters is the worst case between
# runs, and the ingest can absorb ~0.67 GiB/day from one address at the rate
# limit. Hourly caps the overshoot at ~28 MB.
OnCalendar=hourly
Persistent=true
RandomizedDelaySec=5m

[Install]
WantedBy=timers.target
EOF

# --- 9. start --------------------------------------------------------------
/usr/local/bin/caddy validate --config /etc/caddy/Caddyfile
systemctl daemon-reload
# The socket, not the service: the ingest must not be running at rest. The
# socket holds the port for ~0 bytes and the proxy pulls the app up on the
# first beacon, then lets it go again five minutes after the last one.
systemctl enable --now caddy ui-servo-prune-evidence.timer ui-servo-ingest.socket

echo "=== ui-servo init done $(date -Is)"
echo
echo "Host key for the DEPLOY_KNOWN_HOSTS secret:"
# Read the key file rather than ssh-keyscan localhost: keyscan needs sshd to
# be accepting connections already, and at this point in the boot it may not
# be. The file is on disk from the moment the host keys are generated.
PUBLIC_IP=$(curl -s --max-time 5 ifconfig.me || true)
if [ -r /etc/ssh/ssh_host_ed25519_key.pub ]; then
	printf '%s %s\n' "${PUBLIC_IP:-YOUR_DROPLET_IP}" \
		"$(cut -d' ' -f1,2 /etc/ssh/ssh_host_ed25519_key.pub)"
else
	echo "  no ed25519 host key on disk; get it later with:"
	echo "  ssh-keyscan -t ed25519 ${PUBLIC_IP:-YOUR_DROPLET_IP}"
fi
echo
echo "Still to do:"
echo "  1. open 80/tcp and 443/tcp to 0.0.0.0/0 AND ::/0 in the cloud firewall --"
echo "     the ACME challenge is Let's Encrypt connecting IN on port 80, so a"
echo "     source restriction there means the certificate never issues"
echo "  2. add the five repo secrets (see deploy/README.md)"
echo "  3. push to main — CI builds and deploys"
echo "  4. the ingest starts on its first beacon — nothing runs until then"
