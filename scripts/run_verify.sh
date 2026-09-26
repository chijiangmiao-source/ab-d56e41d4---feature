#!/bin/sh
# 一次性验收：代码测试 -> 构建检查 -> HTTP 冒烟；结束即退出，退出码报告结果。
set -eu

cd "${APP_DIR:-/app}"
export PYTHONPATH="${APP_DIR:-/app}"
SMOKE_BASE_URL="${SMOKE_BASE_URL:-http://api:8080}"
export SMOKE_BASE_URL

echo "== 1/3 等待复核服务健康 =="
python - <<'PY'
import os, sys, time, urllib.request
base = os.environ["SMOKE_BASE_URL"]
deadline = time.time() + 60
while time.time() < deadline:
    try:
        with urllib.request.urlopen(base + "/healthz", timeout=3) as r:
            if r.status == 200:
                print("服务已就绪:", r.read().decode().strip())
                sys.exit(0)
    except Exception:
        time.sleep(1)
print("等待服务健康超时", file=sys.stderr)
sys.exit(1)
PY

echo "== 2/3 代码测试与构建检查 =="
python -m pytest -q tests
python -m compileall -q app smoke tests

echo "== 3/3 HTTP 冒烟（μ 扩展 / ν 收敛 / 危险迁移 / 拒绝不发编号）=="
python smoke/smoke_http.py
