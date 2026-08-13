#!/usr/bin/env bash
# Refresh the /usr/local/bin helpers on an already-provisioned droplet.
#
# cloud-init.sh installs ui-servo-activate, ui-servo-ssh-gate,
# ui-servo-sync-ingest and ui-servo-prune-evidence exactly once, at
# provisioning. Editing them in this repository therefore changes nothing on a
# droplet that already exists -- the deploy uploads only the site and the
# ingest app, and the SSH gate runs the copy that was installed months ago.
# That is how a droplet ended up running `uv sync --frozen` for a week after
# `--no-dev` was committed.
#
# This is deliberately NOT part of the deploy workflow, and the omission is the
# security boundary rather than an oversight. The deploy key is pinned to a
# forced command whose whole purpose is that CI cannot run arbitrary code as
# `deploy`; a workflow step that rewrote /usr/local/bin could replace
# ui-servo-ssh-gate itself, which is the one file the entire arrangement rests
# on. Updating helpers is an administrator action, over the admin account, on
# purpose.
#
#   ./deploy/update-helpers.sh admin@142.93.206.223
#
# Idempotent: re-running with nothing changed installs nothing and says so.
set -euo pipefail
cd "$(dirname "$0")"

TARGET=${1:-}
if [ -z "$TARGET" ]; then
	echo "usage: $0 <admin-user>@<host>" >&2
	exit 2
fi

HELPERS=(ui-servo-activate ui-servo-ssh-gate ui-servo-sync-ingest ui-servo-prune-evidence)
UNITS=(ui-servo-ingest.socket ui-servo-ingest.service ui-servo-ingest-backend.service
       ui-servo-prune-evidence.service ui-servo-prune-evidence.timer)

for f in "${HELPERS[@]}" "${UNITS[@]}"; do
	[ -f "$f" ] || { echo "missing $f in $(pwd)" >&2; exit 1; }
done

echo "==> shipping helpers to $TARGET"
tmp=$(ssh "$TARGET" 'mktemp -d')
# shellcheck disable=SC2086  # the arrays are intentionally word-split into scp
scp -q "${HELPERS[@]}" "${UNITS[@]}" "$TARGET:$tmp/"

# Compared before installing so the output says what actually changed, and so
# a no-op run does not restart anything. `install` is used rather than cp so
# mode and ownership are set in the same syscall as the write.
ssh "$TARGET" "sudo sh -euc '
	changed=0
	for f in ${HELPERS[*]}; do
		if ! cmp -s \"$tmp/\$f\" \"/usr/local/bin/\$f\"; then
			install -m 755 -o root -g root \"$tmp/\$f\" \"/usr/local/bin/\$f\"
			echo \"  updated /usr/local/bin/\$f\"
			changed=1
		fi
	done
	for f in ${UNITS[*]}; do
		if ! cmp -s \"$tmp/\$f\" \"/etc/systemd/system/\$f\"; then
			install -m 644 -o root -g root \"$tmp/\$f\" \"/etc/systemd/system/\$f\"
			echo \"  updated /etc/systemd/system/\$f\"
			changed=1
		fi
	done
	rm -rf \"$tmp\"
	if [ \"\$changed\" = 0 ]; then
		echo \"  everything already current\"
		exit 0
	fi
	systemctl daemon-reload
	echo \"  systemd reloaded\"
'"

echo "==> done. The ingest picks up a new sync helper on its next deploy;"
echo "    to apply one now: ssh $TARGET 'sudo -u deploy ui-servo-sync-ingest'"
