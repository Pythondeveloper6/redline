#!/usr/bin/env python3
"""Regenerate the documentation screenshots (and optionally run browser checks).

How to run
----------
1. Build the UI into the package:      scripts/build_ui.sh
2. Seed and start the example apps (see examples/*/README.md) on these ports:
     django 8011 (manage.py runserver 8011), fastapi 8012, flask 8013, litestar 8014
   Optional, for the login screenshot: a copy of the FastAPI example on 8015 started with
     REDLINE_ACCESS=token REDLINE_TOKEN=<24+ chars>   (pass the token with --token)
   Optional, for the load screenshots: run a load test first, e.g.
     uv run --package redline-core --extra load redline load <scenario.toml> \
         --store examples/fastapi/.redline/redline.db
3. Send the broken endpoints some traffic (--traffic does it for you).
4. Run it (Playwright is fetched on the fly; install the browser once with
   `uv run --with playwright playwright install chromium`):

     uv run --no-project --with playwright python scripts/screenshots.py --traffic
     uv run --no-project --with playwright python scripts/screenshots.py \
         --only fastapi --token <token>
     # checks only, no files written:
     uv run --no-project --with playwright python scripts/screenshots.py --verify

PNGs are written at 1440x900 for the light and dark theme into docs/assets/screenshots/
(--out to change). The script lists console errors it saw on every page.
"""

from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
PORTS = {"django": 8011, "fastapi": 8012, "flask": 8013, "litestar": 8014}
MOUNT = {"django": "/redline/"}  # everything else mounts at /_redline/
LEGACY_FASTAPI = {
    "requests",
    "detail-issues",
    "detail-timeline",
    "detail-issues-m2o",
    "detail-issues-sync-m2m",
    "detail-http-out",
    "error500",
    "explain",
    "flamegraph",
    "http-redacted",
    "issues",
    "endpoints",
}

TRAFFIC = {
    "django": [
        "/",
        "/orders/",
        "/items/",
        "/orders/page/",
        "/dashboard/",
        "/reviews/search/?q=battery",
        "/slow/",
        "/slow/?_redline=profile",
        "/boom/",
        "/async/summary/",
        "/cached/",
        "/cached/",
    ],
    "other": [
        "/orders",
        "/orders/customers",
        "/sync/orders",
        "/orders/tags",
        "/orders/page",
        "/orders/1",
        "/orders/2",
        "/report/duplicates",
        "/audit/search?q=refund",
        "/slow",
        "/boom",
        "/pricing",
        "/orders/2?_redline=profile",
    ],
}
# path of the endpoint behind each screenshot
EP = {
    "django": {
        "n1": "/orders/",
        "m2o": "/items/",
        "search": "/reviews/search",
        "profile": "/slow/",
        "boom": "/boom/",
    },
    "other": {
        "n1": "/orders",
        "m2o": "/orders/customers",
        "m2m": "/sync/orders",
        "search": "/audit/search",
        "profile": "/orders/2",
        "boom": "/boom",
        "http_out": "/pricing",
    },
}


def base(fw: str, port: int | None = None) -> str:
    return f"http://127.0.0.1:{port or PORTS[fw]}{MOUNT.get(fw, '/_redline/')}"


def send_traffic(fw: str) -> None:
    root = f"http://127.0.0.1:{PORTS[fw]}"
    for _ in range(3):
        for u in TRAFFIC["django" if fw == "django" else "other"]:
            try:
                urllib.request.urlopen(root + u, timeout=30).read()
            except Exception:
                pass  # 500s are intended
    if fw == "django":
        req = urllib.request.Request(
            root + "/api/products/", headers={"Authorization": "Bearer sk-live-123"}
        )
        urllib.request.urlopen(req, timeout=30).read()
    else:
        req = urllib.request.Request(
            root + "/checkout",
            method="POST",
            data=b'{"email":"a@b.c","card_number":"4111111111111111","password":"hunter2"}',
            headers={"content-type": "application/json", "Authorization": "Bearer topsecret"},
        )
        urllib.request.urlopen(req, timeout=30).read()


class Session:
    def __init__(self, pw, fw: str, theme: str, port: int | None = None, token: str | None = None):
        self.fw, self.theme, self.base, self.token = fw, theme, base(fw, port), token
        self.browser = pw.chromium.launch()
        self.ctx = self.browser.new_context(viewport={"width": 1440, "height": 900})
        self.ctx.add_init_script(
            f"try{{localStorage.setItem('redline-theme','{theme}')}}catch(e){{}}"
        )
        self.page: Page = self.ctx.new_page()
        self.errors: list[str] = []
        self.page.on(
            "console",
            lambda m: (
                self.errors.append(f"[{self.page.url}] {m.text}") if m.type == "error" else None
            ),
        )
        self.page.on("pageerror", lambda e: self.errors.append(f"[{self.page.url}] pageerror {e}"))
        if fw == "django":
            self.page.goto(f"http://127.0.0.1:{PORTS[fw]}/admin/login/?next=/redline/")
            self.page.fill("#id_username", "admin")
            self.page.fill("#id_password", "admin")
            self.page.click("input[type=submit]")
            self.page.wait_for_load_state()
        elif token:
            self.page.goto(self.base)
            self.page.fill("input[name=token]", token)
            self.page.click("button[type=submit]")
            self.page.wait_for_selector("text=Requests")

    def close(self) -> None:
        self.browser.close()

    def goto(self, hash_: str) -> None:
        self.page.goto(self.base + "#" + hash_)
        self.page.wait_for_load_state("networkidle")
        self.page.wait_for_timeout(700)

    def traces(self, q: str = "") -> list[dict]:
        return self.page.request.get(
            self.base + "api/traces", params={"limit": 500, "q": q}
        ).json()["items"]

    def trace_id(self, path: str, method: str = "GET", prefix: bool = False) -> str:
        for t in self.traces(path):
            if t["method"] == method and (
                t["path"].startswith(path) if prefix else t["path"] == path
            ):
                return t["id"]
        raise LookupError(f"no {method} {path} trace")

    def shot(self, out: Path, page: str) -> None:
        """Write <fw>-<page>-<theme>.png.

        The v0.1 FastAPI shots keep their <fw>-<theme>-<page>.png names.
        """
        out.mkdir(parents=True, exist_ok=True)
        legacy = self.fw == "fastapi" and page in LEGACY_FASTAPI
        name = (
            f"{self.fw}-{self.theme}-{page}.png" if legacy else f"{self.fw}-{page}-{self.theme}.png"
        )
        self.page.mouse.move(0, 0)
        self.page.screenshot(path=str(out / name))
        print("wrote", name)


