"""有限奇偶博弈：构造、优先级、求解与位置策略的单元测试。"""

from __future__ import annotations

import pytest

from app.mcalc import evaluate, parse_formula, validate
from app.pgame import (
    CHALLENGER,
    VERIFIER,
    build_parity_game,
    solve_parity_game,
    to_nnf,
    verify_solution,
)


def build(formula, labels, edges, locations, initial=None):
    ast = validate(parse_formula(formula), set(labels))
    order = sorted(locations)
    edge_model = {s: [] for s in order}
    for src, dst in edges:
        edge_model[src].append(dst)
    game = build_parity_game(ast, {k: set(v) for k, v in labels.items()},
                             edge_model, order,
                             initial if initial is not None else order[0])
    return game, solve_parity_game(game)


def winner_at(game, sol, loc, node=0):
    v = game.index_of[(loc, node)]
    if v in sol.regions[0]:
        return VERIFIER
    assert v in sol.regions[1]
    return CHALLENGER


# --------------------------------------------------------------------------
# 否定范式与对偶化
# --------------------------------------------------------------------------


def test_nnf_pushes_negation_onto_propositions():
    from app.mcalc import pretty
    ast = validate(parse_formula("!(p & <>q)"), {"p", "q"})
    assert pretty(to_nnf(ast)) == "(!p | []!q)"


def test_nnf_dualizes_fixpoint_under_negation():
    from app.mcalc import pretty
    ast = validate(parse_formula("!νX.(p & []X)"), {"p"})
    assert pretty(to_nnf(ast)) == "μX.(!p | <>X)"
    ast2 = validate(parse_formula("!μX.(p | <>X)"), {"p"})
    assert pretty(to_nnf(ast2)) == "νX.(!p & []X)"


# --------------------------------------------------------------------------
# 构造：顶点 = 位置 × 闭包；优先级按绑定层级
# --------------------------------------------------------------------------


def test_vertices_are_location_times_closure_with_stable_ids():
    game, _ = build("νX.(safe & []X)", {"safe": ["s0", "s1"]},
                    [("s0", "s0"), ("s1", "s0")], ["s0", "s1"])
    # 闭包：νX. / (safe & []X) / safe / []X / X 共 5 节点 × 2 位置
    assert len(game.vertices) == 10
    v0 = game.vertices[game.index_of[("s0", 0)]]
    assert v0.vid == "s0#0" and v0.kind == "nu"
    assert game.vertices[game.initial].vid == "s0#0"


def test_owners_at_choice_points():
    game, _ = build("νX.((p | <>X) & []X)", {"p": ["a"]},
                    [("a", "a")], ["a"])
    owners = {nd.kind: game.vertices[game.index_of[("a", nd.node)]].owner
              for nd in game.nodes}
    assert owners["disjunction"] == VERIFIER
    assert owners["diamond"] == VERIFIER
    assert owners["conjunction"] == CHALLENGER
    assert owners["box"] == CHALLENGER
    assert owners["nu"] == "deterministic"


def test_priorities_follow_binder_nesting():
    # ν 外 μ 内：外层优先级更高；ν 偶、μ 奇
    game, _ = build("νX.μY.((p & []X) | <>Y)", {"p": ["a"]},
                    [("a", "a")], ["a"])
    prio = {nd.variable: nd.priority for nd in game.nodes
            if nd.kind in ("mu", "nu")}
    assert prio["X"] == 4 and prio["X"] % 2 == 0   # 外层 ν，最显著
    assert prio["Y"] == 3 and prio["Y"] % 2 == 1   # 内层 μ
    assert prio["X"] > prio["Y"]


def test_deadlock_diamond_loses_box_wins():
    # 无后继位置：<> 验证方败（自环奇优先级），[] 空洞为真（自环偶优先级）
    game, sol = build("<>p", {"p": ["a"]}, [], ["a", "b"], initial="b")
    v = game.index_of[("b", 0)]
    assert game.succ[v] == [v] and game.priority[v] % 2 == 1
    assert winner_at(game, sol, "b") == CHALLENGER

    game2, sol2 = build("[]p", {"p": ["a"]}, [], ["a", "b"], initial="b")
    v2 = game2.index_of[("b", 0)]
    assert game2.succ[v2] == [v2] and game2.priority[v2] % 2 == 0
    assert winner_at(game2, sol2, "b") == VERIFIER


