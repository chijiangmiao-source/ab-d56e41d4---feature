"""有限奇偶博弈：由复核公式的闭包 × 位置 × 绑定层级构成，并精确求解。

构造（模态 μ-演算的标准模型检验博弈）：

- 顶点 = 位置 × 公式闭包节点（否定范式下的子公式）。
- 属主：析取与 ``<>`` 归验证方（Verifier，偶优先级方），合取与 ``[]`` 归
  挑战方（Challenger，奇优先级方）；命题、变量与固定点绑定为确定顶点
  （唯一出边，无所谓选择）。
- 终端：命题顶点自环——命题成立优先级 0（验证方胜）、不成立优先级 1
  （挑战方胜）；无后继的 ``<>`` 顶点自环优先级 1（验证方败）、无后继的
  ``[]`` 顶点自环优先级 0（空洞为真，挑战方败）。
- 优先级（绑定层级）：μ 绑定取奇、ν 绑定取偶，外层绑定严格高于内层；
  变量顶点继承其绑定的优先级。一条无限路径上无限次出现的最大优先级
  为偶则验证方胜，为奇则挑战方胜。

求解：Zielonka 递归算法，输出双方各自的胜方区域与位置（无记忆）策略；
策略只依赖当前顶点（位置标识 × 闭包节点），与到达历史无关。
:func:`verify_solution` 对结果做独立自检：区域对对手封闭、策略不出区域，
且策略限制下区域内每个环的最大优先级都属于胜方奇偶。
"""

from __future__ import annotations

import sys
from collections import deque
from dataclasses import dataclass
from typing import Any

from .mcalc import (
    And,
    Box,
    Diamond,
    Formula,
    Mu,
    Not,
    Nu,
    Or,
    Prop,
    Var,
    pretty,
)

VERIFIER = "verifier"      # 验证方：维护“满足”，偶优先级方
CHALLENGER = "challenger"  # 挑战方：维护“不满足”，奇优先级方
DETERMINISTIC = "deterministic"

_PLAYER_CODE = {VERIFIER: 0, CHALLENGER: 1}
_CODE_PLAYER = {0: VERIFIER, 1: CHALLENGER}


# ---------------------------------------------------------------------------
# 否定范式（NNF）：否定仅作用于命题
# ---------------------------------------------------------------------------


def to_nnf(node: Formula) -> Formula:
    """把已校验公式化为否定范式：``!`` 只保留在命题前。

    静态校验保证每个变量在其绑定作用域内只出现在偶数重否定下，因此
    对偶化绑定（μ↔ν、``<>``↔``[]``、``&``↔``|``）后，变量引用相对其
    （对偶）绑定恒为正出现，变量节点本身无需改写。
    """

    def go(n: Formula, neg: bool) -> Formula:
        if isinstance(n, Prop):
            return Not(n) if neg else n
        if isinstance(n, Var):
            return n
        if isinstance(n, Not):
            return go(n.inner, not neg)
        if isinstance(n, And):
            left, right = go(n.left, neg), go(n.right, neg)
            return Or(left, right) if neg else And(left, right)
        if isinstance(n, Or):
            left, right = go(n.left, neg), go(n.right, neg)
            return And(left, right) if neg else Or(left, right)
        if isinstance(n, Diamond):
            inner = go(n.inner, neg)
            return Box(inner) if neg else Diamond(inner)
        if isinstance(n, Box):
            inner = go(n.inner, neg)
            return Diamond(inner) if neg else Box(inner)
        if isinstance(n, (Mu, Nu)):
            # 否定穿过绑定：定点算子对偶（¬μX.ψ ≡ νX.¬ψ，¬νX.ψ ≡ μX.¬ψ）。
            is_mu = isinstance(n, Mu)
            cls = Mu if is_mu != neg else Nu
            return cls(n.var, go(n.inner, neg))
        raise RuntimeError("未知公式节点")  # pragma: no cover

    return go(node, False)


# ---------------------------------------------------------------------------
# 公式闭包：为每个子公式分配稳定节点号（先序），并解析变量 → 绑定
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClosureNode:
    """闭包中的一个子公式节点。"""

    node: int
    kind: str  # proposition|negated_proposition|variable|conjunction|disjunction|diamond|box|mu|nu
    text: str
    child_nodes: tuple[int, ...]
    prop: str | None = None
    variable: str | None = None
    binder_node: int | None = None   # variable → 其绑定节点
    nesting_rank: int | None = None  # mu/nu：0 为最外层
    priority: int = 0


