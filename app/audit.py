"""策略审计：把已保存复核的公式闭包 × 位置 × 绑定层级构成有限奇偶博弈，
精确求解并为胜方生成与到达历史无关、按位置标识稳定裁决的位置策略。

审计载荷与来源复核编号、公式摘要、各博弈顶点的胜方区域及策略共同持久化；
读取时列出对手每种合法选择进入的胜方区域，使策略可被逐步复算。
"""

from __future__ import annotations

from datetime import datetime, timezone
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
    parse_formula,
    pretty,
    validate,
)
from .pgame import (
    CHALLENGER,
    DETERMINISTIC,
    VERIFIER,
    build_parity_game,
    solve_parity_game,
    verify_solution,
)

_PLAYER_CODE = {VERIFIER: 0, CHALLENGER: 1}
_CODE_PLAYER = {0: VERIFIER, 1: CHALLENGER}
_CHOICE_KINDS = {
    VERIFIER: {"disjunction", "diamond"},
    CHALLENGER: {"conjunction", "box"},
}


def _original_binder_kinds(node: Formula) -> dict[str, str]:
    """原始（未对偶化）公式中各绑定变量的定点种类，用于标注 NNF 对偶化。"""
    kinds: dict[str, str] = {}

    def walk(n: Formula) -> None:
        if isinstance(n, (Mu, Nu)):
            kinds[n.var] = "mu" if isinstance(n, Mu) else "nu"
            walk(n.inner)
        elif isinstance(n, Not):
            walk(n.inner)
        elif isinstance(n, (And, Or)):
            walk(n.left)
            walk(n.right)
        elif isinstance(n, (Diamond, Box)):
            walk(n.inner)

    walk(node)
    return kinds


