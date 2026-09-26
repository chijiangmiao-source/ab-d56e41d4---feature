"""一次性 HTTP 冒烟验收：对运行中的复核服务发起验收场景并断言。

场景与验收点：
1. νX.(safe & []X)：安全自循环满足；ν 自全集单调下降至稳定，证据相邻两轮相等。
1b. 策略审计：安全自循环得到验证方无记忆策略，挑战方每种合法选择均留在验证方胜区。
2. μX.(goal | <>X)：可达 goal 的满足集自 ∅ 逐轮扩展至初始位置。
3. 危险迁移：同一 ν 公式在初始位置不满足、不得放行。
3b. 策略审计：危险后继导致不放行时得到挑战方无记忆策略，且策略指向危险位置。
4. 悬空迁移：422 拒绝、persisted=false、不产生可读编号；不存在的复核/审计编号为 404。

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

    # ---- 场景 1b：策略审计——安全自循环须得到验证方策略 ---------------------
    print("场景 1b 策略审计：安全自循环的验证方策略")
    status, audit = request("POST", f"/api/v1/reviews/{nu_id}/audits")
    check(status == 201, "对已保存复核发起策略审计返回 201")
    nu_audit_id = audit.get("audit_id") if status == 201 else None
    if status == 201:
        check(audit["review_id"] == nu_id, "审计记录来源复核编号")
        check(audit["result"]["initial_winner"] == "verifier",
              "初始顶点由验证方获胜（任意无限迁移下持续安全）")
        check(audit["result"]["consistent_with_review"] is True,
              "博弈胜方与复核满足结论一致")
        check(audit["strategy"]["player"] == "verifier"
              and audit["strategy"]["memoryless"] is True,
              "生成验证方无记忆（与到达历史无关）策略")
        verts = {v["id"]: v for v in audit["game"]["vertices"]}
        check(all(verts[o["vertex"]]["owner"] == "challenger"
                  for o in audit["opponent_options"]),
              "对手选择点列表覆盖挑战方全部顶点")
        check(all(opt["region"] == "verifier"
                  for o in audit["opponent_options"] if o["in_winner_region"]
                  for opt in o["options"]),
              "挑战方每种合法选择均进入验证方胜区（无法逃脱）")
        status, reread = request("GET", f"/api/v1/audits/{nu_audit_id}")
        check(status == 200 and reread["audit_id"] == nu_audit_id
              and reread["result"]["initial_winner"] == "verifier"
              and reread["strategy"]["moves"] == audit["strategy"]["moves"],
              f"按编号 {nu_audit_id} 经真实接口读回验证方策略")

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

    # ---- 场景 3b：策略审计——危险后继导致不放行时须得到挑战方策略 -----------
    print("场景 3b 策略审计：危险后继的挑战方策略")
    status, audit = request("POST", f"/api/v1/reviews/{danger_id}/audits")
    check(status == 201, "对不放行复核发起策略审计返回 201")
    danger_audit_id = audit.get("audit_id") if status == 201 else None
    if status == 201:
        check(audit["result"]["initial_winner"] == "challenger",
              "初始顶点由挑战方获胜（不满足可在无限迁移中持续保证）")
        check(audit["result"]["consistent_with_review"] is True,
              "博弈胜方与复核不放行结论一致")
        check(audit["strategy"]["player"] == "challenger"
              and audit["strategy"]["memoryless"] is True,
              "生成挑战方无记忆策略")
        box_moves = [m for m in audit["strategy"]["moves"]
                     if m["kind"] == "box" and m["location"] == "s0"]
        check(any(m["choose_location"] == "d" for m in box_moves),
              "挑战方策略在 [] 选择点指向危险后继 d")
        status, reread = request("GET", f"/api/v1/audits/{danger_audit_id}")
        check(status == 200 and reread["audit_id"] == danger_audit_id
              and reread["result"]["initial_winner"] == "challenger"
              and reread["strategy"]["moves"] == audit["strategy"]["moves"],
              f"按编号 {danger_audit_id} 经真实接口读回挑战方策略")

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

    status, body = request("POST", "/api/v1/reviews/999999/audits")
    check(status == 404, "对不存在的复核发起策略审计返回 404")
    status, body = request("GET", "/api/v1/audits/999999")
    check(status == 404, "不存在的审计编号读取返回 404")

    status, health2 = request("GET", "/healthz")
    expected = reviews_before + 3  # 仅三个合规场景落库
    check(status == 200 and health2["reviews"] == expected,
          f"拒绝请求未落库：记录数 {health2.get('reviews')} == {expected}")

    if failures:
        print(f"\n冒烟验收失败：{len(failures)} 项断言未通过")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\n冒烟验收全部通过：μ 扩展、ν 收敛、危险迁移拒绝放行、拒绝不发编号、"
          "安全自循环验证方策略、危险后继挑战方策略均可经真实接口读取")
    return 0


if __name__ == "__main__":
    sys.exit(main())
