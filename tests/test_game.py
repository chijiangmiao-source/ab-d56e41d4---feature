"""奇偶博弈构建与求解的单元测试：区域与固定点语义逐位置对拍。"""

from __future__ import annotations

import pytest

from app.game import CHALLENGER, VERIFIER, build_game, solve_game
from app.mcalc import evaluate, parse_formula, pretty, validate


def make_model(labels, edges, locations):
    order = sorted(locations)
    edge_model = {s: [] for s in order}
    for src, dst in edges:
        edge_model[src].append(dst)
    return order, edge_model, {k: set(v) for k, v in labels.items()}


def root_vertex(game, ast, location):
    """完整公式在给定位置、正极性下的顶点 id。"""
    text = pretty(ast)
    for v in game.vertices:
        if v.location == location and v.polarity == "+" and v.formula_text == text:
            return v.id
    raise AssertionError(f"位置 {location} 缺少根公式顶点")


# --------------------------------------------------------------------------
# 对拍：每个位置的博弈胜方 == Knaster–Tarski 固定点求值结果
# --------------------------------------------------------------------------

SAFE_LOOP = (
    {"safe": ["s0", "s1"]},
    [("s0", "s0"), ("s1", "s0")],
    ["s0", "s1"],
)
DANGER = (
    {"safe": ["s0"]},
    [("s0", "s0"), ("s0", "d")],
    ["d", "s0"],
)
REACH = (
    {"goal": ["g"]},
    [("s0", "s1"), ("s1", "g"), ("g", "g"), ("x", "x")],
    ["g", "s0", "s1", "x"],
)
NESTED = (
    {"safe": ["s0", "ok"], "ok": ["ok"]},
    [("s0", "s0"), ("t", "ok"), ("ok", "ok")],
    ["ok", "s0", "t"],
)
BRANCH = (
    {"p": ["b"]},
    [("a", "b"), ("a", "c"), ("b", "b"), ("c", "c")],
    ["a", "b", "c"],
)

CASES = [
    ("νX.(safe & []X)", *SAFE_LOOP),
    ("νX.(safe & []X)", *DANGER),
    ("μX.(goal | <>X)", *REACH),
    ("νX.((safe & []X) | μY.(ok | <>Y))", *NESTED),
    ("μY.μX.(goal | <>X)", *REACH),
    ("νX.(μY.(goal | <>Y) & []X)", *REACH),
    ("!μX.(p | <>X)", *BRANCH),
    ("μX.(p | ![]!X)", *BRANCH),
    ("νX.(μY.(p | <>Y) & []X)", *BRANCH),
    ("!(p & <>p)", *BRANCH),
    ("[]p", *BRANCH),
    ("<>p", *BRANCH),
]


@pytest.mark.parametrize("formula,labels,edges,locations", CASES)
def test_game_region_matches_fixpoint_evaluation(formula, labels, edges, locations):
    order, edge_model, label_sets = make_model(labels, edges, locations)
    ast = validate(parse_formula(formula), set(label_sets))
    expected = evaluate(ast, label_sets, edge_model, order, order[0])

    game = build_game(ast, label_sets, edge_model, order, order[0])
    sol = solve_game(game)

    for loc in order:
        vid = root_vertex(game, ast, loc)
        winner = sol.region[vid]
        assert (winner == VERIFIER) == (loc in expected.satisfied), (
            f"{formula} @ {loc}: 博弈胜方 {winner} 与满足集 {expected.satisfied} 不一致"
        )


# --------------------------------------------------------------------------
# 安全自循环：验证方胜，挑战方无处可逃
# --------------------------------------------------------------------------


def test_safe_self_loop_verifier_wins_and_challenger_trapped():
    order, edge_model, labels = make_model(*SAFE_LOOP)
    ast = validate(parse_formula("νX.(safe & []X)"), set(labels))
    game = build_game(ast, labels, edge_model, order, "s0")
    sol = solve_game(game)

    assert sol.initial_winner == VERIFIER
    # 正极性下公式在各位置均成立：根公式顶点都在验证方胜区
    for loc in order:
        assert sol.region[root_vertex(game, ast, loc)] == VERIFIER
    # 挑战方在验证方胜区内的每个合法选择都无法逃脱该区域
    for v in game.vertices:
        if v.owner == CHALLENGER and sol.region[v.id] == VERIFIER:
            assert all(sol.region[t] == VERIFIER for t in v.successors)


