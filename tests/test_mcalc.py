"""μ-演算语法、静态校验与固定点语义的单元测试。"""

from __future__ import annotations

import pytest

from app.mcalc import (
    SyntaxError_,
    ValidationError,
    evaluate,
    parse_formula,
    pretty,
    validate,
)


def solve(formula, labels, edges, locations, initial=None):
    ast = validate(parse_formula(formula), set(labels))
    order = sorted(locations)
    edge_model = {s: [] for s in order}
    for src, dst in edges:
        edge_model[src].append(dst)
    return evaluate(ast, {k: set(v) for k, v in labels.items()},
                    edge_model, order, initial if initial is not None else order[0])


# --------------------------------------------------------------------------
# ν 收敛：安全自循环 νX.(safe & []X)
# --------------------------------------------------------------------------


def test_nu_safe_self_loop_converges_and_satisfies():
    locs = ["s0", "s1"]
    labels = {"safe": ["s0", "s1"]}
    edges = [("s0", "s0"), ("s1", "s0")]
    r = solve("νX.(safe & []X)", labels, edges, locs, initial="s0")

    assert r.satisfied == ["s0", "s1"]
    assert r.initial_satisfied is True
    tr = r.traces[0]
    assert tr.kind == "nu"
    assert tr.start == ["s0", "s1"]          # ν 从全集出发
    assert tr.stable == ["s0", "s1"]
    assert tr.iterations[0] == ["s0", "s1"]
    assert tr.iterations[-1] == tr.iterations[-2]  # 稳定证据：相邻两轮相等
    assert tr.converged_at == 1  # 首轮 F(全集)=全集


def test_nu_dangerous_transition_breaks_formula():
    # s0 有安全自循环，但同时存在指向危险位置 d 的迁移。
    locs = ["d", "s0"]
    labels = {"safe": ["s0"]}
    edges = [("s0", "s0"), ("s0", "d")]
    r = solve("νX.(safe & []X)", labels, edges, locs, initial="s0")

    assert r.satisfied == []
    assert r.initial_satisfied is False
    tr = r.traces[0]
    # 全集 -> {s0} -> ∅ -> ∅，单调下降后稳定
    assert tr.iterations[0] == ["d", "s0"]
    assert tr.iterations[1] == ["s0"]
    assert tr.iterations[2] == []
    assert tr.stable == []
    assert tr.converged_at == 3  # 全集 -> {s0} -> ∅ -> ∅，第 3 轮重复


def test_nu_finite_path_is_not_infinite_safety():
    # s0 -> s1，s1 safe 且停滞：沿有限路径看似安全，但 []X 在停滞点仍要求
    # s1 属于固定点；s1 safe 且无后继，应保留。危险来自 d。
    locs = ["d", "s1"]
    labels = {"safe": ["s1"]}
    edges = [("s1", "d")]
    r = solve("νX.(safe & []X)", labels, edges, locs, initial="s1")
    assert r.satisfied == []  # s1 终将到达 d，无限行为不满足


# --------------------------------------------------------------------------
# μ 扩展：可达 goal 的 μX.(goal | <>X)
# --------------------------------------------------------------------------


def test_mu_reachability_extends_back_to_initial():
    locs = ["g", "s0", "s1", "x"]
    labels = {"goal": ["g"]}
    edges = [("s0", "s1"), ("s1", "g"), ("g", "g"), ("x", "x")]
    r = solve("μX.(goal | <>X)", labels, edges, locs, initial="s0")

    assert r.satisfied == ["g", "s0", "s1"]      # 按位置标识排序；x 不可达 goal
    assert r.initial_satisfied is True
    tr = r.traces[0]
    assert tr.kind == "mu"
    assert tr.start == []                         # μ 从空集出发
    assert tr.iterations[1] == ["g"]
    assert tr.iterations[2] == ["g", "s1"]
    assert tr.iterations[3] == ["g", "s0", "s1"]
    assert tr.stable == tr.iterations[-1] == ["g", "s0", "s1"]
    assert tr.converged_at == 4  # ∅,{g},{g,s1},{g,s0,s1} 后第 4 轮重复
    assert tr.iterations[tr.converged_at] == tr.iterations[tr.converged_at - 1]


def test_mu_unreachable_goal_initial_not_satisfied():
    locs = ["g", "s0"]
    labels = {"goal": ["g"]}
    edges = [("s0", "s0"), ("g", "g")]
    r = solve("μX.(goal | <>X)", labels, edges, locs, initial="s0")
    assert r.initial_satisfied is False
    assert r.satisfied == ["g"]


# --------------------------------------------------------------------------
# 模态算子与布尔
# --------------------------------------------------------------------------


def test_box_vacuously_true_at_deadlock():
    locs = ["a", "b"]
    labels = {"p": ["a"]}
    r = solve("[]p", labels, [], locs, initial="b")
    assert r.satisfied == ["a", "b"]  # 无后继位置空洞为真


def test_diamond_requires_successor():
    locs = ["a", "b"]
    labels = {"p": ["a"]}
    r = solve("<>p", labels, [], locs, initial="b")
    assert r.satisfied == []