def build_audit(review_id: int, record: dict[str, Any]) -> dict[str, Any]:
    """在已保存复核记录上构建策略审计载荷（调用方负责持久化）。

    任何内部不一致（求解自检失败、与复核满足集不符）都抛出异常，
    调用方不得写库。
    """
    req = record["request"]
    order = list(req["locations"])
    initial = req["initial"]
    labels: dict[str, set[str]] = {}
    for item in req["propositions"]:
        labels.setdefault(item["proposition"], set()).add(item["location"])
    edge_model: dict[str, list[str]] = {loc: [] for loc in order}
    via: dict[tuple[str, str], list[str]] = {}
    for t in req["transitions"]:
        edge_model[t["source"]].append(t["target"])
        via.setdefault((t["source"], t["target"]), []).append(t["id"])

    resolved = validate(parse_formula(req["formula"]), set(labels))
    game = build_parity_game(resolved, labels, edge_model, order, initial)
    solution = solve_parity_game(game)
    checks = verify_solution(game, solution)
    if not (checks[0] and checks[1]):
        raise RuntimeError("奇偶博弈求解结果未通过策略自检")

    region_of: list[str] = [""] * len(game.vertices)
    for code, name in _CODE_PLAYER.items():
        for v in solution.regions[code]:
            region_of[v] = name

    # 与来源复核交叉核对：每个位置的根顶点胜方 ⟺ 该位置在满足集中。
    satisfied = set(record["conclusion"]["satisfied_set"])
    consistent = all(
        (region_of[game.index_of[(loc, 0)]] == VERIFIER) == (loc in satisfied)
        for loc in order
    )
    if not consistent:
        raise RuntimeError("博弈胜方区域与复核满足集不一致")

    def move_entry(v: int, w: int) -> dict[str, Any]:
        gv, gw = game.vertices[v], game.vertices[w]
        modal = gv.kind in ("diamond", "box")
        return {
            "to": gw.vid,
            "to_location": gw.location,
            "to_formula": gw.formula,
            "via_transitions": sorted(via.get((gv.location, gw.location), ()))
            if modal
            else [],
            "target_region": region_of[w],
        }

    vertices_out: list[dict[str, Any]] = []
    for v, gv in enumerate(game.vertices):
        vertices_out.append(
            {
                "id": gv.vid,
                "location": gv.location,
                "node": gv.node,
                "formula": gv.formula,
                "kind": gv.kind,
                "owner": gv.owner,
                "choice_point": gv.owner != DETERMINISTIC,
                "priority": gv.priority,
                "region": region_of[v],
                "terminal_self_loop": game.succ[v] == [v],
                "moves": [move_entry(v, w) for w in game.succ[v]],
            }
        )

    winner = region_of[game.initial]
    loser = _CODE_PLAYER[1 - _PLAYER_CODE[winner]]
    winner_region = solution.regions[_PLAYER_CODE[winner]]
    winner_strat = solution.strategies[_PLAYER_CODE[winner]]

    # 胜方位置策略：只在其选择点（验证方：析取/<>；挑战方：合取/[]）裁决，
    # 键为博弈顶点（位置标识 × 闭包节点），与到达历史无关。
    strategy_moves: dict[str, Any] = {}
    for v in sorted(winner_region):
        gv = game.vertices[v]
        if gv.kind not in _CHOICE_KINDS[winner]:
            continue
        w = winner_strat.get(v)
        if w is None:
            if len(game.succ[v]) == 1:
                w = game.succ[v][0]
            else:  # pragma: no cover - 求解器保证选择点全覆盖
                raise RuntimeError("胜方选择点缺少策略裁决")
        strategy_moves[gv.vid] = move_entry(v, w)

    # 对手每种合法选择进入的胜方区域（限胜方区域内的对手选择点）。
    opponent_vertices: dict[str, Any] = {}
    all_stay = True
    for v in sorted(winner_region):
        gv = game.vertices[v]
        if gv.kind not in _CHOICE_KINDS[loser]:
            continue
        choices = [move_entry(v, w) for w in game.succ[v]]
        if any(c["target_region"] != winner for c in choices):
            all_stay = False  # pragma: no cover - 自检已保证区域封闭
        opponent_vertices[gv.vid] = {
            "location": gv.location,
            "formula": gv.formula,
            "choices": choices,
        }

    orig_kinds = _original_binder_kinds(resolved)
    binders = [
        {
            "variable": nd.variable,
            "binder": f"{'μ' if nd.kind == 'mu' else 'ν'}{nd.variable}.",
            "fixpoint": nd.kind,
            "nesting_rank": nd.nesting_rank,
            "priority": nd.priority,
            "dualized_by_negation": orig_kinds.get(nd.variable or "", nd.kind) != nd.kind,
        }
        for nd in game.nodes
        if nd.kind in ("mu", "nu")
    ]

    regions_out = {
        name: [game.vertices[v].vid for v in sorted(solution.regions[code])]
        for code, name in _CODE_PLAYER.items()
    }

    guarantees = "satisfaction" if winner == VERIFIER else "unsatisfaction"
    initial_vid = game.vertices[game.initial].vid
    if winner == VERIFIER:
        statement = (
            f"验证方自初始顶点 {initial_vid} 拥有与到达历史无关的位置胜策略："
            f"在任意无限迁移中都能持续保证初始位置 {initial} 满足该公式"
        )
    else:
        statement = (
            f"挑战方自初始顶点 {initial_vid} 拥有与到达历史无关的位置胜策略："
            f"在任意无限迁移中都能持续保证初始位置 {initial} 不满足该公式，不得放行"
        )

    return {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "review_id": review_id,
            "initial_location": initial,
            "satisfied_set": record["conclusion"]["satisfied_set"],
            "release_permitted": record["conclusion"]["release_permitted"],
        },
        "formula": {
            "source": req["formula"],
            "normalized": record["formula"]["normalized"],
            "nnf": pretty(game.nnf),
            "closure_size": len(game.nodes),
            "binders": binders,
            "priority_rule": (
                "μ 绑定取奇优先级、ν 绑定取偶优先级；外层绑定优先级严格高于内层"
                "（绑定层级决定显著性）；无限次出现的最大优先级为偶则验证方胜，"
                "为奇则挑战方胜"
            ),
        },
        "game": {
            "construction": (
                "顶点 = 位置 × 公式闭包节点；析取与 <> 归验证方、合取与 [] 归挑战方、"
                "命题/变量/固定点绑定为确定顶点；命题按其成立与否终端自环，"
                "无后继的 <> 验证方败、无后继的 [] 空洞为真"
            ),
            "players": {
                "verifier": "验证方（偶优先级方）：维护“满足”",
                "challenger": "挑战方（奇优先级方）：维护“不满足”",
            },
            "vertex_count": len(game.vertices),
            "edge_count": sum(len(s) for s in game.succ),
            "max_priority": max(game.priority),
            "initial_vertex": initial_vid,
            "vertices": vertices_out,
        },
        "winning_regions": regions_out,
        "strategy": {
            "player": winner,
            "kind": "positional",
            "memoryless": True,
            "decided_by": "博弈顶点（位置标识 × 公式闭包节点），与到达历史无关",
            "choice_points": "验证方在析取与 <> 顶点裁决；挑战方在合取与 [] 顶点裁决",
            "move_count": len(strategy_moves),
            "moves": strategy_moves,
        },
        "opponent_choices": {
            "player": loser,
            "description": (
                "胜方区域内每个对手选择点的全部合法选择及其进入的胜方区域；"
                "结合各顶点 moves 可逐步复算策略，而非只适配单一示例"
            ),
            "all_choices_stay_in_winner_region": all_stay,
            "vertices": opponent_vertices,
        },
        "conclusion": {
            "initial_vertex": initial_vid,
            "winner": winner,
            "guarantees": guarantees,
            "statement": statement,
            "consistent_with_review": consistent,
            "solver_self_check": {
                "verifier_region_strategy_valid": checks[0],
                "challenger_region_strategy_valid": checks[1],
            },
        },
    }
