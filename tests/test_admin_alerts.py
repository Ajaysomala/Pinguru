"""GET /admin/alerts and POST /admin/alerts/{id}/resolve."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from bson import ObjectId
from fastapi.testclient import TestClient

import app.main as main_module
from app.database import get_db
from app.routes.admin import get_admin_user


def _matches(doc, query):
    return all(doc.get(k) == v for k, v in query.items())


class _Cursor:
    def __init__(self, docs):
        self.docs = docs

    def sort(self, key, direction):
        self.docs = sorted(self.docs, key=lambda d: d[key], reverse=direction == -1)
        return self

    def skip(self, n):
        self.docs = self.docs[n:]
        return self

    def limit(self, n):
        self.docs = self.docs[:n]
        return self

    async def to_list(self, _n):
        return list(self.docs)


class _Collection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    def find(self, query, projection=None):
        return _Cursor([d for d in self.docs if _matches(d, query)])

    async def count_documents(self, query):
        return sum(1 for d in self.docs if _matches(d, query))

    async def find_one(self, query):
        return next((d for d in self.docs if _matches(d, query)), None)

    async def update_one(self, query, update, upsert=False):
        doc = await self.find_one(query)
        if doc:
            doc.update(update.get("$set", {}))
        return SimpleNamespace(matched_count=1 if doc else 0)

    async def insert_one(self, doc):
        self.docs.append(doc)


def _alerts():
    now = datetime.now(timezone.utc)
    docs = []
    for i in range(25):
        docs.append({
            "_id": ObjectId(),
            "type": "razorpay_cancel_failed" if i % 2 == 0 else "unmatched_subscription_activation",
            "resolved": i % 5 == 0,
            "user_id": f"user_{i}",
            "subscription_id": f"sub_{i}",
            "created_at": now - timedelta(minutes=i),
        })
    return docs


@pytest.fixture
def setup(monkeypatch):
    async def _noop():
        return None

    db = SimpleNamespace(admin_alerts=_Collection(_alerts()), admin_audit=_Collection())

    async def _db():
        yield db

    monkeypatch.setattr(main_module, "connect_db", _noop)
    monkeypatch.setattr(main_module, "disconnect_db", _noop)
    monkeypatch.setattr(main_module, "validate_startup_config", lambda: None)
    main_module.app.dependency_overrides[get_db] = _db
    with TestClient(main_module.app) as client:
        yield client, db
    main_module.app.dependency_overrides.clear()


def _as_admin():
    async def _admin():
        return {"email": "owner@example.com"}

    main_module.app.dependency_overrides[get_admin_user] = _admin


def test_alert_endpoints_require_admin(setup):
    client, db = setup
    alert_id = str(db.admin_alerts.docs[1]["_id"])
    assert client.get("/admin/alerts").status_code == 401
    assert client.post(f"/admin/alerts/{alert_id}/resolve").status_code == 401
    assert db.admin_alerts.docs[1]["resolved"] is False


def test_list_alerts_paginated_newest_first(setup):
    client, _db = setup
    _as_admin()
    first = client.get("/admin/alerts", params={"page": 1, "limit": 10}).json()
    third = client.get("/admin/alerts", params={"page": 3, "limit": 10}).json()

    assert first["total"] == 25 and first["page"] == 1 and first["limit"] == 10
    assert len(first["alerts"]) == 10 and len(third["alerts"]) == 5
    created = [a["created_at"] for a in first["alerts"]]
    assert created == sorted(created, reverse=True)
    assert first["alerts"][0]["user_id"] == "user_0"
    assert isinstance(first["alerts"][0]["id"], str) and "_id" not in first["alerts"][0]


def test_list_alerts_filters_by_type_and_resolved(setup):
    client, _db = setup
    _as_admin()
    resp = client.get("/admin/alerts", params={"type": "razorpay_cancel_failed", "resolved": "false", "limit": 100}).json()
    assert resp["total"] == len(resp["alerts"]) > 0
    assert all(a["type"] == "razorpay_cancel_failed" and a["resolved"] is False for a in resp["alerts"])

    resolved = client.get("/admin/alerts", params={"resolved": "true", "limit": 100}).json()
    assert resolved["total"] == 5 and all(a["resolved"] for a in resolved["alerts"])


def test_list_alerts_rejects_unknown_type_and_bad_paging(setup):
    client, _db = setup
    _as_admin()
    assert client.get("/admin/alerts", params={"type": "$where"}).status_code == 400
    assert client.get("/admin/alerts", params={"limit": 1000}).status_code == 422
    assert client.get("/admin/alerts", params={"page": 0}).status_code == 422


def test_resolve_alert_marks_resolved_and_audits(setup):
    client, db = setup
    _as_admin()
    alert = db.admin_alerts.docs[1]
    resp = client.post(f"/admin/alerts/{alert['_id']}/resolve")

    assert resp.status_code == 200
    assert resp.json() == {"id": str(alert["_id"]), "resolved": True, "already_resolved": False}
    assert alert["resolved"] is True and alert["resolved_by"] == "owner@example.com"
    assert alert["resolved_at"] is not None
    assert len(db.admin_audit.docs) == 1

    again = client.post(f"/admin/alerts/{alert['_id']}/resolve").json()
    assert again["already_resolved"] is True
    assert len(db.admin_audit.docs) == 1


def test_resolve_unknown_alert_404(setup):
    client, _db = setup
    _as_admin()
    assert client.post(f"/admin/alerts/{ObjectId()}/resolve").status_code == 404
    assert client.post("/admin/alerts/not-an-id/resolve").status_code == 404