def _closure(root: Formula) -> list[ClosureNode]:
    entries: list[dict[str, Any]] = []
    scope: dict[str, int] = {}

    def visit(n: Formula, depth: int) -> int:
        idx = len(entries)
        e: dict[str, Any] = {"node": idx, "ast": n, "children": ()}
        entries.append(e)
        if isinstance(n, Prop):
            e.update(kind="proposition", prop=n.name)
        elif isinstance(n, Not):
            if not isinstance(n.inner, Prop):  # pragma: no cover - NNF 保证
                raise RuntimeError("否定范式中否定应仅作用于命题")
            e.update(kind="negated_proposition", prop=n.inner.name)
        elif isinstance(n, Var):
            e.update(kind="variable", variable=n.name, binder_node=scope[n.name])
        elif isinstance(n, And):
            e["kind"] = "conjunction"
            e["children"] = (visit(n.left, depth), visit(n.right, depth))
        elif isinstance(n, Or):
            e["kind"] = "disjunction"
            e["children"] = (visit(n.left, depth), visit(n.right, depth))
        elif isinstance(n, Diamond):
            e["kind"] = "diamond"
            e["children"] = (visit(n.inner, depth),)
        elif isinstance(n, Box):
            e["kind"] = "box"
            e["children"] = (visit(n.inner, depth),)
        elif isinstance(n, (Mu, Nu)):
            e["kind"] = "mu" if isinstance(n, Mu) else "nu"
            e["variable"] = n.var
            e["nesting_rank"] = depth
            scope[n.var] = idx
            e["children"] = (visit(n.inner, depth + 1),)
            del scope[n.var]
        else:  # pragma: no cover - 穷尽性
            raise RuntimeError("未知公式节点")
        return idx

    visit(root, 0)
    total = sum(1 for e in entries if e["kind"] in ("mu", "nu"))
    for e in entries:
        if e["kind"] in ("mu", "nu"):
            # 绑定层级：外层绑定优先级严格更高；μ 奇、ν 偶；最低为 2，
            # 使终端命题的 0/1 永远被任何固定点优先级盖过。
            e["priority"] = 2 * (total - e["nesting_rank"]) + (1 if e["kind"] == "mu" else 0)
    for e in entries:
        if e["kind"] == "variable":
            e["priority"] = entries[e["binder_node"]]["priority"]
    return [
        ClosureNode(
            node=e["node"],
            kind=e["kind"],
            text=pretty(e["ast"]),
            child_nodes=e["children"],
            prop=e.get("prop"),
            variable=e.get("variable"),
            binder_node=e.get("binder_node"),
            nesting_rank=e.get("nesting_rank"),
            priority=e.get("priority", 0),
        )
        for e in entries
    ]


# ---------------------------------------------------------------------------
# 博弈结构与构造
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GameVertex:
    vid: str          # "{位置标识}#{闭包节点号}"
    location: str
    node: int
    kind: str
    formula: str
    owner: str        # verifier | challenger | deterministic
    priority: int


@dataclass
class ParityGame:
    vertices: list[GameVertex]
    succ: list[list[int]]
    owner_code: list[int]  # 0=验证方/确定，1=挑战方
    priority: list[int]
    initial: int
    index_of: dict[tuple[str, int], int]
    nnf: Formula
    nodes: list[ClosureNode]


_OWNER_OF_KIND = {
    "disjunction": VERIFIER,
    "diamond": VERIFIER,
    "conjunction": CHALLENGER,
    "box": CHALLENGER,
}


