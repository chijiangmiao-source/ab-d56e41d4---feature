"""μ-演算模型检验奇偶博弈：构建、精确求解与无记忆策略提取。

把复核的**公式闭包 × 位置 × 绑定层级**展开为有限奇偶博弈：

- 顶点为 ``(位置, 闭包子公式, 极性)`` 三元组；极性记录该子公式处于偶数（``+``）
  还是奇数（``-``）重否定之下，否定无需改写公式即可直接入博弈（静态校验已保证
  变量在其绑定点与引用点之间只受偶数重否定，故极性翻转是良定义的对偶）。
- 选择点归属：析取 ``|`` 与 ``<>`` 归验证方（verifier），合取 ``&`` 与 ``[]``
  归挑战方（challenger）；否定极性下对偶互换。变量、绑定、否定与命题顶点没有
  真实选择，统一归验证方（单后继顶点的归属不影响胜负）。
- 优先级：μ 绑定为奇、ν 绑定为偶（否定极性下按对偶翻转）；外层绑定数值更大、
  更显著；其余顶点为 0。无限博弈中无限次经过的最大优先级决定胜方：
  偶数归验证方，奇数归挑战方——这正是 μ/ν 嵌套固定点的博弈语义。
- 命题顶点按当前位置的真值自环（真→偶优先级、假→奇优先级）；无后继位置的
  模态顶点轮到谁走子谁判负，与 ``<>φ`` 为假、``[]φ`` 空洞为真的语义一致。

求解采用 Zielonka 递归：有限奇偶博弈双方均有位置（无记忆）最优策略，故胜方
策略与到达历史无关；并列获胜移动按顶点规范序（位置标识字典序优先）取最小者，
保证同一复核反复审计得到同一策略，且可逐步复算而非只适配一个示例。
"""

from __future__ import annotations

import sys
import threading
from collections import deque
from dataclasses import dataclass

from .mcalc import And, Box, Diamond, Formula, Mu, Not, Nu, Or, Prop, Var, pretty

VERIFIER = "verifier"
CHALLENGER = "challenger"


def opponent(player: str) -> str:
    return CHALLENGER if player == VERIFIER else VERIFIER


@dataclass(frozen=True)
class Vertex:
    """博弈顶点：位置 × 闭包子公式 × 否定极性。"""

    id: int
    location: str
    formula: Formula
    formula_text: str
    polarity: str  # "+" 偶数重否定下 | "-" 奇数重否定下
    kind: str  # proposition|variable|binder|not|and|or|diamond|box
    owner: str  # verifier | challenger
    priority: int
    terminal: bool
    successors: tuple[int, ...]


@dataclass(frozen=True)
class Game:
    vertices: tuple[Vertex, ...]
    initial: int  # 初始顶点 id：(初始位置, 完整公式, "+")
    closure_size: int
    binders: tuple[dict, ...]  # 绑定层级摘要（最外层在前）

    @property
    def edge_count(self) -> int:
        return sum(len(v.successors) for v in self.vertices)


@dataclass(frozen=True)
class Solution:
    region: tuple[str, ...]  # 每个博弈顶点的胜方区域
    strategy: dict[int, int]  # 双方各自胜区内的位置策略：顶点 -> 后继顶点
    initial_winner: str


