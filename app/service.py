"""复核业务层：模型校验、固定点求值与规范证据组装。"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from . import mcalc
from .mcalc import SyntaxError_, ValidationError, evaluate, parse_formula, pretty, validate

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MIN_LOCATIONS = 2
MAX_LOCATIONS = 24


class RequestError(ValueError):
    """请求不合规；携带机器可读错误码与明细。"""

    def __init__(self, code: str, message: str, details: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or []


def _err(code: str, message: str, **detail: Any) -> RequestError:
    details = [detail] if detail else []
    return RequestError(code, message, details)


def build_review(payload: dict[str, Any]) -> dict[str, Any]:
    """校验请求并计算结论；成功时返回可持久化的完整记录。

    任何不合规情形均抛出 :class:`RequestError`，调用方不得写库、不得发编号。
    """
    locations = payload.get("locations")
    initial = payload.get("initial")
    propositions = payload.get("propositions", [])
    transitions = payload.get("transitions", [])
    formula_src = payload.get("formula")

    # ---- 位置 --------------------------------------------------------------
    if not isinstance(locations, list) or not locations:
        raise _err("INVALID_LOCATIONS", "locations 必须为非空列表")
    if not (MIN_LOCATIONS <= len(locations) <= MAX_LOCATIONS):
        raise _err(
            "INVALID_LOCATIONS",
            f"唯一位置数量必须在 {MIN_LOCATIONS} 至 {MAX_LOCATIONS} 之间",
            count=len(locations) if isinstance(locations, list) else None,
        )
    seen: set[str] = set()
    dupes: list[str] = []
    for loc in locations:
        if not isinstance(loc, str) or not loc.strip():
            raise _err("INVALID_LOCATIONS", "位置标识必须为非空字符串", value=loc)
        if loc in seen:
            dupes.append(loc)
        seen.add(loc)
    if dupes:
        raise RequestError(
            "DUPLICATE_LOCATION",
            "位置标识必须唯一",
            [{"location": d} for d in dupes],
        )
    location_set = set(locations)
    order = sorted(locations)

    # ---- 初始位置 ----------------------------------------------------------
    if not isinstance(initial, str) or not initial:
        raise _err("INVALID_INITIAL", "initial 必须为非空位置标识")
    if initial not in location_set:
        raise _err("UNKNOWN_INITIAL", "初始位置不在已提交位置集合中", location=initial)

    # ---- 位置命题 ----------------------------------------------------------
    if not isinstance(propositions, list):
        raise _err("INVALID_PROPOSITIONS", "propositions 必须为列表")
    labels: dict[str, set[str]] = {}
    prop_details: list[dict[str, Any]] = []
    prop_seen: set[tuple[str, str]] = set()
    for i, item in enumerate(propositions):
        if not isinstance(item, dict):
            raise _err("INVALID_PROPOSITIONS", "propositions 条目必须为对象", index=i)
        loc = item.get("location")
        name = item.get("proposition")
        if loc not in location_set:
            raise _err(
                "DANGLING_PROPOSITION_LOCATION",
                "命题标注的位置不存在（悬空引用）",
                index=i,
                location=loc,
            )
        if not isinstance(name, str) or not _IDENT_RE.match(name):
            raise _err(
                "INVALID_PROPOSITION_NAME",
                "命题名必须为字母/下划线开头的字母数字串",
                index=i,
                value=name,
            )
        if (loc, name) in prop_seen:
            raise _err(
                "DUPLICATE_PROPOSITION",
                "同一位置的同一命题重复标注",
                location=loc,
                proposition=name,
            )
        prop_seen.add((loc, name))
        labels.setdefault(name, set()).add(loc)
        prop_details.append({"location": loc, "proposition": name})

    # ---- 有向迁移 ----------------------------------------------------------
    if not isinstance(transitions, list):
        raise _err("INVALID_TRANSITIONS", "transitions 必须为列表")
    edge_ids: set[str] = set()
    edge_dupes: list[str] = []
    edge_model: dict[str, list[str]] = {loc: [] for loc in locations}
    edge_details: list[dict[str, Any]] = []
    for i, item in enumerate(transitions):
        if not isinstance(item, dict):
            raise _err("INVALID_TRANSITION", "transitions 条目必须为对象", index=i)
        tid = item.get("id")
        src = item.get("source")
        dst = item.get("target")
        if not isinstance(tid, str) or not tid.strip():
            raise _err("INVALID_TRANSITION_ID", "迁移标识必须为非空字符串", index=i)
        if tid in edge_ids:
            edge_dupes.append(tid)
        edge_ids.add(tid)
        if src not in location_set:
            raise _err(
                "DANGLING_TRANSITION",
                f"迁移 {tid} 的源位置不存在（悬空迁移必须被拒绝）",
                id=tid,
                endpoint="source",
                location=src,
            )
        if dst not in location_set:
            raise _err(
                "DANGLING_TRANSITION",
                f"迁移 {tid} 的目标位置不存在（悬空迁移必须被拒绝）",
                id=tid,
                endpoint="target",
                location=dst,
            )
        edge_model[src].append(dst)
        edge_details.append({"id": tid, "source": src, "target": dst})
    if edge_dupes:
        raise RequestError(
            "DUPLICATE_TRANSITION_ID",
            "迁移标识必须唯一",
            [{"id": t} for t in dict.fromkeys(edge_dupes)],
        )

    # ---- 公式 --------------------------------------------------------------
    if not isinstance(formula_src, str) or not formula_src.strip():
        raise _err("INVALID_FORMULA", "formula 必须为非空字符串")
    try:
        ast = parse_formula(formula_src)
        resolved = validate(ast, set(labels.keys()))
    except SyntaxError_ as exc:
        raise _err("FORMULA_SYNTAX", f"公式语法错误：{exc}") from exc
    except ValidationError as exc:
        raise _err("FORMULA_INVALID", f"公式校验失败：{exc}") from exc

    # ---- 求值（μ 从 ∅、ν 从全集迭代到稳定）---------------------------------
    result = evaluate(resolved, labels, edge_model, order, initial)

    # ---- 证据 --------------------------------------------------------------
    traces: list[dict[str, Any]] = []
    for t in result.traces:
        stages: list[dict[str, Any]] = []
        prev: list[str] | None = None
        for idx, stage in enumerate(t.iterations):
            added = sorted(set(stage) - set(prev or []))
            removed = sorted(set(prev or []) - set(stage))
            stages.append(
                {
                    "iteration": idx,
                    "locations": stage,
                    "added": added,
                    "removed": removed,
                }
            )
            prev = stage
        if t.kind == "mu":
            rule = "最小固定点：自空集 ∅ 出发单调上升迭代"
        else:
            rule = "最大固定点：自位置全集出发单调下降迭代"
        stability = (
            f"第 {t.converged_at} 轮集合与第 {t.converged_at - 1} 轮相等，"
            "F(X) = X，达到固定点（Knaster–Tarski）"
        )
        traces.append(
            {
                "binder": t.binder,
                "variable": t.var,
                "fixpoint": t.kind,
                "occurrence": t.occurrence,
                "iteration_rule": rule,
                "start": t.start,
                "stages": stages,
                "stable": t.stable,
                "converged_at": t.converged_at,
                "stability_evidence": stability,
            }
        )

    satisfaction_set = result.satisfied  # 已按位置标识排序
    record = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "request": {
            "locations": order,
            "initial": initial,
            "propositions": sorted(prop_details, key=lambda p: (p["location"], p["proposition"])),
            "transitions": edge_details,
            "formula": formula_src,
        },
        "formula": {
            "source": formula_src,
            "normalized": pretty(resolved),
            "allowed_operators": ["命题", "!", "&", "|", "<>", "[]", "μX.", "νX."],
            "scoping": "变量引用最近绑定者；嵌套绑定不污染外层环境",
        },
        "model": {
            "location_order": order,
            "valuation": {name: sorted(locs) for name, locs in sorted(labels.items())},
            "successors": {loc: edge_model[loc] for loc in order},
        },
        "conclusion": {
            "satisfied_set": satisfaction_set,
            "satisfied_set_sorted_by": "location identifier",
            "initial_location": initial,
            "initial_satisfied": result.initial_satisfied,
            "release_permitted": result.initial_satisfied,
        },
        "evidence": {
            "fixpoint_iterations": traces,
            "semantics": {
                "diamond": "<>φ 在位置 s 成立当且仅当存在 s 的迁移到达满足 φ 的位置",
                "box": "[]φ 在位置 s 成立当且仅当 s 的所有迁移均到达满足 φ 的位置",
                "mu": "μX.F(X) = F 自 ∅ 迭代的稳定点",
                "nu": "νX.F(X) = F 自位置全集迭代的稳定点",
                "note": "结论基于无限行为的固定点语义，非有限路径回放",
            },
        },
    }
    return record
