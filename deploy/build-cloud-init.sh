#!/usr/bin/env bash
# Produce the pasteable DigitalOcean startup script.
#
# cloud-init.sh is a template with placeholders; the four droplet scripts live
# in their own files so they can be shellcheck'd, unit-tested and reviewed as
# code rather than as strings inside a heredoc. This inlines them, so what you
# paste into the DO console is self-contained and does not depend on the repo
# being reachable — or public — at boot.
#
#   ./deploy/build-cloud-init.sh > cloud-init.generated.sh
set -euo pipefail
cd "$(dirname "$0")"

emit() {
	# Indentation is not cosmetic here: the heredocs in the template are
	# unindented ('EOF' at column 0), so the inlined bodies must be too.
	cat "$1"
}

template=$(cat cloud-init.sh)
for pair in ACTIVATE:ui-servo-activate GATE:ui-servo-ssh-gate \
            PRUNE:ui-servo-prune-evidence SYNC:ui-servo-sync-ingest \
            INGEST_SOCKET:ui-servo-ingest.socket \
            INGEST_PROXY:ui-servo-ingest.service \
            INGEST_BACKEND:ui-servo-ingest-backend.service; do
	token="__${pair%%:*}__"
	file="${pair##*:}"
	[ -f "$file" ] || { echo "missing $file" >&2; exit 1; }
	# python rather than sed: the bodies contain slashes, ampersands and
	# backslashes, all of which sed would reinterpret in a replacement.
	template=$(TEMPLATE="$template" TOKEN="$token" FILE="$file" python3 - <<'PY'
import os
t = os.environ["TEMPLATE"]
body = open(os.environ["FILE"]).read().rstrip("\n")
tok = os.environ["TOKEN"]
if tok not in t:
    raise SystemExit(f"token {tok} not found in template")
print(t.replace(tok, body), end="")
PY
	)
done

printf '%s\n' "$template"