def build_game(
    ast: Formula,
    labels: dict[str, set[str]],
    edge_model: dict[str, list[str]],
    order: list[str],
    initial: str,
) -> Game:
    """由已校验公式与 Kripke 结构构建有限奇偶博弈。

    ``ast`` 须为 :func:`app.mcalc.validate` 返回的、变量已解析的语法树。
    """
    # ---- 公式闭包与绑定层级 ------------------------------------------------
    closure: list[Formula] = []
    seen: set[Formula] = set()
    binder_of: dict[str, Mu | Nu] = {}
    depth_of: dict[Formula, int] = {}

    def walk(n: Formula, depth: int) -> None:
        if n in seen:
            return
        seen.add(n)
        closure.append(n)
        if isinstance(n, (Mu, Nu)):
            binder_of[n.var] = n  # 静态校验禁止重复绑定名，变量 -> 绑定唯一
            depth_of[n] = depth
            walk(n.inner, depth + 1)
        elif isinstance(n, Not):
            walk(n.inner, depth)
        elif isinstance(n, (And, Or)):
            walk(n.left, depth)
            walk(n.right, depth)
        elif isinstance(n, (Diamond, Box)):
            walk(n.inner, depth)

    walk(ast, 0)

    total_binders = len(binder_of)
    # 外层绑定更显著：priority_base 随嵌套深度递减，且相邻层级至少相差 2，
    # 为极性翻转留出奇偶位。
    base = {b: 2 * (total_binders - 1 - depth_of[b]) for b in binder_of.values()}
    binders = tuple(
        sorted(
            (
                {
                    "binder": f"{'μ' if isinstance(b, Mu) else 'ν'}{b.var}.",
                    "variable": b.var,
                    "fixpoint": "mu" if isinstance(b, Mu) else "nu",
                    "nesting_depth": depth_of[b],
                    "priority_base": base[b],
                }
                for b in binder_of.values()
            ),
            key=lambda item: (item["nesting_depth"], item["binder"]),
        )
    )

    # ---- 顶点编号：规范序（位置标识 -> 公式文本 -> 极性）稳定 --------------
    text = {n: pretty(n) for n in closure}
    keys = [(loc, node, pol) for loc in order for node in closure for pol in ("+", "-")]
    keys.sort(key=lambda k: (k[0], text[k[1]], k[2]))
    vid = {k: i for i, k in enumerate(keys)}

    vertices: list[Vertex] = []
    for loc, node, pol in keys:
        i = vid[(loc, node, pol)]
        neg = pol == "-"
        if isinstance(node, Prop):
            holds = loc in labels.get(node.name, frozenset())
            verifier_wins = (not holds) if neg else holds
            vertices.append(
                Vertex(i, loc, node, text[node], pol, "proposition", VERIFIER,
                       0 if verifier_wins else 1, True, (i,))
            )
        elif isinstance(node, Var):
            binder = binder_of[node.name]
            vertices.append(
                Vertex(i, loc, node, text[node], pol, "variable", VERIFIER,
                       0, False, (vid[(loc, binder, pol)],))
            )
        elif isinstance(node, (Mu, Nu)):
            kind_bit = 1 if isinstance(node, Mu) else 0
            # 否定极性下 μ/ν 对偶翻转：¬μX.ψ ≡ νX.¬ψ(¬X)，反之亦然。
            priority = base[node] + (kind_bit ^ (1 if neg else 0))
            vertices.append(
                Vertex(i, loc, node, text[node], pol, "binder", VERIFIER,
                       priority, False, (vid[(loc, node.inner, pol)],))
            )
        elif isinstance(node, Not):
            vertices.append(
                Vertex(i, loc, node, text[node], pol, "not", VERIFIER, 0, False,
                       (vid[(loc, node.inner, "-" if not neg else "+")],))
            )
        elif isinstance(node, And):
            owner = CHALLENGER if not neg else VERIFIER
            succ = tuple(sorted({vid[(loc, node.left, pol)],
                                 vid[(loc, node.right, pol)]}))
            vertices.append(
                Vertex(i, loc, node, text[node], pol, "and", owner,
                       0, False, succ)
            )
        elif isinstance(node, Or):
            owner = VERIFIER if not neg else CHALLENGER
            succ = tuple(sorted({vid[(loc, node.left, pol)],
                                 vid[(loc, node.right, pol)]}))
            vertices.append(
                Vertex(i, loc, node, text[node], pol, "or", owner,
                       0, False, succ)
            )
        elif isinstance(node, (Diamond, Box)):
            if isinstance(node, Diamond):
                owner = VERIFIER if not neg else CHALLENGER
                kind = "diamond"
            else:
                owner = CHALLENGER if not neg else VERIFIER
                kind = "box"
            succ = tuple(sorted({vid[(t, node.inner, pol)]
                                 for t in edge_model.get(loc, ())}))
            if succ:
                vertices.append(
                    Vertex(i, loc, node, text[node], pol, kind, owner,
                           0, False, succ)
                )
            else:
                # 无合法迁移：轮到谁走子谁判负（<>φ 为假 / []φ 空洞为真）。
                verifier_wins = owner == CHALLENGER
                vertices.append(
                    Vertex(i, loc, node, text[node], pol, kind, owner,
                           0 if verifier_wins else 1, True, (i,))
                )
        else:  # pragma: no cover - 穷尽性
            raise RuntimeError("未知公式节点")

    vertices.sort(key=lambda v: v.id)
    return Game(
        vertices=tuple(vertices),
        initial=vid[(initial, ast, "+")],
        closure_size=len(closure),
        binders=binders,
    )


