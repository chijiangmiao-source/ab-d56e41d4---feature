"""服务层：模型校验、拒绝路径不发编号证据语义。"""

from __future__ import annotations

import pytest

from app.service import RequestError, build_review


def safe_self_loop_payload():
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


def reach_goal_payload():
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


def test_safe_self_loop_review_persistable_record():
    rec = build_review(safe_self_loop_payload())
    assert rec["conclusion"]["initial_satisfied"] is True
    assert rec["conclusion"]["release_permitted"] is True
    assert rec["conclusion"]["satisfied_set"] == ["s0", "s1"]
    tr = rec["evidence"]["fixpoint_iterations"][0]
    assert tr["fixpoint"] == "nu"
    assert tr["start"] == ["s0", "s1"]
    assert tr["stable"] == ["s0", "s1"]
    assert "Knaster" in tr["stability_evidence"]


def test_mu_reachability_review_extends_to_initial():
    rec = build_review(reach_goal_payload())
    assert rec["conclusion"]["initial_satisfied"] is True
    assert rec["conclusion"]["satisfied_set"] == ["g", "s0", "s1"]
    tr = rec["evidence"]["fixpoint_iterations"][0]
    assert tr["fixpoint"] == "mu"
    assert tr["start"] == []
    stages = [s["locations"] for s in tr["stages"]]
    # 第 0 轮是 μ 的起点 ∅，随后 goal 及其前驱逐层扩展
    assert stages[:4] == [[], ["g"], ["g", "s1"], ["g", "s0", "s1"]]
    assert stages[-1] == stages[-2]


def test_dangerous_transition_makes_formula_false():
    payload = safe_self_loop_payload()
    payload["locations"] = ["d", "s0"]
    payload["propositions"] = [{"location": "s0", "proposition": "safe"}]
    payload["transitions"] = [
        {"id": "e1", "source": "s0", "target": "s0"},
        {"id": "e2", "source": "s0", "target": "d"},
    ]
    rec = build_review(payload)
    assert rec["conclusion"]["initial_satisfied"] is False
    assert rec["conclusion"]["release_permitted"] is False
    assert rec["conclusion"]["satisfied_set"] == []
    tr = rec["evidence"]["fixpoint_iterations"][0]
    assert tr["stable"] == []
    assert tr["stages"][-1]["locations"] == tr["stages"][-2]["locations"] == []


def test_satisfaction_set_sorted_by_location_identifier():
    payload = reach_goal_payload()
    payload["locations"] = ["g", "b", "a"]  # 提交顺序无序
    payload["initial"] = "a"
    payload["transitions"] = [
        {"id": "e1", "source": "a", "target": "b"},
        {"id": "e2", "source": "b", "target": "g"},
        {"id": "e3", "source": "g", "target": "g"},
    ]
    rec = build_review(payload)
    assert rec["conclusion"]["satisfied_set"] == ["a", "b", "g"]
    assert rec["model"]["location_order"] == ["a", "b", "g"]


@pytest.mark.parametrize(
    "mutate,code",
    [
        (lambda p: p.update(locations=["s0"]), "INVALID_LOCATIONS"),
        (lambda p: p.update(locations=["s0", "s0"]), "DUPLICATE_LOCATION"),
        (lambda p: p.update(locations=[f"s{i}" for i in range(25)]),
         "INVALID_LOCATIONS"),
        (lambda p: p.update(initial="zzz"), "UNKNOWN_INITIAL"),
        (lambda p: p["transitions"].append(
            {"id": "e9", "source": "s0", "target": "ghost"}),
         "DANGLING_TRANSITION"),
        (lambda p: p["transitions"].append(
            {"id": "e9", "source": "ghost", "target": "s0"}),
         "DANGLING_TRANSITION"),
        (lambda p: p["transitions"].append(dict(p["transitions"][0])),
         "DUPLICATE_TRANSITION_ID"),
        (lambda p: p.update(formula="μX.<>Y"), "FORMULA_INVALID"),
        (lambda p: p.update(formula="νX.μX.[]X"), "FORMULA_INVALID"),
        (lambda p: p.update(formula="safe &"), "FORMULA_SYNTAX"),
        (lambda p: p.update(formula="safe -> safe"), "FORMULA_SYNTAX"),
        (lambda p: p.update(formula="νX.(unknownprop & []X)"), "FORMULA_INVALID"),
        (lambda p: p["propositions"].append(
            {"location": "ghost", "proposition": "safe"}),
         "DANGLING_PROPOSITION_LOCATION"),
    ],
)
def test_invalid_requests_raise_and_never_persist(mutate, code):
    payload = safe_self_loop_payload()
    mutate(payload)
    with pytest.raises(RequestError) as exc:
        build_review(payload)
    assert exc.value.code == code
