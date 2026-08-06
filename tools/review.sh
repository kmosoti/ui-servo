#!/usr/bin/env bash
# Adversarial unit review via codex (gpt-5.6-sol, read-only sandbox).
# Usage: tools/review.sh <unit-id> <unit-spec-text-file>
# Prints the JSON verdict (schema: tools/verdict.schema.json) to stdout.
set -euo pipefail
UNIT="$1"
SPEC_FILE="$2"
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
  echo "=== DIFF (staged+unstaged vs HEAD, scoped to unit paths) ==="
  cd "${REPO}" && git diff HEAD --stat && git diff HEAD | head -4000
} | ~/.codex/packages/standalone/current/codex exec \
      -s read-only -C "${REPO}" --skip-git-repo-check \
      --output-schema "${REPO}/tools/verdict.schema.json" \
      -o "${OUT}" - >/dev/null 2>&1
cat "${OUT}"
