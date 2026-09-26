"""策略审计：审计记录结构、无记忆策略、对手选项与持久化读取。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.audit import build_audit
from app.service import build_review


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


def danger_payload():
    return {
        "locations": ["d", "s0"],
        "initial": "s0",
        "propositions": [{"location": "s0", "proposition": "safe"}],
        "transitions": [
            {"id": "e1", "source": "s0", "target": "s0"},
            {"id": "e2", "source": "s0", "target": "d"},
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


def replay(audit):
    """逐步复算：自初始顶点出发，胜方按策略走子，对手穷举每种合法选择。"""
    verts = {v["id"]: v for v in audit["game"]["vertices"]}
    moves = {m["vertex"]: m["choose"] for m in audit["strategy"]["moves"]}
    winner = audit["result"]["initial_winner"]
    seen, stack = set(), [audit["game"]["initial_vertex"]]
    while stack:
        vid = stack.pop()
        if vid in seen:
            continue
        seen.add(vid)
        v = verts[vid]
        assert v["region"] == winner, f"顶点 v{vid} 逸出胜方区域"
        if v["terminal"]:
            assert v["terminal_winner"] == winner
            continue
        if v["owner"] == winner:
            nxt = moves[vid]
            assert nxt in v["successors"], "策略移动必须是合法选择"
            stack.append(nxt)
        else:
            stack.extend(v["successors"])
    return seen


# --------------------------------------------------------------------------
# 审计记录结构与安全自循环：验证方策略
# --------------------------------------------------------------------------


def test_safe_self_loop_audit_verifier_strategy():
    rec = build_review(safe_payload())
    audit = build_audit(rec, 1)

    assert audit["review_id"] == 1
    assert audit["source_review"]["initial_satisfied"] is True
    assert audit["formula_summary"]["normalized"] == "νX.(safe & []X)"
    assert audit["formula_summary"]["binders"][0]["fixpoint"] == "nu"
    assert audit["result"]["initial_winner"] == "verifier"
    assert audit["result"]["initial_satisfied"] is True
    assert audit["result"]["consistent_with_review"] is True
    assert audit["strategy"]["player"] == "verifier"
    assert audit["strategy"]["memoryless"] is True
    assert audit["strategy"]["moves"], "验证方策略非空"

    # 挑战方每种合法选择都进入验证方胜区
    for entry in audit["opponent_options"]:
        assert entry["in_winner_region"] is True
        for opt in entry["options"]:
            assert opt["region"] == "verifier"

    replay(audit)


def test_danger_audit_challenger_strategy_points_to_danger():
    rec = build_review(danger_payload())
    audit = build_audit(rec, 1)

    assert audit["result"]["initial_winner"] == "challenger"
    assert audit["result"]["initial_satisfied"] is False
    assert audit["result"]["consistent_with_review"] is True
    assert audit["strategy"]["player"] == "challenger"

    # 挑战方在 s0 的 [] 模态选择点指向危险位置 d
    box = [m for m in audit["strategy"]["moves"]
           if m["kind"] == "box" and m["location"] == "s0"]
    assert box and box[0]["choose_location"] == "d"
    assert box[0]["choice_kind"] == "box"

    replay(audit)


def test_reach_audit_verifier_choices_and_steppable_recompute():
    rec = build_review(reach_payload())
    audit = build_audit(rec, 1)

    assert audit["result"]["initial_winner"] == "verifier"
    moves = {(m["location"], m["kind"]): m for m in audit["strategy"]["moves"]}
    assert moves[("g", "or")]["choose_formula"] == "goal"
    assert moves[("s1", "diamond")]["choose_location"] == "g"
    assert moves[("s0", "diamond")]["choose_location"] == "s1"

    seen = replay(audit)
    # 复算覆盖 g/s0/s1 三处的根公式顶点，x 不可达（挑战方区域）
    roots = {(v["location"], v["formula"], v["polarity"]): v["id"]
             for v in audit["game"]["vertices"]}
    for loc in ("g", "s0", "s1"):
        assert roots[(loc, "μX.(goal | <>X)", "+")] in seen
    verts = {v["id"]: v for v in audit["game"]["vertices"]}
    assert verts[roots[("x", "μX.(goal | <>X)", "+")]]["region"] == "challenger"


def test_opponent_options_cover_every_legal_choice():
    rec = build_review(reach_payload())
    audit = build_audit(rec, 1)
    verts = {v["id"]: v for v in audit["game"]["vertices"]}
    loser = "challenger"  # 本例胜方为验证方

    listed = {o["vertex"]: o for o in audit["opponent_options"]}
    expected = {v["id"] for v in verts.values() if v["owner"] == loser}
    assert set(listed) == expected, "对手每个顶点都必须列出"
    for vid, entry in listed.items():
        targets = {opt["vertex"] for opt in entry["options"]}
        assert targets == set(verts[vid]["successors"]), "每种合法选择都须列出"
        for opt in entry["options"]:
            assert opt["region"] == verts[opt["vertex"]]["region"]


def test_audit_deterministic_across_builds():
    rec = build_review(reach_payload())
    a1 = build_audit(rec, 1)
    a2 = build_audit(rec, 1)
    for key in ("game", "result", "strategy", "opponent_options", "formula_summary"):
        assert a1[key] == a2[key]


# --------------------------------------------------------------------------
# HTTP：创建审计、按编号读取、复核记录不受影响
# --------------------------------------------------------------------------


def test_audit_http_create_and_read_back(client):
    r = client.post("/api/v1/reviews", json=safe_payload())
    assert r.status_code == 201
    rid = r.json()["review_id"]
    before = client.get(f"/api/v1/reviews/{rid}").json()

    r = client.post(f"/api/v1/reviews/{rid}/audits")
    assert r.status_code == 201
    audit = r.json()
    aid = audit["audit_id"]
    assert isinstance(aid, int)
    assert audit["review_id"] == rid
    assert audit["result"]["initial_winner"] == "verifier"
    assert audit["strategy"]["player"] == "verifier"

    got = client.get(f"/api/v1/audits/{aid}")
    assert got.status_code == 200
    body = got.json()
    assert body["audit_id"] == aid
    assert body["review_id"] == rid
    assert body["strategy"] == audit["strategy"]
    assert body["opponent_options"] == audit["opponent_options"]
    replay(body)

    # 原复核读取结果不受审计影响：满足集与固定点证据保持一致
    after = client.get(f"/api/v1/reviews/{rid}").json()
    assert after["conclusion"] == before["conclusion"]
    assert after["evidence"] == before["evidence"]

    health = client.get("/healthz").json()
    assert health["reviews"] == 1 and health["audits"] == 1


def test_audit_http_danger_challenger(client):
    r = client.post("/api/v1/reviews", json=danger_payload())
    rid = r.json()["review_id"]
    r = client.post(f"/api/v1/reviews/{rid}/audits")
    assert r.status_code == 201
    audit = r.json()
    assert audit["result"]["initial_winner"] == "challenger"
    assert audit["strategy"]["player"] == "challenger"
    box = [m for m in audit["strategy"]["moves"]
           if m["kind"] == "box" and m["location"] == "s0"]
    assert box[0]["choose_location"] == "d"

    got = client.get(f"/api/v1/audits/{audit['audit_id']}")
    assert got.status_code == 200
    assert got.json()["result"]["initial_winner"] == "challenger"


def test_audit_http_not_found(client):
    assert client.post("/api/v1/reviews/9999/audits").status_code == 404
    assert client.get("/api/v1/audits/9999").status_code == 404