# --------------------------------------------------------------------------
# 求解与策略
# --------------------------------------------------------------------------


def test_safe_self_loop_verifier_wins_everywhere():
    game, sol = build("νX.(safe & []X)", {"safe": ["s0", "s1"]},
                      [("s0", "s0"), ("s1", "s0")], ["s0", "s1"], initial="s0")
    assert winner_at(game, sol, "s0") == VERIFIER
    assert sol.regions[1] == set()
    checks = verify_solution(game, sol)
    assert checks == {0: True, 1: True}


def test_dangerous_successor_challenger_wins_with_strategy():
    game, sol = build("νX.(safe & []X)", {"safe": ["s0"]},
                      [("s0", "s0"), ("s0", "d")], ["d", "s0"], initial="s0")
    assert winner_at(game, sol, "s0") == CHALLENGER
    strat = sol.strategies[1]
    # 挑战方在 s0 的合取选择 []X 支（而非 safe 支），在 [] 选危险迁移到 d
    conj = game.index_of[("s0", 1)]
    box = game.index_of[("s0", 3)]
    assert strat[conj] == box
    tgt = game.vertices[strat[box]]
    assert tgt.location == "d"
    checks = verify_solution(game, sol)
    assert checks[1] is True


def test_mu_reachability_verifier_strategy_points_to_goal():
    game, sol = build("μX.(goal | <>X)", {"goal": ["g"]},
                      [("s0", "s1"), ("s1", "g"), ("g", "g"), ("x", "x")],
                      ["g", "s0", "s1", "x"], initial="s0")
    assert winner_at(game, sol, "s0") == VERIFIER
    assert winner_at(game, sol, "x") == CHALLENGER  # x 不可达 goal
    strat = sol.strategies[0]
    # s1 的 <> 选择点必须走向 g
    dia_s1 = game.index_of[("s1", 3)]
    assert game.vertices[strat[dia_s1]].location == "g"
    checks = verify_solution(game, sol)
    assert checks == {0: True, 1: True}


def test_nested_alternation_nu_mu():
    # νX.μY.((p & []X) | <>Y)：从任意位置可反复回到 p —— 验证方在 a 胜
    game, sol = build("νX.μY.((p & []X) | <>Y)", {"p": ["a"]},
                      [("a", "a"), ("a", "b"), ("b", "a"), ("b", "b")],
                      ["a", "b"], initial="a")
    assert winner_at(game, sol, "a") == VERIFIER
    checks = verify_solution(game, sol)
    assert checks == {0: True, 1: True}


def test_regions_partition_and_match_mcalc():
    labels = {"safe": {"s0"}, "goal": {"g"}}
    edges = [("s0", "s0"), ("s0", "d"), ("s0", "g"), ("g", "g"), ("d", "d")]
    for formula in ["νX.(safe & []X)", "μX.(goal | <>X)",
                    "!νX.(safe & []X)", "νX.μY.((safe & []X) | <>Y)"]:
        ast = validate(parse_formula(formula), set(labels))
        order = sorted(["d", "g", "s0"])
        em = {s: [] for s in order}
        for a, b in edges:
            em[a].append(b)
        r = evaluate(ast, {k: set(v) for k, v in labels.items()}, em, order, "s0")
        game, sol = build(formula, labels, edges, ["d", "g", "s0"], initial="s0")
        assert sol.regions[0] | sol.regions[1] == set(range(len(game.vertices)))
        assert not (sol.regions[0] & sol.regions[1])
        for loc in order:
            expect = VERIFIER if loc in set(r.satisfied) else CHALLENGER
            assert winner_at(game, sol, loc) == expect, (formula, loc)
        checks = verify_solution(game, sol)
        assert checks == {0: True, 1: True}, formula
