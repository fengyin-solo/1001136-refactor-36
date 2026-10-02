"""市政道路桥梁养护管理平台 后端服务入口。

启动：uvicorn app.main:app --host 127.0.0.1 --port 8000
健康检查：GET /api/health
"""
from __future__ import annotations

import contextlib
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import ROUTERS
from app.services.bearing import BearingService
from app.store import store

logger = logging.getLogger("bearing-migration")

app = FastAPI(title="市政道路桥梁养护管理平台", version="1.0.0")


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    # 启动即做一次存量迁移回填：按支座编号补齐谱系批次，重复启动幂等跳过。
    summary = BearingService().migrate_legacy_bearings()
    logger.info(
        "支座存量迁移：回填 %s 个，跳过 %s 个",
        summary["迁移数量"],
        summary["跳过数量"],
    )
    yield


app = FastAPI(title="市政道路桥梁养护管理平台", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for module in ROUTERS:
    app.include_router(module.router)


@app.get("/api/health")
def health() -> dict[str, object]:
    """健康检查：确认服务已经监听、示例数据已经就绪。"""
    return {"ok": True, "app": settings.app_name, "modules": len(store.module_names())}


@app.get("/api/overview")
def overview() -> dict[str, object]:
    """运营概览：把各业务模块的待处理量汇总成看板卡片。"""
    return store.overview()
