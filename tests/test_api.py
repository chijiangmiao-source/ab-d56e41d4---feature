"""HTTP 接口冒烟层：编号持久化、按编号读取、拒绝请求不产生编号。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.main as main


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "reviews.db"))
    with TestClient(main.app) as c:
        yield c


def safe_payload():
    return {
        "locations": ["s0", "s1"],
        "initial": "s0",
        "propositions": [
            {"location": "s0", "proposition": "safe"},
            {"location": "s1", "proposition": "safe"},
        ],
        "transitions": [
            {"id": "e1", "source": "s0", "target": "s0"},
            {"id": "e2", "source": "s1", "target": "s0"},
        ],
        "formula": "νX.(safe & []X)",
    }


def reach_payload():
    return {
        "locations": ["g", "s0", "s1", "x"],
        "initial": "s0",
        "propositions": [{"location": "g", "proposition": "goal"}],
        "transitions": [
            {"id": "e1", "source": "s0", "target": "s1"},
            {"id": "e2", "source": "s1", "target": "g"},
            {"id": "e3", "source": "g", "target": "g"},
            {"id": "e4", "source": "x", "target": "x"},
        ],
        "formula": "μX.(goal | <>X)",
    }


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["reviews"] == 0


def test_create_returns_persistent_id_and_can_read_back(client):
    r = client.post("/api/v1/reviews", json=safe_payload())
    assert r.status_code == 201
    rid = r.json()["review_id"]
    assert isinstance(rid, int)

    got = client.get(f"/api/v1/reviews/{rid}")
    assert got.status_code == 200
    body = got.json()
    assert body["review_id"] == rid
    assert body["conclusion"]["initial_satisfied"] is True
    assert body["conclusion"]["satisfied_set"] == ["s0", "s1"]
    tr = body["evidence"]["fixpoint_iterations"][0]
    assert tr["fixpoint"] == "nu"
    assert tr["stable"] == ["s0", "s1"]
    # 稳定证据：最后两轮相等
    assert tr["stages"][-1]["locations"] == tr["stages"][-2]["locations"]


def test_mu_reachability_via_http(client):
    r = client.post("/api/v1/reviews", json=reach_payload())
    assert r.status_code == 201
    body = r.json()
    assert body["conclusion"]["initial_satisfied"] is True
    assert body["conclusion"]["satisfied_set"] == ["g", "s0", "s1"]
    tr = body["evidence"]["fixpoint_iterations"][0]
    assert tr["fixpoint"] == "mu"
    assert tr["start"] == []
    assert tr["stages"][-1]["locations"] == ["g", "s0", "s1"]

    reread = client.get(f"/api/v1/reviews/{body['review_id']}").json()
    assert reread["conclusion"]["satisfied_set"] == ["g", "s0", "s1"]


def test_dangerous_transition_via_http(client):
    payload = safe_payload()
    payload["locations"] = ["d", "s0"]
    payload["propositions"] = [{"location": "s0", "proposition": "safe"}]
    payload["transitions"] = [
        {"id": "e1", "source": "s0", "target": "s0"},
        {"id": "e2", "source": "s0", "target": "d"},
    ]
    r = client.post("/api/v1/reviews", json=payload)
    assert r.status_code == 201
    body = r.json()
    assert body["conclusion"]["initial_satisfied"] is False
    assert body["conclusion"]["release_permitted"] is False
    assert body["conclusion"]["satisfied_set"] == []

    reread = client.get(f"/api/v1/reviews/{body['review_id']}").json()
    assert reread["evidence"]["fixpoint_iterations"][0]["stable"] == []


def test_rejected_request_gets_no_id_and_leaves_nothing_readable(client):
    ok = client.post("/api/v1/reviews", json=safe_payload())
    assert ok.status_code == 201
    first_id = ok.json()["review_id"]

    bad = safe_payload()
    bad["transitions"].append({"id": "e9", "source": "s0", "target": "ghost"})
    r = client.post("/api/v1/reviews", json=bad)
    assert r.status_code == 422
    err = r.json()
    assert err["error"]["code"] == "DANGLING_TRANSITION"
    assert err["persisted"] is False
    assert err["review_id"] is None

    # 被拒绝后，新的成功复核编号紧接已持久化记录；不存在编号 2 的记录
    ok2 = client.post("/api/v1/reviews", json=safe_payload())
    assert ok2.status_code == 201
    assert ok2.json()["review_id"] == first_id + 1
    assert client.get(f"/api/v1/reviews/{first_id + 1}").status_code == 200
    assert client.get("/api/v1/reviews/9999").status_code == 404


@pytest.mark.parametrize(
    "mutate,code",
    [
        (lambda p: p.update(locations=["s0"]), "INVALID_LOCATIONS"),
        (lambda p: p.update(locations=["s0", "s0"]), "DUPLICATE_LOCATION"),
        (lambda p: p["transitions"].append(dict(p["transitions"][0])),
         "DUPLICATE_TRANSITION_ID"),
        (lambda p: p.update(formula="μX.X"), "FORMULA_INVALID"),
        (lambda p: p.update(formula="νX.νX.[]X"), "FORMULA_INVALID"),
        (lambda p: p.update(formula="p )"), "FORMULA_SYNTAX"),
        (lambda p: p.update(initial="nope"), "UNKNOWN_INITIAL"),
    ],
)
def test_all_rejection_classes_via_http(client, mutate, code):
    payload = safe_payload()
    mutate(payload)
    r = client.post("/api/v1/reviews", json=payload)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == code


def test_malformed_json_body(client):
    r = client.post("/api/v1/reviews", content=b"{not json",
                    headers={"content-type": "application/json"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_JSON"
