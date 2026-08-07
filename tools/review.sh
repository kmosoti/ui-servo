#!/usr/bin/env bash
# Adversarial unit review via codex (gpt-5.6-sol, read-only sandbox).
# Usage: tools/review.sh <unit-id> <unit-spec-text-file> [path ...]
# Paths scope the diff to the unit's owned files (required when units build in parallel).
# Prints the JSON verdict (schema: tools/verdict.schema.json) to stdout.
set -euo pipefail
UNIT="$1"
SPEC_FILE="$2"
shift 2
SCOPE=("$@")
REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$(mktemp)"
{
  echo "You are a ruthless adversarial code reviewer (the blind critic in a Gauntlet Loop)."
  echo "Unit under review: ${UNIT}. You see only the spec, the diff, and the working tree — judge against the spec, hunt for real defects: broken logic, spec violations, security holes, hexagonal dependency-rule violations (domain must import no ports/adapters/control), test gaps that hide bugs."
  echo "critical = would break correctness/security/architecture and must be fixed before merge."
  echo "minor = worth recording, not blocking."
  echo "Do not praise. If it survives honest attack, return empty arrays."
  echo
  echo "=== UNIT SPEC ==="
  cat "${SPEC_FILE}"
  echo
  echo "=== DIFF (vs HEAD, scoped to unit paths; untracked owned files shown whole) ==="
  cd "${REPO}"
  git diff HEAD --stat -- "${SCOPE[@]}"
  git diff HEAD -- "${SCOPE[@]}" | head -4000
  for p in "${SCOPE[@]}"; do
    git ls-files --others --exclude-standard -- "$p" | while read -r f; do
      # Binary blobs (wasm, images, fonts) would corrupt the prompt stream.
      if grep -Iq . "$f" 2>/dev/null; then
        echo "=== NEW FILE: $f ==="; head -400 "$f"
      else
        echo "=== NEW BINARY (skipped, $(wc -c <"$f") bytes): $f ==="
      fi
    done
  done
} | ~/.codex/packages/standalone/current/codex exec \
      -s read-only -C "${REPO}" --skip-git-repo-check \
      --output-schema "${REPO}/tools/verdict.schema.json" \
      -o "${OUT}" - >/dev/null 2>&1
cat "${OUT}"
