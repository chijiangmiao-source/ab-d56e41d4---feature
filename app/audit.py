"""策略审计：把已持久化的复核展开为有限奇偶博弈，精确求解并固化胜方策略。

审计确认初始位置的满足（或不满足）可由某一方在**任意无限迁移**中持续保证：
验证方（verifier）力证公式成立，挑战方（challenger）力证其不成立；胜方在
整个无限博弈上获胜，而非把固定点集合当成单条路径回放。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .game import CHALLENGER, VERIFIER, Vertex, build_game, opponent, solve_game
from .mcalc import Mu, parse_formula, pretty, validate

_PLAYER_ZH = {VERIFIER: "验证方", CHALLENGER: "挑战方"}


def _rebuild_model(request: dict[str, Any]):
    """从已持久化复核的请求部分重建 Kripke 结构（与创建时同一模型）。"""
    order = list(request["locations"])
    labels: dict[str, set[str]] = {}
    for item in request["propositions"]:
        labels.setdefault(item["proposition"], set()).add(item["location"])
    edge_model: dict[str, list[str]] = {loc: [] for loc in order}
    for edge in request["transitions"]:
        edge_model[edge["source"]].append(edge["target"])
    return order, labels, edge_model


def _choice_kind(v: Vertex) -> str:
    return {
        "or": "disjunction",
        "and": "conjunction",
        "diamond": "diamond",
        "box": "box",
        "proposition": "terminal",
    }.get(v.kind, "forced")


def _move_note(winner: str, v: Vertex, target: Vertex) -> str:
    who = _PLAYER_ZH[winner]
    if v.kind == "or":
        return f"{who}在析取选择点取子公式 {target.formula_text}（顶点 v{target.id}）"
    if v.kind == "and":
        return f"{who}在合取选择点取子公式 {target.formula_text}（顶点 v{target.id}）"
    if v.kind in ("diamond", "box"):
        return f"{who}在模态选择点迁移至位置 {target.location}（顶点 v{target.id}）"
    if v.kind == "proposition":
        return f"命题终端自环：位置 {v.location} 处 {v.formula_text} 的真值有利于{who}"
    return f"强制移动（唯一合法选择）：进入顶点 v{target.id}"


def build_audit(record: dict[str, Any], review_id: int) -> dict[str, Any]:
    """对已保存复核构建策略审计记录（可整体持久化）。

    ``record`` 为 :func:`app.service.build_review` 产出并已持久化的复核记录。
    """
    request = record["request"]
    initial = request["initial"]
    order, labels, edge_model = _rebuild_model(request)
    ast = validate(parse_formula(request["formula"]), set(labels))

    game = build_game(ast, labels, edge_model, order, initial)
    sol = solve_game(game)

    winner = sol.initial_winner
    loser = opponent(winner)

    # ---- 各博弈顶点及其胜方区域 -------------------------------------------
    vertices_out: list[dict[str, Any]] = []
    for v in game.vertices:
        entry: dict[str, Any] = {
            "id": v.id,
            "location": v.location,
            "formula": v.formula_text,
            "polarity": v.polarity,
            "kind": v.kind,
            "owner": v.owner,
            "priority": v.priority,
            "terminal": v.terminal,
            "successors": list(v.successors),
            "region": sol.region[v.id],
        }
        if v.kind == "binder":
            node = v.formula
            syntactic = "mu" if isinstance(node, Mu) else "nu"
            flipped = v.polarity == "-"
            entry["fixpoint"] = syntactic
            entry["variable"] = node.var
            entry["effective_fixpoint"] = (
                ("nu" if syntactic == "mu" else "mu") if flipped else syntactic
            )
        if v.terminal:
            entry["terminal_winner"] = VERIFIER if v.priority % 2 == 0 else CHALLENGER
        vertices_out.append(entry)

    # ---- 胜方无记忆策略：析取、合取与模态选择点（含强制移动）----------------
    moves: list[dict[str, Any]] = []
    for v in game.vertices:
        if v.owner != winner or sol.region[v.id] != winner:
            continue
        chosen = sol.strategy.get(v.id)
        if chosen is None:  # pragma: no cover - 胜区内胜方顶点必有策略
            continue
        target = game.vertices[chosen]
        moves.append(
            {
                "vertex": v.id,
                "location": v.location,
                "formula": v.formula_text,
                "polarity": v.polarity,
                "kind": v.kind,
                "choice_kind": _choice_kind(v),
                "choose": chosen,
                "choose_location": target.location,
                "choose_formula": target.formula_text,
                "note": _move_note(winner, v, target),
            }
        )

    # ---- 对手每种合法选择进入的胜方区域（供逐步复算）-----------------------
    opponent_options: list[dict[str, Any]] = []
    for v in game.vertices:
        if v.owner != loser:
            continue
        options = [
            {
                "vertex": t,
                "location": game.vertices[t].location,
                "formula": game.vertices[t].formula_text,
                "polarity": game.vertices[t].polarity,
                "region": sol.region[t],
            }
            for t in v.successors
        ]
        opponent_options.append(
            {
                "vertex": v.id,
                "location": v.location,
                "formula": v.formula_text,
                "polarity": v.polarity,
                "kind": v.kind,
                "in_winner_region": sol.region[v.id] == winner,
                "options": options,
            }
        )

    initial_satisfied = winner == VERIFIER
    consistent = initial_satisfied == record["conclusion"]["initial_satisfied"]

    return {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "review_id": review_id,
        "source_review": {
            "review_id": review_id,
            "initial_location": initial,
            "initial_satisfied": record["conclusion"]["initial_satisfied"],
            "release_permitted": record["conclusion"]["release_permitted"],
            "satisfied_set": record["conclusion"]["satisfied_set"],
        },
        "formula_summary": {
            "source": request["formula"],
            "normalized": pretty(ast),
            "closure_size": game.closure_size,
            "binders": [dict(b) for b in game.binders],
            "priority_rule": (
                "μ 绑定为奇优先级、ν 绑定为偶优先级；外层绑定优先级数值更大、"
                "更显著；否定极性下按对偶翻转"
            ),
        },
        "game": {
            "vertex_count": len(game.vertices),
            "edge_count": game.edge_count,
            "initial_vertex": game.initial,
            "vertices": vertices_out,
            "semantics": {
                "players": "验证方力证初始位置满足公式，挑战方力证其不满足",
                "choice_points": "析取 | 与 <> 归验证方裁决，合取 & 与 [] 归挑战方裁决；否定极性下对偶互换",
                "priorities": "无限次经过的最大优先级决定胜方：偶数归验证方、奇数归挑战方",
                "strategy": "位置策略仅依赖当前博弈顶点，与到达历史无关；并列获胜移动按位置标识字典序稳定裁决",
                "note": "胜方在整个无限博弈上持续保证结论，而非单条有限路径回放",
            },
        },
        "result": {
            "initial_vertex": game.initial,
            "initial_winner": winner,
            "initial_satisfied": initial_satisfied,
            "consistent_with_review": consistent,
            "region_sizes": {
                VERIFIER: sum(1 for r in sol.region if r == VERIFIER),
                CHALLENGER: sum(1 for r in sol.region if r == CHALLENGER),
            },
        },
        "strategy": {
            "player": winner,
            "memoryless": True,
            "history_independent": "策略仅依赖当前博弈顶点，与到达历史无关",
            "tie_break": "并列获胜移动按顶点规范序（位置标识字典序优先）取最小者",
            "moves": moves,
        },
        "opponent_options": opponent_options,
        "recompute_hint": (
            "自初始顶点出发逐步复算：胜方顶点按 strategy.moves 走子；对手顶点查 "
            "opponent_options，其每种合法选择进入的胜方区域均逐条列出"
        ),
    }