def shots_for(s: Session, out: Path) -> None:
    fw = s.fw
    ep = EP["django" if fw == "django" else "other"]
    dj = fw == "django"
    s.goto("/requests")
    s.shot(out, "requests")
    if fw in ("flask", "litestar"):
        return
    n1 = s.trace_id(ep["n1"])
    s.goto(f"/requests/{n1}?tab=issues")
    s.shot(out, "n-plus-one" if dj else "detail-issues")
    if not dj:
        s.goto(f"/requests/{n1}?tab=timeline")
        s.shot(out, "detail-timeline")
        s.goto(f"/requests/{s.trace_id(ep['m2o'])}?tab=issues")
        s.shot(out, "detail-issues-m2o")
        s.goto(f"/requests/{s.trace_id(ep['m2m'])}?tab=issues")
        s.shot(out, "detail-issues-sync-m2m")
        s.goto(f"/requests/{s.trace_id(ep['http_out'])}?tab=timeline")
        s.shot(out, "detail-http-out")
        s.goto(f"/requests/{s.trace_id(ep['boom'])}?tab=exception")
        s.shot(out, "error500")
    # explain
    s.goto(f"/requests/{s.trace_id(ep['search'], prefix=True)}?tab=sql")
    s.page.locator(".divide-y button[aria-expanded]").first.click()  # slowest query group
    s.page.locator("button:has-text('Explain'):not([disabled])").first.click()
    s.page.wait_for_selector("text=Query plan")
    s.page.wait_for_timeout(1500)
    s.shot(out, "explain")
    # flamegraph
    prof = next(
        t
        for t in s.traces(ep["profile"])
        if t["has_profile"] and t["path"].startswith(ep["profile"])
    )
    s.goto(f"/requests/{prof['id']}?tab=profile")
    s.page.wait_for_timeout(1000)
    s.shot(out, "flamegraph")
    # redaction
    if dj:
        red = s.trace_id("/api/products/")
    else:
        red = s.trace_id("/checkout", "POST")
    s.goto(f"/requests/{red}?tab=http")
    s.shot(out, "redaction" if dj else "http-redacted")
    s.goto("/issues")
    s.shot(out, "issues")
    s.goto("/endpoints")
    if not dj:
        s.shot(out, "endpoints")
    if fw != "fastapi":
        return
    # v0.2 pages (shown on FastAPI)
    s.goto("/database")
    s.shot(out, "database")
    s.goto("/history")
    s.shot(out, "history")
    s.goto("/load")
    s.page.wait_for_timeout(500)
    runs = s.page.request.get(s.base + "api/runs")
    if runs.ok and runs.json().get("items"):
        s.goto(f"/load/{runs.json()['items'][0]['id']}")
        s.page.wait_for_timeout(1500)
        s.shot(out, "load")
    s.goto("/settings")
    s.shot(out, "settings")
    a, b = s.trace_id("/orders"), s.trace_id("/orders/customers")
    s.goto(f"/compare/{a}/{b}")
    s.shot(out, "compare")
    s.goto("/requests")
    s.page.keyboard.press("Meta+k")
    s.page.wait_for_timeout(400)
    s.page.keyboard.type("iss")
    s.page.wait_for_timeout(400)
    s.shot(out, "palette")


def login_shot(pw, out: Path, token: str, port: int) -> None:
    for theme in ("light", "dark"):
        s = Session(pw, "fastapi", theme, port=port)
        s.page.goto(s.base)
        s.page.wait_for_selector("input[name=token]")
        s.shot(out, "login")
        s.close()


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--out", default=str(ROOT / "docs/assets/screenshots"))
    ap.add_argument(
        "--only", nargs="*", choices=list(PORTS), help="frameworks to capture (default: all)"
    )
    ap.add_argument(
        "--traffic", action="store_true", help="send the broken endpoints some requests first"
    )
    ap.add_argument(
        "--token", help="access token of a protected FastAPI example for the login screenshot"
    )
    ap.add_argument("--login-port", type=int, default=8015)
    ap.add_argument("--verify", action="store_true", help="only scan every page for console errors")
    a = ap.parse_args()
    out = Path(a.out)
    fws = a.only or list(PORTS)
    if a.traffic:
        for fw in fws:
            send_traffic(fw)
    errors: list[str] = []
    with sync_playwright() as pw:
        for fw in fws:
            for theme in ("light", "dark"):
                s = Session(pw, fw, theme)
                if a.verify:
                    for p in (
                        "/requests",
                        "/endpoints",
                        "/issues",
                        "/database",
                        "/cache",
                        "/external",
                        "/history",
                        "/load",
                        "/settings",
                    ):
                        s.goto(p)
                else:
                    shots_for(s, out)
                errors += s.errors
                s.close()
        if a.token and not a.verify:
            login_shot(pw, out, a.token, a.login_port)
    print("console errors:", len(errors))
    for e in errors:
        print("  ", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