def build_parity_game(
    resolved: Formula,
    labels: dict[str, set[str]],
    edge_model: dict[str, list[str]],
    order: list[str],
    initial: str,
) -> ParityGame:
    """由已校验公式与 Kripke 结构构成有限奇偶博弈。

    ``resolved`` 须为 :func:`app.mcalc.validate` 返回的语法树。
    """
    nnf = to_nnf(resolved)
    nodes = _closure(nnf)

    vertices: list[GameVertex] = []
    index_of: dict[tuple[str, int], int] = {}
    for loc in order:
        for nd in nodes:
            idx = len(vertices)
            index_of[(loc, nd.node)] = idx
            owner = _OWNER_OF_KIND.get(nd.kind, DETERMINISTIC)
            vertices.append(
                GameVertex(
                    vid=f"{loc}#{nd.node}",
                    location=loc,
                    node=nd.node,
                    kind=nd.kind,
                    formula=nd.text,
                    owner=owner,
                    priority=0,  # 顶点优先级在出边构造时一并确定
                )
            )

    succ: list[list[int]] = []
    owner_code: list[int] = []
    priority: list[int] = []
    for loc in order:
        loc_targets = edge_model.get(loc, [])
        for nd in nodes:
            idx = index_of[(loc, nd.node)]
            if nd.kind == "proposition":
                edges = [idx]  # 终端自环
                vp = 0 if loc in labels.get(nd.prop or "", ()) else 1
            elif nd.kind == "negated_proposition":
                edges = [idx]  # 终端自环
                vp = 1 if loc in labels.get(nd.prop or "", ()) else 0
            elif nd.kind == "variable":
                edges = [index_of[(loc, nd.binder_node or 0)]]
                vp = nd.priority
            elif nd.kind in ("mu", "nu"):
                edges = [index_of[(loc, nd.child_nodes[0])]]
                vp = nd.priority
            elif nd.kind in ("conjunction", "disjunction"):
                edges = [index_of[(loc, c)] for c in nd.child_nodes]
                vp = 0
            elif nd.kind in ("diamond", "box"):
                inner = nd.child_nodes[0]
                targets = sorted(set(loc_targets), key=order.index)
                edges = [index_of[(t, inner)] for t in targets]
                if not edges:
                    # 死端自环：<> 验证方败（奇）、[] 空洞为真（偶）。
                    edges = [idx]
                    vp = 1 if nd.kind == "diamond" else 0
                else:
                    vp = 0
            else:  # pragma: no cover - 穷尽性
                raise RuntimeError("未知闭包节点")
            succ.append(edges)
            owner_code.append(_PLAYER_CODE.get(vertices[idx].owner, 0))
            priority.append(vp)

    vertices = [
        GameVertex(v.vid, v.location, v.node, v.kind, v.formula, v.owner, priority[i])
        for i, v in enumerate(vertices)
    ]
    return ParityGame(
        vertices=vertices,
        succ=succ,
        owner_code=owner_code,
        priority=priority,
        initial=index_of[(initial, 0)],
        index_of=index_of,
        nnf=nnf,
        nodes=nodes,
    )


# ---------------------------------------------------------------------------
# Zielonka 精确求解：胜方区域 + 双方位置策略
# ---------------------------------------------------------------------------


@dataclass
class Solution:
    regions: dict[int, set[int]]        # 玩家代号 -> 顶点下标集合
    strategies: dict[int, dict[int, int]]  # 玩家代号 -> {顶点: 后继}


