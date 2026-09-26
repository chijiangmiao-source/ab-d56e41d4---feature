# 中子束联锁固定点复核接口

对含递归定义的放行条件做模型检验：公式为模态 μ-演算，μ（最小固定点）自空集、
ν（最大固定点）自全集单调迭代至稳定，直接判断**无限行为**，不以有限路径回放代替。

## 公式语言

仅允许：位置命题、`!`、`&`、`|`、`<>`（存在后继）、`[]`（所有后继）、
`μX.`（ASCII：`muX.`）、`νX.`（ASCII：`nuX.`）。

- 变量引用**最近绑定者**；重复绑定名拒绝。
- 每个绑定变量在其**绑定点与引用点之间**必须受模态算子 `<>`/`[]` 守卫。
- 变量不得出现在作用域内奇数重否定下（保证固定点算子单调）。
- 未绑定变量、未声明命题、残缺符号、语法残留一律 422 拒绝。

语义（Knaster–Tarski）：`μX.F(X)` 从 `∅` 迭代 `F`，`νX.F(X)` 从位置全集迭代 `F`，
至相邻两轮相等为止；嵌套绑定各自维护环境，不污染外层。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/reviews` | 提交 2–24 个唯一位置、初始位置、位置命题、带唯一标识的有向迁移和公式 |
| GET | `/api/v1/reviews/{id}` | 按持久化编号读取结论与每个固定点的逐轮迭代证据 |
| GET | `/healthz` | 健康检查 |

创建成功返回 `review_id`、按位置标识排序的 `satisfaction_set`、初始位置是否满足、
`release_permitted`，以及每个固定点的起点（μ 为 `[]`、ν 为全集）、逐轮集合、
相邻两轮相等的稳定证据。校验失败返回 422、`persisted: false`、`review_id: null`，
**不写库、不产生可读取编号**；悬空迁移、重复迁移标识同样拒绝。

验收场景（见 `smoke/smoke_http.py`）：

1. `νX.(safe & []X)`：安全自循环满足，ν 自全集单调下降至稳定；
2. `μX.(goal | <>X)`：满足集自 ∅ 逐轮扩展到初始位置；
3. 危险迁移：同一 ν 公式在初始位置不满足、`release_permitted=false`；
4. 悬空迁移：422、`persisted=false`，拒绝后记录数不增长。

## 运行

```bash
# 启动服务（端口可配置，默认 8080）
docker compose up -d api
APP_PORT=9000 docker compose up -d api     # 自定义端口

# 单次验收：等待健康 -> 代码测试 -> 构建检查 -> HTTP 冒烟，结束即退出
docker compose up --build verify
docker compose inspect verify 或查看退出码：
docker compose up verify; echo "verify exit code: $?"
```

verify 服务依次执行 `pytest`（μ 扩展 / ν 收敛等代码测试）、
`python -m compileall`（构建检查）、`smoke/smoke_http.py`（真实 HTTP 冒烟），
全部成功退出码 0，任一失败非 0；`restart: "no"`，单次结束即退出。

本地（无 Docker）：

```bash
pip install -r requirements.txt
PYTHONPATH=. uvicorn app.main:app --port 8080
SMOKE_BASE_URL=http://127.0.0.1:8080 python smoke/smoke_http.py
PYTHONPATH=. pytest -q
```
