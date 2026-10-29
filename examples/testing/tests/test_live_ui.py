"""Exercise the built browser UI against a real disposable HTTP demo server."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("REDLINE_COMPOSE_TESTS") != "1", reason="disposable Compose opt-in required"
)
ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def demo_server(tmp_path):
    import httpx

    env = {
        **os.environ,
        "SHOP_DB": str(tmp_path / "shop.db"),
        "REDLINE_STORAGE_PATH": str(tmp_path / "traces.db"),
        "REDLINE_ACCESS": "local",
    }
    project = ROOT / "examples/flask"
    subprocess.run(
        [sys.executable, "-c", "from seed import main; main(audit_rows=1000)"],
        cwd=project,
        env=env,
        check=True,
        capture_output=True,
        timeout=60,
    )
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    assert port >= 8011
    origin = f"http://127.0.0.1:{port}"
    with (tmp_path / "demo-server.log").open("w") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "flask",
                "--app",
                "shop.main",
                "run",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=project,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            with httpx.Client(base_url=origin, timeout=5) as client:
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    assert process.poll() is None, "Owned HTTP demo exited before readiness"
                    try:
                        if client.get("/_redline/api/meta").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(0.1)
                else:
                    pytest.fail("Owned HTTP demo did not become ready")
                assert client.get("/workflows/handled-sql").status_code == 200
                result = client.post("/workflows/job", json={"fail_summary": True})
                assert result.status_code == 200
                trace_id = result.json()["trace_id"]
                assert result.json()["commit_observed_before_failure"]
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if client.get("/_redline/api/traces/" + trace_id).status_code == 200:
                        break
                    time.sleep(0.05)
                else:
                    pytest.fail("Job recording was not retained")
            yield origin, trace_id
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def test_built_ui_jobs_sql_checker_privacy_and_mobile(demo_server, tmp_path):
    # Missing browser/driver support is a failure in the opted-in container, not a skip.
    from playwright.sync_api import expect, sync_playwright

    origin, trace_id = demo_server
    screenshots = Path(os.environ.get("REDLINE_TEST_RESULTS", str(tmp_path))) / "ui"
    screenshots.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(origin + "/_redline/#/jobs")
            expect(page.get_by_role("heading", name="Jobs", exact=True)).to_be_visible()
            expect(page.get_by_text("demo.product-import", exact=True)).to_be_visible()
            page.goto(origin + "/_redline/#/requests/" + trace_id)
            expect(page.get_by_role("heading", name="Job outcome: failed")).to_be_visible()
            expect(
                page.get_by_text("A commit was observed before this failure.", exact=True)
            ).to_be_visible()
            page.screenshot(path=str(screenshots / "job-desktop.png"), full_page=True)
            page.goto(origin + "/_redline/#/analysis")
            page.get_by_role("button", name="SQL checker", exact=True).click()
            page.get_by_role("button", name="Load example", exact=True).click()
            page.get_by_role("button", name="Check SQL", exact=True).click()
            expect(page.get_by_text("bind-in-comment", exact=True)).to_be_visible()
            expect(page.get_by_text("output-without-into", exact=True)).to_be_visible()
            page.get_by_role("button", name="Redaction preview", exact=True).click()
            page.get_by_role("button", name="Preview redaction", exact=True).click()
            preview = page.locator('pre[aria-live="polite"]')
            expect(preview).to_contain_text("[redacted]")
            expect(preview).not_to_contain_text("example@example.test")
            page.set_viewport_size({"width": 390, "height": 844})
            expect(page.get_by_role("button", name="Preview redaction", exact=True)).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(screenshots / "privacy-mobile.png"), full_page=True)
            assert not errors, "Browser raised application errors"
        finally:
            browser.close()
