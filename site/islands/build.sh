#!/usr/bin/env sh
# Build the island into the site's asset directory.
#
# The one wrinkle worth a script: wasm-pack writes a blanket `*` .gitignore into
# its out-dir on every build, and the out-dir is `site/assets/islands/`, where
# the hand-written `loader.js` lives. Left alone, that .gitignore would quietly
# untrack the only file in there a human wrote. The generated artefacts are
# committed for the same reason `tokens.css` and `htmx.min.js` are: the server
# serves `assets/` as-is and must not need a wasm toolchain to boot.
set -eu

cd "$(dirname "$0")"
OUT=../assets/islands

wasm-pack build --target web --out-dir "$OUT" "$@"
rm -f "$OUT/.gitignore"

echo "islands -> $(cd "$OUT" && pwd)"