def solve_parity_game(game: ParityGame) -> Solution:
    """Zielonka 递归算法；输出对双方均为位置（无记忆）的胜策略。"""
    n = len(game.vertices)
    # 递归深度上界为顶点数；对过大博弈预先放宽解释器限制。
    needed = 4 * n + 1000
    if sys.getrecursionlimit() < needed:
        sys.setrecursionlimit(needed)

    pred: list[list[int]] = [[] for _ in range(n)]
    for v in range(n):
        for w in game.succ[v]:
            pred[w].append(v)

    def attractor(player: int, target: set[int], allowed: set[int]):
        """player 可强制进入 target 的顶点集；同时返回吸引子策略。"""
        attr = set(target)
        remaining = {
            v: sum(1 for w in game.succ[v] if w in allowed)
            for v in allowed
            if game.owner_code[v] != player
        }
        strat: dict[int, int] = {}
        queue = deque(sorted(target))
        # 子博弈中无出边的对手顶点立即归入（不能行动者败）。
        for v in sorted(allowed):
            if v not in attr and game.owner_code[v] != player and remaining[v] == 0:
                attr.add(v)
                queue.append(v)
        while queue:
            w = queue.popleft()
            for v in pred[w]:
                if v not in allowed or v in attr:
                    continue
                if game.owner_code[v] == player:
                    attr.add(v)
                    strat[v] = w
                    queue.append(v)
                else:
                    remaining[v] -= 1
                    if remaining[v] == 0:
                        attr.add(v)
                        queue.append(v)
        return attr, strat

    def zielonka(allowed: set[int]):
        if not allowed:
            return {0: set(), 1: set()}, {0: {}, 1: {}}
        top = max(game.priority[v] for v in allowed)
        alpha = top % 2
        upper = {v for v in allowed if game.priority[v] == top}
        attr, strat_a = attractor(alpha, upper, allowed)
        rest = allowed - attr
        wins, strats = zielonka(rest)
        if not wins[1 - alpha]:
            # alpha 在整个子博弈制胜：吸引子回到最高优先级，循环占优。
            wins[alpha] = set(allowed)
            strats[alpha].update(strat_a)
            for v in sorted(upper):
                if game.owner_code[v] == alpha and v not in strats[alpha]:
                    for w in game.succ[v]:
                        if w in allowed:
                            strats[alpha][v] = w
                            break
            return wins, strats
        # 对手在 rest 中有制胜核：把它连同其吸引子一并划给对手后递归。
        attr_b, strat_b = attractor(1 - alpha, wins[1 - alpha], allowed)
        rest2 = allowed - attr_b
        wins2, strats2 = zielonka(rest2)
        wins2[1 - alpha] |= attr_b
        strats2[1 - alpha].update(strat_b)
        for v, t in strats[1 - alpha].items():
            strats2[1 - alpha].setdefault(v, t)
        return wins2, strats2

    regions, strategies = zielonka(set(range(n)))
    return Solution(regions, strategies)


# ---------------------------------------------------------------------------
# 独立自检：区域封闭 + 策略不出区域 + 环奇偶属于胜方
# ---------------------------------------------------------------------------


def _sccs(adj: dict[int, list[int]]) -> list[list[int]]:
    """迭代版 Tarjan 强连通分量。"""
    index: dict[int, int] = {}
    lowlink: dict[int, int] = {}
    on_stack: set[int] = set()
    stack: list[int] = []
    result: list[list[int]] = []
    counter = 0

    for root in adj:
        if root in index:
            continue
        index[root] = lowlink[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        work: list[tuple[int, Any]] = [(root, iter(adj.get(root, ())))]
        while work:
            v, it = work[-1]
            descended = False
            for w in it:
                if w not in adj:
                    continue
                if w not in index:
                    index[w] = lowlink[w] = counter
                    counter += 1
                    stack.append(w)
                    on_stack.add(w)
                    work.append((w, iter(adj[w])))
                    descended = True
                    break
                if w in on_stack:
                    lowlink[v] = min(lowlink[v], index[w])
            if descended:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[v])
            if lowlink[v] == index[v]:
                scc = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    scc.append(w)
                    if w == v:
                        break
                result.append(scc)
    return result


def verify_solution(game: ParityGame, solution: Solution) -> dict[int, bool]:
    """逐方校验：胜方区域对对手封闭、策略不出区域、策略限制下每个环的
    最大优先级都属于该方奇偶（即策略确实制胜，而非只适配单条路径）。"""
    return {p: _verify_player(game, solution, p) for p in (0, 1)}


def _verify_player(game: ParityGame, solution: Solution, player: int) -> bool:
    region = solution.regions[player]
    strat = solution.strategies[player]
    for v, w in strat.items():
        if v not in region or game.owner_code[v] != player:
            return False
        if w not in game.succ[v] or w not in region:
            return False
    for v in region:
        outs = game.succ[v]
        if game.owner_code[v] == player:
            if v in strat:
                continue
            # 无策略裁决的己方顶点必须是唯一出边且留在区域内。
            if len(outs) != 1 or outs[0] not in region:
                return False
        elif any(w not in region for w in outs):
            # 对手选择点：每种合法选择都必须留在胜方区域内。
            return False
    adj: dict[int, list[int]] = {}
    for v in region:
        if v in strat:
            adj[v] = [strat[v]]
        else:
            adj[v] = [w for w in game.succ[v] if w in region]
    for scc in _sccs(adj):
        cyclic = len(scc) > 1 or (bool(scc) and scc[0] in adj.get(scc[0], ()))
        if cyclic and max(game.priority[v] for v in scc) % 2 != player:
            return False
    return True