# ---------------------------------------------------------------------------
# Zielonka 递归求解：精确划分双方胜区并给出位置策略
# ---------------------------------------------------------------------------

_LIMIT_LOCK = threading.Lock()


def solve_game(game: Game) -> Solution:
    """精确求解奇偶博弈，返回每个顶点的胜方区域与双方位置策略。"""
    n = len(game.vertices)
    owner = [v.owner for v in game.vertices]
    prio = [v.priority for v in game.vertices]
    succ = [list(v.successors) for v in game.vertices]
    region: list[str | None] = [None] * n
    strategy: dict[int, int] = {}

    def attractor(player: str, target: set[int], sub: set[int]):
        """player 在子博弈 sub 内对 target 的吸引集及吸引策略（按层推进）。

        返回 (吸引集, 策略)。策略对 player 顶点取规范序最小且层号更小的
        后继，保证朝 target 单调推进且结果稳定。
        """
        preds: list[list[int]] = [[] for _ in range(n)]
        remaining = [0] * n
        for v in sub:
            cnt = 0
            for w in succ[v]:
                if w in sub:
                    preds[w].append(v)
                    cnt += 1
            remaining[v] = cnt
        attr = set(target)
        rank = {v: 0 for v in target}
        dq = deque(sorted(target))
        while dq:
            w = dq.popleft()
            for v in preds[w]:
                if v in attr:
                    continue
                if owner[v] == player:
                    attr.add(v)
                    rank[v] = rank[w] + 1
                    dq.append(v)
                else:
                    remaining[v] -= 1
                    if remaining[v] == 0:  # 对手所有合法移动都被迫进入吸引集
                        attr.add(v)
                        rank[v] = rank[w] + 1
                        dq.append(v)
        strat: dict[int, int] = {}
        for v in attr:
            if owner[v] == player and rank.get(v, 0) > 0:
                cand = [w for w in succ[v] if w in attr and rank[w] < rank[v]]
                strat[v] = min(cand)  # 顶点 id 即规范序：位置标识字典序优先
        return attr, strat

    def rec(sub: set[int]) -> None:
        if not sub:
            return
        p_max = max(prio[v] for v in sub)
        player = VERIFIER if p_max % 2 == 0 else CHALLENGER
        opp = opponent(player)
        top = {v for v in sub if prio[v] == p_max}
        attr, astrat = attractor(player, top, sub)
        rest = sub - attr
        rec(rest)
        if all(region[v] != opp for v in rest):
            # 对手在剩余子博弈中一无所获：player 守住整个 sub。
            for v in sub:
                region[v] = player
            strategy.update(astrat)
            for v in top:
                if owner[v] == player:
                    strategy[v] = min(w for w in succ[v] if w in sub)
        else:
            opp_wins = {v for v in rest if region[v] == opp}
            pulled, bstrat = attractor(opp, opp_wins, sub)
            rec(sub - pulled)
            for v in pulled:
                region[v] = opp
            strategy.update(bstrat)

    # 递归深度以顶点数为界；顶点数随位置数与公式闭包增长，按需放宽解释器限制。
    needed = 4 * n + 1000
    with _LIMIT_LOCK:
        if sys.getrecursionlimit() < needed:
            sys.setrecursionlimit(needed)
    rec(set(range(n)))
    assert all(r is not None for r in region)  # pragma: no cover - 求解完备性
    return Solution(
        region=tuple(r if r is not None else CHALLENGER for r in region),
        strategy=strategy,
        initial_winner=region[game.initial] or CHALLENGER,
    )
