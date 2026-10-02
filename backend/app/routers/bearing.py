"""支座维护接口：维护桥梁支座，覆盖防锈处理、纠偏复位、安排更换等动作。

重构后所有写操作都带批次语义：

- POST /inspections       并发补检：按批次号幂等，证据/结论/建议同批落地；
- GET  /batches/{no}      按批次号回读完整结果；
- GET  /lineage           所属桥梁档案读取同一批次结果；
- GET  /todos             工程待办入口；
- GET  /notifications     通知入口；
- POST /migrate           显式触发存量按支座编号回填（通常启动时已自动完成）。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app import lineage
from app.schemas import ActionResult, BatchResult, EntryPayload, PageResult
from app.services.bearing import MIGRATION_BATCH_NO, BearingService

router = APIRouter(prefix="/api/bearing", tags=["支座维护"])

service = BearingService()

LIST_FIELDS = ["支座编号", "所属桥梁", "支座类型", "设计承载力", "位移量", "锈蚀程度", "最近检查", "支座状态"]
STATUSES = ["正常", "锈蚀", "偏位", "需更换"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按支座编号检索"),
    status: str | None = Query(default=None, description="正常、锈蚀、偏位、需更换"),
    bridge: str | None = Query(default=None, description="按所属桥梁过滤"),
    batch_no: str | None = Query(default=None, description="只看某个谱系批次改动过的支座"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按支座编号、所属桥梁与状态过滤支座列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(
        keyword=keyword, status=status, bridge=bridge, batch_no=batch_no, page=page, size=size
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/batches")
def list_batches() -> dict[str, Any]:
    """已落地的谱系批次清单。"""
    return {"items": service.list_batches()}


@router.get("/batches/{batch_no}")
def get_batch(batch_no: str) -> dict[str, Any]:
    """按批次号回读：事件流 + 所属桥梁档案/待办/通知三个入口的同批结果。"""
    try:
        return service.get_batch(batch_no)
    except lineage.LineageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/lineage")
def archive_lineage(
    bridge: str | None = Query(default=None, description="按桥梁名称过滤"),
) -> dict[str, Any]:
    """所属桥梁档案入口：读取桥梁对应支座的最新批次与结论汇总。"""
    return {"items": service.archive_lineage(bridge)}


@router.get("/todos")
def project_todos(
    status: str | None = Query(default=None, description="待安排、进行中、已完成"),
) -> dict[str, Any]:
    """工程待办入口：读取批次提交后派生的处置待办。"""
    return {"items": service.project_todos(status)}


@router.get("/notifications")
def notifications(
    unread: bool = Query(default=False, description="只看未读"),
) -> dict[str, Any]:
    """通知入口：读取批次落地结论。"""
    return {"items": service.notifications(only_unread=unread)}


@router.post("/inspections", response_model=BatchResult)
def submit_inspection(payload: EntryPayload) -> BatchResult:
    """并发补检：证据、结论、建议在同一批次落地；重复批次号原样回读不追加事件。"""
    values = payload.values
    try:
        result, message = service.submit_inspection(values)
    except lineage.LineageError as exc:
        # 事务内异常已整体回滚，转成可读的 409 而不是 500。
        raise HTTPException(status_code=409, detail=f"批次未落地，已整体回滚：{exc}") from exc
    if result is None:
        return BatchResult(ok=False, message=message)
    return BatchResult(
        ok=True,
        message=message,
        batch_no=str(result.get("批次号") or values.get("批次号") or ""),
        replayed=bool(result.get("replayed")),
        events=result.get("events") or [],
        result=result.get("result"),
    )


@router.post("/migrate")
def migrate_stock() -> dict[str, Any]:
    """显式触发存量迁移；已迁移时幂等返回既有迁移批次。"""
    migrated = service.ensure_migrated()
    if migrated is not None:
        return {"ok": True, "message": "存量支座已按支座编号回填", "batch": migrated}
    return {
        "ok": True,
        "message": "存量迁移此前已落地，无需重复回填",
        "batch": service.get_batch(MIGRATION_BATCH_NO),
    }


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出支座维护清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "bearing", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条桥梁支座明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"桥梁支座 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条桥梁支座，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段或编号冲突：{'、'.join(missing)}")
    return ActionResult(ok=True, message="桥梁支座已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条桥梁支座执行防锈处理、纠偏复位、安排更换；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    result, message = service.run_action(entry_id, action)
    if result is None:
        return ActionResult(ok=False, message=message)
    bearings = ((result.get("result") or {}).get("bearings") or [])
    entry = bearings[0] if bearings else None
    return ActionResult(ok=True, message=message, entry=entry)
