"""固定点复核与策略审计 HTTP 接口。

POST /api/v1/reviews                    创建复核（校验失败不落库、不返回编号）
GET  /api/v1/reviews/{id}               按编号读取结论与规范证据
POST /api/v1/reviews/{id}/audits        对已保存复核发起策略审计（奇偶博弈求解）
GET  /api/v1/audits/{id}                按编号读取审计：胜方区域、无记忆策略与对手选项
GET  /healthz                           健康检查
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import __version__
from .audit import build_audit
from .service import RequestError, build_review
from .storage import ReviewStore

DB_PATH = os.environ.get("REVIEW_DB_PATH", "/data/reviews.db")

_store: ReviewStore | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _store
    _store = ReviewStore(DB_PATH)
    try:
        yield
    finally:
        _store.close()


app = FastAPI(
    title="中子束联锁固定点复核接口",
    description="对含递归（μ/ν 固定点）定义的放行条件进行模型检验、持久化证据，"
                "并在已保存复核上发起奇偶博弈策略审计",
    version=__version__,
    lifespan=lifespan,
)


def _store_or_503() -> ReviewStore:
    if _store is None:  # pragma: no cover - lifespan 之外不会发生
        raise RuntimeError("存储未初始化")
    return _store


@app.exception_handler(RequestError)
async def request_error_handler(_: Request, exc: RequestError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
            },
            "persisted": False,
            "review_id": None,
        },
    )


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    store = _store_or_503()
    return {
        "status": "ok",
        "version": __version__,
        "reviews": store.count(),
        "audits": store.count_audits(),
    }


@app.post("/api/v1/reviews", status_code=201)
async def create_review(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={
                "error": {"code": "INVALID_JSON", "message": "请求体必须为合法 JSON",
                          "details": []},
                "persisted": False,
                "review_id": None,
            },
        )
    if not isinstance(payload, dict):
        return JSONResponse(
            status_code=400,
            content={
                "error": {"code": "INVALID_REQUEST", "message": "请求体必须为 JSON 对象",
                          "details": []},
                "persisted": False,
                "review_id": None,
            },
        )

    # 先完成全部校验与求值，再持久化：被拒绝的请求不产生可读编号。
    record = build_review(payload)
    review_id = _store_or_503().create(record)
    record_out = dict(record)
    record_out["review_id"] = review_id
    return JSONResponse(status_code=201, content=record_out)


@app.get("/api/v1/reviews/{review_id}")
async def read_review(review_id: int) -> JSONResponse:
    record = _store_or_503().get(review_id)
    if record is None:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "NOT_FOUND",
                               "message": f"编号 {review_id} 的复核记录不存在",
                               "details": []}},
        )
    record_out = dict(record)
    record_out["review_id"] = review_id
    return JSONResponse(content=record_out)


@app.post("/api/v1/reviews/{review_id}/audits", status_code=201)
async def create_audit(review_id: int) -> JSONResponse:
    store = _store_or_503()
    record = store.get(review_id)
    if record is None:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "NOT_FOUND",
                               "message": f"编号 {review_id} 的复核记录不存在，"
                                          "无法发起策略审计",
                               "details": []}},
        )
    # 基于已持久化的复核请求重建模型：公式闭包 × 位置 × 绑定层级构成有限
    # 奇偶博弈，按 μ/ν 嵌套优先级精确求解后整体落库。
    audit = build_audit(record, review_id)
    audit_id = store.create_audit(review_id, audit)
    audit_out = dict(audit)
    audit_out["audit_id"] = audit_id
    return JSONResponse(status_code=201, content=audit_out)


@app.get("/api/v1/audits/{audit_id}")
async def read_audit(audit_id: int) -> JSONResponse:
    audit = _store_or_503().get_audit(audit_id)
    if audit is None:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "NOT_FOUND",
                               "message": f"编号 {audit_id} 的策略审计不存在",
                               "details": []}},
        )
    audit_out = dict(audit)
    audit_out["audit_id"] = audit_id
    return JSONResponse(content=audit_out)
