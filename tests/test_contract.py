"""Cross-framework contract tests against isolated local SQLite databases."""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend/fastapi_app"))
sys.path.insert(0, str(ROOT / "backend/drf_app"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django
django.setup()

from django.conf import settings
from django.core.management import call_command
from rest_framework.test import APIClient
from fastapi.testclient import TestClient
import main as fastapi_main


@pytest.fixture
def apis(tmp_path, monkeypatch):
    monkeypatch.setattr(fastapi_main, "DB_PATH", tmp_path / "fastapi.sqlite3")
    fastapi_main._dummyjson_cache = []
    fastapi_main._dummyjson_cache_until = 0
    original = settings.DATABASES["default"]["NAME"]
    settings.DATABASES["default"]["NAME"] = str(tmp_path / "drf.sqlite3")
    call_command("migrate", verbosity=0, interactive=False)
    with TestClient(fastapi_main.app) as fastapi:
        yield fastapi, APIClient()
    settings.DATABASES["default"]["NAME"] = original


def test_contract_validation_and_crud(apis):
    fa, drf = apis
    payload = {"title": "Contract product", "description": "fixture", "price": .01, "category": "Test", "stock": 0, "image": "https://dummyjson.com/image/400x300"}
    created = [fa.post("/api/v1/products", json=payload), drf.post("/api/v1/products", payload, format="json")]
    assert [r.status_code for r in created] == [201, 201]
    assert set(created[0].json()["data"]) == set(created[1].data["data"])
    ids = [created[0].json()["data"]["id"], created[1].data["data"]["id"]]
    assert fa.patch(f"/api/v1/products/{ids[0]}", json={"stock": 4}).status_code == drf.patch(f"/api/v1/products/{ids[1]}", {"stock": 4}, format="json").status_code == 200
    cases = [
        ({**payload, "price": 0}, 422), ({**payload, "price": .001}, 422),
        ({**payload, "stock": -1}, 422),
        ({**payload, "image": "not a url"}, 422), ({"price": 2, "category": "Test"}, 422),
        ({**payload, "extra": True}, 422), ({**payload, "title": ["wrong"]}, 422),
        ({**payload, "title": 7}, 422), ({**payload, "stock": "not an integer"}, 422),
        ({**payload, "stock": 1.5}, 422), ({**payload, "price": True}, 422),
    ]
    for body, expected in cases:
        actual = [fa.post("/api/v1/products", json=body).status_code, drf.post("/api/v1/products", body, format="json").status_code]
        assert actual == [expected, expected], body
    empty = [fa.patch(f"/api/v1/products/{ids[0]}", json={}), drf.patch(f"/api/v1/products/{ids[1]}", {}, format="json")]
    assert [r.status_code for r in empty] == [422, 422]
    put_missing = [fa.put(f"/api/v1/products/{ids[0]}", json={"title":"Incomplete"}), drf.put(f"/api/v1/products/{ids[1]}", {"title":"Incomplete"}, format="json")]
    assert [r.status_code for r in put_missing] == [422, 422]
    missing = [fa.get("/api/v1/products/2147483000"), drf.get("/api/v1/products/2147483000")]
    assert [r.status_code for r in missing] == [404, 404]
    assert fa.get("/api/v1/health").json()["data"]["status"] == drf.get("/api/v1/health").data["data"]["status"] == "ok"
    assert set(fa.get("/api/v1/products?limit=5").json()["data"][0]) == set(drf.get("/api/v1/products?limit=5").data["data"][0])


def test_import_counts_cache_and_upstream_failure(apis, monkeypatch):
    fa, drf = apis
    from products import views as drf_views
    payload = {"products": [{"title": "Imported contract", "description": "from fixture", "price": 1.2, "category": "Test", "stock": 2, "thumbnail": "https://dummyjson.com/image/400x300"}]}

    class Response:
        def raise_for_status(self): pass
        def json(self): return payload

    monkeypatch.setattr(fastapi_main.httpx, "get", lambda *a, **kw: Response())
    monkeypatch.setattr(drf_views.httpx, "get", lambda *a, **kw: Response())
    for client, key in [(fa, "fastapi"), (drf, "drf")]:
        route = "/api/v1/products/import-dummyjson"
        first = client.post(route) if key == "fastapi" else client.post(route, {}, format="json")
        again = client.post(route) if key == "fastapi" else client.post(route, {}, format="json")
        data = first.json()["data"] if key == "fastapi" else first.data["data"]
        repeated = again.json()["data"] if key == "fastapi" else again.data["data"]
        assert (data["fetched"], data["created"], data["skipped"]) == (1, 1, 0)
        assert (repeated["fetched"], repeated["created"], repeated["skipped"]) == (1, 0, 1)
        assert repeated["cache_hit"] is True
    class InvalidResponse(Response):
        def json(self): return {"unexpected": []}
    monkeypatch.setattr(fastapi_main.httpx, "get", lambda *a, **kw: InvalidResponse())
    monkeypatch.setattr(drf_views.httpx, "get", lambda *a, **kw: InvalidResponse())
    fastapi_main._dummyjson_cache = []
    fastapi_main._dummyjson_cache_until = 0
    drf_views._upstream_cache = []
    drf_views._upstream_cache_until = 0
    assert fa.post("/api/v1/products/import-dummyjson").status_code == 502
    assert drf.post("/api/v1/products/import-dummyjson", {}, format="json").status_code == 502
    monkeypatch.setattr(fastapi_main.httpx, "get", lambda *a, **kw: (_ for _ in ()).throw(fastapi_main.httpx.TimeoutException("timeout")))
    monkeypatch.setattr(drf_views.httpx, "get", lambda *a, **kw: (_ for _ in ()).throw(drf_views.httpx.TimeoutException("timeout")))
    fastapi_main._dummyjson_cache = []
    fastapi_main._dummyjson_cache_until = 0
    drf_views._upstream_cache = []
    drf_views._upstream_cache_until = 0
    a = fa.post("/api/v1/products/import-dummyjson")
    b = drf.post("/api/v1/products/import-dummyjson", {}, format="json")
    assert [a.status_code, b.status_code] == [502, 502]
