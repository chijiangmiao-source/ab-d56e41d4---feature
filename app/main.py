"""固定点复核 HTTP 接口。

POST /api/v1/reviews          创建复核（校验失败不落库、不返回编号）
GET  /api/v1/reviews/{id}     按编号读取结论与规范证据
GET  /healthz                 健康检查
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import __version__
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
    description="对含递归（μ/ν 固定点）定义的放行条件进行模型检验并持久化证据",
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
    return {"status": "ok", "version": __version__, "reviews": store.count()}


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
