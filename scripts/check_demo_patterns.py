"""Exercise the four shop demos in-process against disposable databases, without servers."""

from __future__ import annotations

import argparse
import contextlib
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRAMEWORKS = ("django", "fastapi", "flask", "litestar")


def check(framework: str) -> None:
    from redline_core.check.diff import compare_captures

    with tempfile.TemporaryDirectory(prefix=f"redline-{framework}-patterns-") as directory:
        work = Path(directory)
        os.environ["SHOP_DB"] = str(work / "shop.db")
        os.environ["REDLINE_STORAGE_PATH"] = str(work / "redline.db")
        os.environ["SHOP_REFACTOR"] = "0"
        sys.path.insert(0, str(ROOT / "examples" / framework))
        os.chdir(work)
        if framework == "django":
            os.environ["DJANGO_SETTINGS_MODULE"] = "shopsite.settings"
            import django
            from django.conf import settings
            from django.core.management import call_command
            from django.test import Client

            settings.DATABASES["default"]["NAME"] = str(work / "shop.db")
            settings.REDLINE["STORAGE_PATH"] = str(work / "redline.db")
            django.setup()
            call_command("migrate", verbosity=0)
            from shop import views as module
            from shop.models import Category, Product

            category = Category.objects.create(name="test")
            Product.objects.bulk_create(
                [Product(name=f"Product {i}", price=1, category=category) for i in range(30)]
            )
            from django.contrib.auth import get_user_model

            admin = get_user_model().objects.create_superuser(
                "pattern-admin", "admin@example.test", "test-only"
            )
            client = Client(HTTP_HOST="localhost", REMOTE_ADDR="127.0.0.1")
            client.force_login(admin)
            client_context = contextlib.nullcontext(client)
            prefix, path, writes = "/redline", "/patterns/", "/patterns/writes/"
        else:
            from shop import models
            from shop.db import sync_engine
            from sqlalchemy.orm import Session

            models.Base.metadata.create_all(sync_engine)
            with Session(sync_engine) as session:
                session.add_all([models.Product(name=f"Product {i}", price=1) for i in range(30)])
                session.commit()
            if framework == "flask":
                from shop import views as module
                from shop.main import app

                client_context = app.test_client()
            elif framework == "fastapi":
                from fastapi.testclient import TestClient
                from shop import api as module
                from shop.main import app

                client_context = TestClient(app, base_url="http://127.0.0.1:8011")
            else:
                from litestar.testing import TestClient
                from shop import api as module
                from shop.main import app

                client_context = TestClient(app, base_url="http://127.0.0.1:8013")
            prefix, path, writes = "/_redline", "/patterns", "/patterns/writes"

        def json_body(response):
            assert response.status_code < 400, (
                response.status_code,
                (response.content if hasattr(response, "content") else response.data)[:300],
            )
            return response.json() if callable(response.json) else response.json

        with client_context as client:
            before = json_body(client.get(path))

            def recorded(expected_queries):
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    items = json_body(client.get(prefix + "/api/traces"))["items"]
                    for item in items:
                        if item["path"] == path and item["query_count"] == expected_queries:
                            return json_body(client.get(prefix + "/api/traces/" + item["id"]))
                    time.sleep(0.01)
                raise AssertionError(f"{framework}: expected trace with {expected_queries} queries")

            baseline = recorded(4)
            assert {"repeated_scan", "deep_offset", "many_orm_instances"} <= {
                i["kind"] for i in baseline["issues"]
            }
            module.REFACTOR = True
            after = json_body(client.get(path))
            current = recorded(2)
            assert not {"repeated_scan", "deep_offset", "many_orm_instances"} & {
                i["kind"] for i in current["issues"]
            }
            equivalent = compare_captures(
                {"body_kind": "json", "body": before},
                {"body_kind": "json", "body": after},
                array_keys={"items": "id"},
                tolerances={"total": {"absolute": 0.01}},
            )
            assert equivalent["diff_total"] == 0, equivalent
            assert before != after  # explicit rules, rather than an accidentally identical demo
            rolled_back = json_body(client.post(writes))
            assert rolled_back == {"writes": 6, "rolled_back": True}
            assert json_body(client.get(path)) == after
            report = json_body(
                client.post(
                    prefix + "/api/analysis/source",
                    json={"source": "sum(x.price for x in Product.objects.all())"},
                )
                if framework != "django"
                else client.post(
                    prefix + "/api/analysis/source",
                    data='{"source":"sum(x.price for x in Product.objects.all())"}',
                    content_type="application/json",
                )
            )
            assert report["findings"][0]["kind"] == "python_aggregation"
        print(
            f"{framework}: queries 4 -> 2; findings fixed; keyed/tolerant responses equivalent; "
            "writes rolled back; UI source API passed"
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--framework", choices=FRAMEWORKS)
    args = parser.parse_args()
    if args.framework:
        check(args.framework)
    else:
        for framework in FRAMEWORKS:
            subprocess.run([sys.executable, __file__, "--framework", framework], check=True)


if __name__ == "__main__":
    main()
