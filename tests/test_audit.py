"""策略审计载荷层：胜方区域、位置策略、对手选择逐步复算与持久化内容。"""

from __future__ import annotations

from app.audit import build_audit
from app.service import build_review


def safe_review():
    return build_review({
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
    })


def danger_review():
    return build_review({
        "locations": ["d", "s0"],
        "initial": "s0",
        "propositions": [{"location": "s0", "proposition": "safe"}],
        "transitions": [
            {"id": "e1", "source": "s0", "target": "s0"},
            {"id": "e2", "source": "s0", "target": "d"},
        ],
        "formula": "νX.(safe & []X)",
    })


def test_audit_carries_source_review_and_formula_summary():
    audit = build_audit(7, safe_review())
    assert audit["source"]["review_id"] == 7
    assert audit["source"]["initial_location"] == "s0"
    assert audit["formula"]["source"] == "νX.(safe & []X)"
    assert audit["formula"]["nnf"] == "νX.(safe & []X)"
    binder = audit["formula"]["binders"][0]
    assert binder["variable"] == "X" and binder["fixpoint"] == "nu"
    assert binder["priority"] % 2 == 0
    assert binder["dualized_by_negation"] is False


def test_every_vertex_has_region_and_all_moves_annotated():
    audit = build_audit(1, safe_review())
    verts = audit["game"]["vertices"]
    assert len(verts) == audit["game"]["vertex_count"]
    regions = set(audit["winning_regions"]["verifier"]) | set(
        audit["winning_regions"]["challenger"])
    assert regions == {v["id"] for v in verts}
    for v in verts:
        assert v["region"] in ("verifier", "challenger")
        for m in v["moves"]:
            assert m["target_region"] in ("verifier", "challenger")
            # 模态选择必须带迁移标识，布尔/绑定跳转不带
            if v["kind"] in ("diamond", "box") and not v["terminal_self_loop"]:
                assert m["via_transitions"]
            else:
                assert m["via_transitions"] == []


def test_opponent_every_legal_choice_stays_in_winner_region():
    audit = build_audit(1, safe_review())
    assert audit["conclusion"]["winner"] == "verifier"
    opp = audit["opponent_choices"]
    assert opp["player"] == "challenger"
    assert opp["all_choices_stay_in_winner_region"] is True
    assert opp["vertices"]  # 合取与 [] 选择点都被列出
    # 逐步复算：s0 的 [] 选择点合法选择是经 e1 自环，进入验证方胜区
    s0_box = next(info for vid, info in opp["vertices"].items()
                  if vid.startswith("s0#") and info["formula"] == "[]X")
    assert len(s0_box["choices"]) == 1
    choice = s0_box["choices"][0]
    assert choice["via_transitions"] == ["e1"]
    assert choice["to_location"] == "s0"
    assert choice["target_region"] == "verifier"


def test_dangerous_review_audit_challenger_strategy_is_positional():
    audit = build_audit(3, danger_review())
    assert audit["conclusion"]["winner"] == "challenger"
    strat = audit["strategy"]
    assert strat["player"] == "challenger" and strat["memoryless"] is True
    # 策略键只依赖顶点（位置 # 闭包节点），两次构建裁决稳定一致
    again = build_audit(3, danger_review())
    assert again["strategy"]["moves"] == strat["moves"]
    # 合取选择点裁决：s0 处选 []X 支（safe 在 s0 成立，选 safe 支挑战方必败），
    # d 处选 safe 支（safe 在 d 不成立，命题终端挑战方即胜）
    assert strat["moves"]["s0#1"]["to_formula"] == "[]X"
    assert strat["moves"]["d#1"]["to_formula"] == "safe"
    # 模态选择点裁决危险迁移
    box_moves = [m for m in strat["moves"].values() if m["via_transitions"]]
    assert any(m["via_transitions"] == ["e2"] and m["to_location"] == "d"
               for m in box_moves)


def test_conclusion_consistent_with_review_satisfaction():
    for review, winner in ((safe_review(), "verifier"),
                           (danger_review(), "challenger")):
        audit = build_audit(1, review)
        assert audit["conclusion"]["winner"] == winner
        assert audit["conclusion"]["consistent_with_review"] is True
        assert audit["conclusion"]["solver_self_check"] == {
            "verifier_region_strategy_valid": True,
            "challenger_region_strategy_valid": True,
        }


def test_negated_formula_dualizes_binder_in_summary():
    review = build_review({
        "locations": ["d", "s0"],
        "initial": "s0",
        "propositions": [{"location": "s0", "proposition": "safe"}],
        "transitions": [
            {"id": "e1", "source": "s0", "target": "s0"},
            {"id": "e2", "source": "s0", "target": "d"},
        ],
        "formula": "!νX.(safe & []X)",
    })
    audit = build_audit(1, review)
    assert audit["formula"]["nnf"] == "μX.(!safe | <>X)"
    binder = audit["formula"]["binders"][0]
    assert binder["fixpoint"] == "mu" and binder["dualized_by_negation"] is True
    # s0 原 ν 公式不满足，故其否定满足；博弈初始顶点验证方胜
    assert audit["conclusion"]["winner"] == "verifier"
