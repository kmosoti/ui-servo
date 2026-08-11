#!/usr/bin/env bash
# End-to-end test of the deploy path against a droplet-like container.
#
# The unit tests for `ui-servo-ssh-gate` call it directly with a crafted
# $SSH_ORIGINAL_COMMAND. That proves the gate's logic and nothing about the
# thing that actually runs in production: a real sshd, applying a real
# `restrict,command=` line, to the exact rsync and ssh invocations in
# .github/workflows/deploy.yml. Those are different claims, and the gap
# between them is where a deploy breaks at 2am.
#
# This boots Ubuntu 24.04 with sshd, installs the gate and activate script the
# way cloud-init.sh does, and then runs the workflow's own commands verbatim --
# including the ones that must be refused.
#
#   ./deploy/e2e-deploy-test.sh
#
# Requires docker and a dist/ to ship; builds a throwaway one if absent.
set -euo pipefail
cd "$(dirname "$0")/.."

IMAGE=ui-servo-droplet-e2e
NAME=ui-servo-droplet-e2e-run
PORT=${E2E_SSH_PORT:-2222}          # never 8080; that is the owner's dev server
WORK=$(mktemp -d)
PASS=0
FAIL=0

cleanup() {
	docker rm -f "$NAME" >/dev/null 2>&1 || true
	rm -rf "$WORK"
}
trap cleanup EXIT

