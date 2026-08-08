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

# The second wrinkle: when this crate ships a `wasm_bindgen(inline_js)` snippet,
# `ui_servo_islands.js` opens with an import from `./snippets/` — and wasm-pack
# writes that directory but leaves it out of the generated package.json's
# `files`. Anything that assembles a package from that list (npm pack, a copy
# step, a CDN publish) then produces a module that throws on its first import.
# No snippet ships today, so this is a no-op; it is here because the failure is
# silent, the generated file is rewritten on every build, and a hand edit would
# last exactly one of them.
python3 - "$OUT" <<'PY'
import json, pathlib, sys

out = pathlib.Path(sys.argv[1])
if not (out / "snippets").is_dir():
    raise SystemExit(0)
path = out / "package.json"
package = json.loads(path.read_text())
files = package.get("files", [])
if "snippets/" not in files:
    files.append("snippets/")
    package["files"] = files
    path.write_text(json.dumps(package, indent=2) + "\n")
PY

echo "islands -> $(cd "$OUT" && pwd)"
