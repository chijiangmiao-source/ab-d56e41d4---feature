"""一次性 HTTP 冒烟验收：对运行中的复核服务发起验收场景并断言。

场景与验收点：
1. νX.(safe & []X)：安全自循环满足；ν 自全集单调下降至稳定，证据相邻两轮相等；
   策略审计须给出验证方（偶优先级方）位置策略，挑战方每种合法选择都进入验证方胜区。
2. μX.(goal | <>X)：可达 goal 的满足集自 ∅ 逐轮扩展至初始位置。
3. 危险迁移：同一 ν 公式在初始位置不满足、不得放行；策略审计须给出挑战方
   （奇优先级方）位置策略，且策略经危险迁移走向危险位置。
4. 悬空迁移：422 拒绝、persisted=false、不产生可读编号。

成功时退出码 0；任一断言失败退出码 1。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("SMOKE_BASE_URL", "http://api:8080").rstrip("/")
failures: list[str] = []


def check(cond: bool, message: str) -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {message}")
    if not cond:
        failures.append(message)


def request(method: str, path: str, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def is_subset(a: list[str], b: list[str]) -> bool:
    return set(a) <= set(b)


def main() -> int:
    print(f"== HTTP 冒烟验收，目标 {BASE} ==")

    status, health = request("GET", "/healthz")
    check(status == 200 and health["status"] == "ok", "健康检查 /healthz 返回 ok")
    reviews_before = health.get("reviews", 0)
    audits_before = health.get("audits", 0)

    # ---- 场景 1：ν 收敛，安全自循环 ----------------------------------------
    print("场景 1 νX.(safe & []X) 安全自循环")
    nu_payload = {
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
    status, body = request("POST", "/api/v1/reviews", nu_payload)
    check(status == 201, "创建复核返回 201 与持久化编号")
    nu_id = body.get("review_id") if status == 201 else None
    check(isinstance(nu_id, int), f"返回持久化编号 review_id={nu_id}")
    if status == 201:
        tr = body["evidence"]["fixpoint_iterations"][0]
        stages = [s["locations"] for s in tr["stages"]]
        check(body["conclusion"]["initial_satisfied"] is True,
              "初始位置 s0 满足（放行）")
        check(body["conclusion"]["satisfied_set"] == ["s0", "s1"],
              "满足集按位置标识排序为 [s0, s1]")
        check(tr["fixpoint"] == "nu" and tr["start"] == ["s0", "s1"],
              "ν 迭代自位置全集 [s0, s1] 出发")
        check(all(is_subset(stages[i + 1], stages[i])
                  for i in range(len(stages) - 1)),
              "ν 迭代序列单调下降")
        check(stages[-1] == stages[-2], "相邻两轮相等，达到稳定点")
        check(tr["stable"] == ["s0", "s1"], "稳定集合为 [s0, s1]")

        status, reread = request("GET", f"/api/v1/reviews/{nu_id}")
        check(status == 200 and reread["review_id"] == nu_id
              and reread["evidence"]["fixpoint_iterations"][0]["stable"] == ["s0", "s1"],
              f"按编号 {nu_id} 读回结论与 ν 稳定证据")

        # 策略审计：安全自循环须由验证方拥有位置策略
        status, audit = request("POST", f"/api/v1/reviews/{nu_id}/audits")
        check(status == 201, "在已保存复核上发起策略审计返回 201")
        nu_audit_id = audit.get("audit_id") if status == 201 else None
        if status == 201:
            check(audit["source"]["review_id"] == nu_id, "审计记录带来源复核编号")
            check(audit["formula"]["source"] == "νX.(safe & []X)"
                  and audit["formula"]["nnf"] == "νX.(safe & []X)",
                  "审计记录公式摘要（原文与否定范式闭包）")
            binder = audit["formula"]["binders"][0]
            check(binder["fixpoint"] == "nu" and binder["priority"] % 2 == 0,
                  "ν 绑定取偶优先级（绑定层级精确）")
            check(audit["game"]["initial_vertex"] == "s0#0"
                  and audit["game"]["vertex_count"] == len(audit["game"]["vertices"]),
                  "博弈顶点 = 公式闭包 × 位置，初始顶点 s0#0")
            check(audit["conclusion"]["winner"] == "verifier"
                  and audit["conclusion"]["consistent_with_review"] is True,
                  "安全自循环：验证方胜，可在任意无限迁移中持续保证满足")
            check(audit["strategy"]["player"] == "verifier"
                  and audit["strategy"]["memoryless"] is True
                  and audit["strategy"]["move_count"] == 0,
                  "验证方策略为位置策略（无选择点需裁决：选择权属挑战方）")
            opp = audit["opponent_choices"]
            check(opp["all_choices_stay_in_winner_region"] is True
                  and opp["player"] == "challenger" and opp["vertices"],
                  "列出挑战方每个选择点")
            all_in = all(c["target_region"] == "verifier"
                         for info in opp["vertices"].values()
                         for c in info["choices"])
            check(all_in, "挑战方每种合法选择都进入验证方胜区，可逐步复算")
            # [] 选择点的合法选择须标注迁移标识，进入顶点带胜方区域
            modal = [c for info in opp["vertices"].values()
                     for c in info["choices"] if c["via_transitions"]]
            check(len(modal) == 2
                  and all(c["target_region"] == "verifier" for c in modal)
                  and sorted(c["via_transitions"][0] for c in modal) == ["e1", "e2"],
                  "模态选择携带迁移标识（e1/e2），进入顶点带胜方区域")
            status, reread_audit = request("GET", f"/api/v1/audits/{nu_audit_id}")
            check(status == 200 and reread_audit["audit_id"] == nu_audit_id
                  and reread_audit["winning_regions"] == audit["winning_regions"]
                  and reread_audit["strategy"] == audit["strategy"],
                  f"按编号 {nu_audit_id} 真实接口再次读取审计区域与策略")

    # ---- 场景 2：μ 扩展，可达 goal -----------------------------------------
    print("场景 2 μX.(goal | <>X) 可达 goal")
    mu_payload = {
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
    status, body = request("POST", "/api/v1/reviews", mu_payload)
    check(status == 201, "创建复核返回 201")
    mu_id = body.get("review_id") if status == 201 else None
    if status == 201:
        tr = body["evidence"]["fixpoint_iterations"][0]
        stages = [s["locations"] for s in tr["stages"]]
        check(body["conclusion"]["initial_satisfied"] is True,
              "满足集扩展到初始位置 s0")
        check(body["conclusion"]["satisfied_set"] == ["g", "s0", "s1"],
              "满足集恰为可达 goal 的位置（x 排除）")
        check(tr["fixpoint"] == "mu" and tr["start"] == [],
              "μ 迭代自空集 ∅ 出发")
        check(stages[:4] == [[], ["g"], ["g", "s1"], ["g", "s0", "s1"]],
              "μ 逐轮扩展：∅ → {g} → {g,s1} → {g,s0,s1}")
        check(all(is_subset(stages[i], stages[i + 1])
                  for i in range(len(stages) - 1)),
              "μ 迭代序列单调上升")
        check(stages[-1] == stages[-2], "相邻两轮相等，达到稳定点")

        status, reread = request("GET", f"/api/v1/reviews/{mu_id}")
        check(status == 200
              and reread["conclusion"]["satisfied_set"] == ["g", "s0", "s1"],
              f"按编号 {mu_id} 读回 μ 扩展结论与逐轮证据")

        # 策略审计：μ 可达场景验证方在 <> 选择点裁决走向 goal
        status, audit = request("POST", f"/api/v1/reviews/{mu_id}/audits")
        check(status == 201, "μ 复核上发起策略审计返回 201")
        if status == 201:
            check(audit["conclusion"]["winner"] == "verifier",
                  "μ 可达：验证方胜")
            binder = audit["formula"]["binders"][0]
            check(binder["fixpoint"] == "mu" and binder["priority"] % 2 == 1,
                  "μ 绑定取奇优先级")
            moves = audit["strategy"]["moves"]
            to_goal = [m for m in moves.values() if m["to_location"] == "g"]
            check(bool(to_goal) and all(m["target_region"] == "verifier"
                                        for m in to_goal),
                  "验证方策略在 <> 选择点裁决走向 g 且留在胜区")
            check("x#0" in audit["winning_regions"]["challenger"],
                  "不可达 goal 的 x 根顶点归入挑战方胜区")

    # ---- 场景 3：危险迁移使前式不满足 --------------------------------------
    print("场景 3 危险迁移使 νX.(safe & []X) 不满足")
    danger_payload = {
        "locations": ["d", "s0"],
        "initial": "s0",
        "propositions": [{"location": "s0", "proposition": "safe"}],
        "transitions": [
            {"id": "e1", "source": "s0", "target": "s0"},
            {"id": "e2", "source": "s0", "target": "d"},
        ],
        "formula": "νX.(safe & []X)",
    }
    status, body = request("POST", "/api/v1/reviews", danger_payload)
    check(status == 201, "创建复核返回 201")
    danger_id = body.get("review_id") if status == 201 else None
    if status == 201:
        tr = body["evidence"]["fixpoint_iterations"][0]
        stages = [s["locations"] for s in tr["stages"]]
        check(body["conclusion"]["initial_satisfied"] is False,
              "存在危险迁移时初始位置不满足")
        check(body["conclusion"]["release_permitted"] is False,
              "release_permitted=false，不得放行")
        check(body["conclusion"]["satisfied_set"] == [],
              "满足集为空")
        check(tr["stable"] == [] and stages[-1] == stages[-2] == [],
              "ν 迭代下降至空集并给出稳定证据")
        status, reread = request("GET", f"/api/v1/reviews/{danger_id}")
        check(status == 200
              and reread["conclusion"]["release_permitted"] is False,
              f"按编号 {danger_id} 读回不放行结论")

        # 策略审计：危险后继导致不放行时须由挑战方拥有位置策略
        status, audit = request("POST", f"/api/v1/reviews/{danger_id}/audits")
        check(status == 201, "危险复核上发起策略审计返回 201")
        if status == 201:
            check(audit["conclusion"]["winner"] == "challenger"
                  and audit["conclusion"]["guarantees"] == "unsatisfaction",
                  "挑战方胜：初始位置不满足可在任意无限迁移中被持续保证")
            moves = audit["strategy"]["moves"]
            danger = [m for m in moves.values() if m["via_transitions"] == ["e2"]]
            check(bool(danger) and danger[0]["to_location"] == "d",
                  "挑战方策略在模态选择点经危险迁移 e2 走向 d")
            # 位置策略键为博弈顶点（位置标识 # 闭包节点），按位置稳定裁决
            check(all("#" in vid for vid in moves),
                  "策略按位置标识 × 闭包节点裁决，与到达历史无关")
            total = (len(audit["winning_regions"]["verifier"])
                     + len(audit["winning_regions"]["challenger"]))
            check(total == audit["game"]["vertex_count"],
                  "各博弈顶点都归入且仅归入一个胜方区域")
            danger_audit_id = audit["audit_id"]
            status, reread_audit = request("GET", f"/api/v1/audits/{danger_audit_id}")
            check(status == 200
                  and reread_audit["conclusion"]["winner"] == "challenger"
                  and reread_audit["strategy"]["moves"] == moves,
                  f"按编号 {danger_audit_id} 再次读取挑战方策略")

    # ---- 场景 4：悬空迁移必须被拒绝且不发编号 ------------------------------
    print("场景 4 悬空迁移拒绝")
    bad = dict(nu_payload)
    bad["transitions"] = nu_payload["transitions"] + [
        {"id": "e9", "source": "s0", "target": "ghost"}
    ]
    status, body = request("POST", "/api/v1/reviews", bad)
    check(status == 422 and body["error"]["code"] == "DANGLING_TRANSITION",
          "悬空迁移返回 422 DANGLING_TRANSITION")
    check(body.get("persisted") is False and body.get("review_id") is None,
          "拒绝响应声明 persisted=false 且无编号")

    status, missing = request("GET", "/api/v1/reviews/999999")
    check(status == 404, "不存在的编号读取返回 404（无悬空记录可读）")

    status, health2 = request("GET", "/healthz")
    expected = reviews_before + 3  # 仅三个合规场景落库
    check(status == 200 and health2["reviews"] == expected,
          f"拒绝请求未落库：记录数 {health2.get('reviews')} == {expected}")
    check(health2["audits"] == audits_before + 3,
          f"三个场景各落库一条审计：审计记录数 {health2.get('audits')} "
          f"== {audits_before} + 3")

    if failures:
        print(f"\n冒烟验收失败：{len(failures)} 项断言未通过")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\n冒烟验收全部通过：μ 扩展、ν 收敛、危险迁移拒绝放行、拒绝不发编号、"
          "策略审计（验证方/挑战方位置策略）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