ok()   { PASS=$((PASS + 1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad()  { FAIL=$((FAIL + 1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
check(){ if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (got '$2', want '$3')"; fi; }

command -v docker >/dev/null || { echo "docker not installed; skipping"; exit 0; }

# ---- 1. a droplet-shaped host --------------------------------------------
echo "==> building the droplet image"
cat > "$WORK/Dockerfile" <<'DOCKER'
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq && apt-get install -y -qq openssh-server rsync >/dev/null \
 && mkdir -p /run/sshd
# Mirrors cloud-init.sh section 3/4: an unprivileged deploy account whose only
# key is pinned to the forced command, and no password anywhere.
RUN useradd --create-home --shell /bin/bash deploy \
 && install -d -m 700 -o deploy -g deploy /home/deploy/.ssh \
 && install -d -o deploy -g deploy -m 755 \
      /opt/ui-servo /opt/ui-servo/releases /opt/ui-servo/incoming /opt/ui-servo/app
COPY ui-servo-ssh-gate /usr/local/bin/ui-servo-ssh-gate
COPY ui-servo-activate /usr/local/bin/ui-servo-activate
RUN chmod 755 /usr/local/bin/ui-servo-ssh-gate /usr/local/bin/ui-servo-activate
CMD ["/usr/sbin/sshd", "-D", "-e"]
DOCKER
cp deploy/ui-servo-ssh-gate deploy/ui-servo-activate "$WORK/"
docker build -q -t "$IMAGE" "$WORK" >/dev/null

ssh-keygen -t ed25519 -N '' -C 'e2e ci-deploy' -f "$WORK/key" -q
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" -p "127.0.0.1:$PORT:22" "$IMAGE" >/dev/null

# The one line that makes the whole arrangement safe, installed exactly as
# cloud-init.sh writes it.
docker exec -i "$NAME" sh -c \
	"printf 'restrict,command=\"/usr/local/bin/ui-servo-ssh-gate\" %s\n' '$(cat "$WORK/key.pub")' \
	 > /home/deploy/.ssh/authorized_keys \
	 && chown deploy:deploy /home/deploy/.ssh/authorized_keys \
	 && chmod 600 /home/deploy/.ssh/authorized_keys"

for _ in $(seq 1 60); do
	ssh-keyscan -p "$PORT" -t ed25519 127.0.0.1 2>/dev/null | grep -q ssh-ed25519 && break
	sleep 0.5
done
ssh-keyscan -p "$PORT" -t ed25519 127.0.0.1 2>/dev/null > "$WORK/known_hosts"

SSH="ssh -n -i $WORK/key -o UserKnownHostsFile=$WORK/known_hosts -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=10 -o PasswordAuthentication=no -p $PORT"
SSH="timeout 20 $SSH"
export RSYNC_RSH="ssh -i $WORK/key -o UserKnownHostsFile=$WORK/known_hosts -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=10 -p $PORT"

# ---- 2. something to ship -------------------------------------------------
DIST=${E2E_DIST:-}
if [ -z "$DIST" ]; then
	DIST="$WORK/dist"
	mkdir -p "$DIST/assets"
	printf '<!doctype html><title>e2e</title>\n' > "$DIST/index.html"
	printf "var VERSION = 'e2e-1';\n"            > "$DIST/sw.js"
	printf 'body{}\n'                            > "$DIST/assets/site.css"
fi
SHA=$(printf '%s' "e2e" | sha1sum | cut -c1-40)

# ---- 3. the workflow's own commands, verbatim -----------------------------
echo "==> deploy path (.github/workflows/deploy.yml)"

timeout 60 rsync -az --delete --chmod=D755,F644 "$DIST/" "deploy@127.0.0.1:/opt/ui-servo/incoming/" \
	&& ok "rsync into incoming/ is allowed" || bad "rsync into incoming/ was refused"

out=$($SSH deploy@127.0.0.1 "ui-servo-activate $SHA" 2>&1) \
	&& ok "ui-servo-activate <sha> is allowed" || bad "ui-servo-activate refused: $out"

live=$(docker exec "$NAME" readlink /opt/ui-servo/current || true)
check "current -> releases/<sha>" "$live" "/opt/ui-servo/releases/$SHA"

served=$(docker exec "$NAME" cat "/opt/ui-servo/current/sw.js" 2>/dev/null || true)
check "the shipped bytes are live" "$served" "var VERSION = 'e2e-1';"

emptied=$(docker exec "$NAME" sh -c 'ls -A /opt/ui-servo/incoming | wc -l')
check "incoming/ is emptied for the next deploy" "$emptied" "0"

timeout 120 rsync -az --delete --exclude '.venv' --chmod=D755,F644 \
	ui_servo probe direction pyproject.toml uv.lock "deploy@127.0.0.1:/opt/ui-servo/app/" >/dev/null 2>&1 \
	&& ok "rsync of the ingest app is allowed" || bad "rsync into app/ was refused"

# Rollback: nothing staged, but the release is still on disk.
$SSH deploy@127.0.0.1 "ui-servo-activate $SHA" >/dev/null 2>&1 \
	&& ok "re-activating an on-disk release (rollback) works" \
	|| bad "rollback path failed"

# ---- 4. everything else must be refused -----------------------------------
echo "==> refusals"
refuse() {
	if $SSH deploy@127.0.0.1 "$2" >/dev/null 2>&1; then bad "$1 was ALLOWED"; else ok "$1"; fi
}
refuse "a plain shell"                  ""
refuse "id"                             "id"
refuse "cat /etc/shadow"                "cat /etc/shadow"
refuse "an unknown verb"                "ui-servo-nope"
refuse "activate with a non-hex arg"    "ui-servo-activate ../../etc"
refuse "activate with a shell chain"    "ui-servo-activate $SHA; id"
refuse "activate with a substitution"   "ui-servo-activate \$(id)"
refuse "rsync outside the known dirs"   "rsync --server -vlogDtpre.iLsfxCIvu . /root/"
refuse "rsync with --rsh (shell escape)" "rsync --server --rsh=/bin/sh -vlogDtpre.iLsfxCIvu . /opt/ui-servo/incoming/"
refuse "scp out of the box"             "scp -f /etc/passwd"
refuse "sftp subsystem"                 "/usr/lib/openssh/sftp-server"

# A forced command must also survive the client asking for a pty or a tunnel.
if $SSH -T deploy@127.0.0.1 "id" >/dev/null 2>&1; then
	bad "id via -T was ALLOWED"; else ok "pty-less shell request still refused"; fi
if $SSH -N -L "9999:127.0.0.1:22" -o ExitOnForwardFailure=yes deploy@127.0.0.1 true >/dev/null 2>&1; then
	bad "port forwarding was ALLOWED"; else ok "port forwarding refused by restrict"; fi

echo
printf '%s passed, %s failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
