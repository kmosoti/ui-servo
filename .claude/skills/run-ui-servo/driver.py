#!/usr/bin/env python3
"""Launch the ui-servo site server, screenshot every page, assert the basics.

The agent-facing harness for this repo's web app. It owns the whole lifecycle:
build+boot the axum binary on a chosen port, wait for readiness, drive the
pages with Playwright (the repo's browser tool — installed via uv), write
screenshots, and shut the server down. Exit 0 means every page served and
rendered visible content.

Usage:
    uv run python .claude/skills/run-ui-servo/driver.py --port 8199
    uv run python .claude/skills/run-ui-servo/driver.py --port 8199 --out /tmp/shots

Never point this at port 8080 without checking — interactive sessions often
keep a dev server there.
"""

import argparse
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # .claude/skills/run-ui-servo -> repo root
SITE = ROOT / "site"

# (route, screenshot name, string that must appear in the page's VISIBLE text —
# display:none content like the print resume doesn't count).
PAGES = [
    ("/", "home", "kennedy@observability"),
    ("/projects/blackcell", "blackcell", "BlackCell"),
    ("/projects/splunk-dashboard-studio", "splunk", "dashboard-studio"),
    ("/about", "about", None),  # classic shell; asserted via its island element instead
]


def wait_for(url: str, seconds: int) -> bool:
    for _ in range(seconds):
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except Exception:
            time.sleep(1)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8199)
    ap.add_argument("--out", default="/tmp/ui-servo-run")
    ap.add_argument("--settle-ms", type=int, default=4000,
                    help="margin canvas scenes are animated; give them time before shooting")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    base = f"http://127.0.0.1:{args.port}"

    env = dict(os.environ, UI_SERVO_PORT=str(args.port))
    # New session so the whole cargo->binary tree can be signalled together.
    srv = subprocess.Popen(
        ["cargo", "run", "--quiet"], cwd=SITE, env=env, start_new_session=True
    )
    try:
        # First boot may compile; 180s covers a cold debug build on this machine.
        if not wait_for(base + "/", 180):
            print("FAIL — server never answered", file=sys.stderr)
            return 1

        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(
                viewport={"width": 1920, "height": 950}, device_scale_factor=2
            )
            for route, name, marker in PAGES:
                page.goto(base + route)
                page.wait_for_timeout(args.settle_ms)
                page.screenshot(path=str(out / f"{name}.png"), full_page=True)
                body = page.locator("body").inner_text()
                if len(body.strip()) < 200:
                    print(f"FAIL — {route} rendered almost no text", file=sys.stderr)
                    return 1
                if marker and marker not in body:
                    print(f"FAIL — {route} missing marker {marker!r}", file=sys.stderr)
                    return 1
                if route == "/about" and page.locator("resume-sandbox").count() != 1:
                    print("FAIL — /about lost its resume-sandbox island", file=sys.stderr)
                    return 1
                print(f"ok  {route}  ->  {out / (name + '.png')}")
            browser.close()
        print(f"PASS — {len(PAGES)} pages served, screenshots in {out}")
        return 0
    finally:
        os.killpg(os.getpgid(srv.pid), signal.SIGINT)
        try:
            srv.wait(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(srv.pid), signal.SIGKILL)


if __name__ == "__main__":
    sys.exit(main())