def test_dangerous_successor_challenger_wins_and_targets_danger():
    order, edge_model, labels = make_model(*DANGER)
    ast = validate(parse_formula("νX.(safe & []X)"), set(labels))
    game = build_game(ast, labels, edge_model, order, "s0")
    sol = solve_game(game)

    assert sol.initial_winner == CHALLENGER
    # 挑战方策略在 s0 的 [] 选择点必须指向危险位置 d
    box_moves = {
        v.location: sol.strategy[v.id]
        for v in game.vertices
        if v.kind == "box" and v.polarity == "+" and v.id in sol.strategy
        and sol.region[v.id] == CHALLENGER
    }
    chosen = game.vertices[box_moves["s0"]]
    assert chosen.location == "d"


def test_mu_reachability_verifier_strategy_reaches_goal():
    order, edge_model, labels = make_model(*REACH)
    ast = validate(parse_formula("μX.(goal | <>X)"), set(labels))
    game = build_game(ast, labels, edge_model, order, "s0")
    sol = solve_game(game)

    assert sol.initial_winner == VERIFIER
    by_key = {(v.location, v.kind, v.polarity): v for v in game.vertices}
    # g 处析取选择 goal（直接成立的支路）
    g_or = by_key[("g", "or", "+")]
    assert game.vertices[sol.strategy[g_or.id]].formula_text == "goal"
    # s1 处模态选择迁移到 g
    s1_dia = by_key[("s1", "diamond", "+")]
    assert game.vertices[sol.strategy[s1_dia.id]].location == "g"
    # x 不可达 goal：挑战方胜
    x_root = root_vertex(game, ast, "x")
    assert sol.region[x_root] == CHALLENGER


def test_strategy_is_memoryless_and_deterministic():
    order, edge_model, labels = make_model(*REACH)
    ast = validate(parse_formula("μX.(goal | <>X)"), set(labels))
    game = build_game(ast, labels, edge_model, order, "s0")
    first = solve_game(game)
    second = solve_game(game)
    # 位置策略：每个顶点至多一个选定后继，与到达历史无关
    assert all(isinstance(t, int) for t in first.strategy.values())
    # 稳定裁决：同一博弈重复求解得到同一策略与区域
    assert first.strategy == second.strategy
    assert first.region == second.region


def test_negated_binder_flips_effective_priority():
    order, edge_model, labels = make_model(*BRANCH)
    ast = validate(parse_formula("!μX.(p | <>X)"), set(labels))
    game = build_game(ast, labels, edge_model, order, "a")
    sol = solve_game(game)
    # a 可达 p（a->b），故 !μX.(p | <>X) 在 a 不成立：挑战方胜
    assert sol.initial_winner == CHALLENGER
    # c 不可达 p：验证方胜
    assert sol.region[root_vertex(game, ast, "c")] == VERIFIER
    # 否定极性下的 μ 绑定顶点按对偶取偶优先级（等效 ν）
    binder = next(v for v in game.vertices
                  if v.kind == "binder" and v.polarity == "-")
    assert binder.priority % 2 == 0


def test_binding_hierarchy_priorities_outer_dominates_inner():
    order, edge_model, labels = make_model(*REACH)
    ast = validate(parse_formula("νX.(μY.(goal | <>Y) & []X)"), set(labels))
    game = build_game(ast, labels, edge_model, order, "s0")
    assert [b["nesting_depth"] for b in game.binders] == [0, 1]
    outer, inner = game.binders
    assert outer["fixpoint"] == "nu" and inner["fixpoint"] == "mu"
    assert outer["priority_base"] > inner["priority_base"]
    nu_vertex = next(v for v in game.vertices
                     if v.kind == "binder" and v.polarity == "+"
                     and v.formula_text.startswith("ν"))
    mu_vertex = next(v for v in game.vertices
                     if v.kind == "binder" and v.polarity == "+"
                     and v.formula_text.startswith("μ"))
    assert nu_vertex.priority % 2 == 0 and mu_vertex.priority % 2 == 1
    assert nu_vertex.priority > mu_vertex.priority