def test_negation_and_complement():
    locs = ["a", "b"]
    labels = {"p": ["a"]}
    assert solve("!p", labels, [], locs, initial="b").satisfied == ["b"]
    assert solve("p | !p", labels, [], locs).satisfied == ["a", "b"]
    assert solve("p & !p", labels, [], locs).satisfied == []


def test_ascii_keywords_and_pretty_print():
    ast = parse_formula("nuX.(safe & []X)")
    assert pretty(ast) == "νX.(safe & []X)"
    ast2 = parse_formula("muY.(goal | <>Y)")
    assert pretty(ast2) == "μY.(goal | <>Y)"


# --------------------------------------------------------------------------
# 嵌套固定点：就近绑定与环境不污染
# --------------------------------------------------------------------------


def test_nested_shadowing_does_not_pollute_outer_environment():
    # 外层 νX 要求无限期 safe；内层 μY = 可达 ok。同名重复绑定被禁止，
    # 故嵌套必须使用不同变量名；内层 μY 的迭代环境退出后不得污染外层 νX。
    locs = ["ok", "s0", "t"]
    labels = {"safe": ["s0", "ok"], "ok": ["ok"]}
    edges = [("s0", "s0"), ("t", "ok"), ("ok", "ok")]
    f = "νX.((safe & []X) | μY.(ok | <>Y))"
    r = solve(f, labels, edges, locs, initial="s0")
    # s0：左支成立（safe 自循环）；ok：两支皆成立；t：仅右支（可达 ok）成立。
    assert r.satisfied == ["ok", "s0", "t"]
    kinds = [(t.var, t.kind) for t in r.traces]
    assert ("Y", "mu") in kinds and ("X", "nu") in kinds
    # 外层 νX 的稳定集合必须与内层 μY 的结果各自独立
    nu_trace = next(t for t in r.traces if t.var == "X")
    assert nu_trace.stable == ["ok", "s0", "t"]
    mu_trace = next(t for t in r.traces if t.var == "Y")
    assert mu_trace.stable == ["ok", "t"]


def test_nested_fixpoint_iteration_traces_independent():
    r = solve("μY.μX.(goal | <>X)", {"goal": ["g"]},
              [("s0", "s1"), ("s1", "g"), ("g", "g")],
              ["g", "s0", "s1"], initial="s0")
    assert r.satisfied == ["g", "s0", "s1"]
    # 所有固定点（含每次外层迭代中独立重跑的内层）均从 ∅ 开始
    assert all(t.start == [] for t in r.traces)
    # 外层 μY 迭代两轮，内层 μX 在每轮中独立重新迭代：两条 X 轨迹 + 一条 Y 轨迹
    xs = [t for t in r.traces if t.var == "X"]
    ys = [t for t in r.traces if t.var == "Y"]
    assert len(xs) == 2
    assert len(ys) == 1
    assert [t.occurrence for t in xs] == [1, 2]  # 外层两轮各自触发一次内层迭代
    assert ys[0].occurrence == 1
    assert all(t.stable == ["g", "s0", "s1"] for t in xs)


# --------------------------------------------------------------------------
# 拒绝：未绑定变量、重复绑定名、守卫性、语法残留
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "src",
    [
        "μX.X",            # 无守卫
        "μX.(X | p)",      # 无守卫（析取支中直接出现）
        "νX.(p & X)",      # 无守卫
        "μX.(p | <>Y)",    # Y 未绑定
        "μX.νX.p",         # 重复绑定名 X
        "νX.μX.<>X",       # 重复绑定名 X（同名遮蔽不允许）
        "μX.(!X)",         # 奇数重否定（且无守卫）
        "μX.(p | !<>X)",   # 守卫存在，但作用域内奇数重否定 -> 不单调
        "μX.(p | q)",      # q 未定义为命题
        "νX.[]μY.(p | Y)", # [] 在 Y 绑定点之外，Y 仍无守卫
    ],
)
def test_invalid_formulas_rejected(src):
    with pytest.raises((ValidationError, SyntaxError_)):
        validate(parse_formula(src), {"p"})


@pytest.mark.parametrize(
    "src",
    [
        "μX.(p | <>X)",
        "νX.(p & []X)",
        "μX.(p | ![]!X)",           # 作用域内双重否定
        "!μX.(p | <>X)",            # 绑定点之外的否定与单调性无关
        "!νX.(p & []X)",
        "νX.[]μY.(p & X)",          # X 在其绑定点内的 [] 之下，受守卫
    ],
)
def test_valid_guarded_formulas_accepted(src):
    validate(parse_formula(src), {"p"})


@pytest.mark.parametrize(
    "src",
    [
        "",
        "   ",
        "p q",        # 语法残留
        "μX.",        # 缺体
        "<",          # 残缺模态
        "[]",         # 残缺模态
        "p &",        # 缺右支
        "(p",         # 括号不匹配
        "p)",         # 多余括号 / 残留
        "μ.P",        # 缺变量名
        "p -> q",     # 不允许的算子（-> 为非法字符/残留）
        "p ^ q",
    ],
)
def test_syntax_residue_rejected(src):
    with pytest.raises(SyntaxError_):
        parse_formula(src)
