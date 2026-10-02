"""支座维护接口：存量迁移、谱系批次补检与同批次结果读取。

路由分四组：
- 存量迁移：``POST /migration`` 按支座编号回填谱系；
- 补检事务：``POST /inspections`` 证据/结论/建议同批提交，批次号幂等；
- 同批次读取：批次详情、所属桥梁档案、工程待办、通知入口；
- 旧版单条接口：列表、明细、动作，保持前端兼容。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, InspectionBatchPayload, PageResult
from app.services.bearing import BearingService

router = APIRouter(prefix="/api/bearing", tags=["支座维护"])

service = BearingService()

LIST_FIELDS = ["支座编号", "所属桥梁", "支座类型", "设计承载力", "位移量", "锈蚀程度", "最近检查", "支座状态"]
STATUSES = ["正常", "锈蚀", "偏位", "需更换"]


# ------------------------------------------------------------ 同批次读取

@router.get("/batches", response_model=PageResult[dict])
def list_batches(
    bridge: str | None = Query(default=None, description="按所属桥梁过滤谱系批次"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """列出已提交的谱系批次；事务未落地的批次不会出现。"""
    rows = service.list_batches(bridge_name=bridge)
    total = len(rows)
    start = max(page - 1, 0) * size
    return PageResult(items=rows[start:start + size], total=total, page=page, size=size)


@router.get("/batches/{batch_no}", response_model=dict)
def get_batch(batch_no: str) -> dict[str, Any]:
    """读取一个批次的完整结果：证据、结论、建议、桥梁档案、待办、通知同源。"""
    batch = service.get_batch(batch_no)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"谱系批次 {batch_no} 不存在或尚未提交")
    return batch


@router.get("/bridge-archive", response_model=PageResult[dict])
def bridge_archive(bridge: str = Query(description="桥梁名称或编号")) -> PageResult[dict]:
    """所属桥梁档案入口：只读取被支座批次投影过的档案，批次号一并返回。"""
    items = service.bridge_archive(bridge)
    return PageResult(items=items, total=len(items), page=1, size=len(items) or 1)


@router.get("/todos", response_model=PageResult[dict])
def pending_todos(
    batch_no: str | None = Query(default=None, description="只看某一批次派生的工程待办"),
    include_done: bool = Query(default=False, description="是否包含已完成待办"),
) -> PageResult[dict]:
    """工程待办入口：待办由已提交批次派生，批次号回链可溯源。"""
    items = service.pending_todos(batch_no=batch_no, only_pending=not include_done)
    return PageResult(items=items, total=len(items), page=1, size=len(items) or 1)


@router.get("/notifications", response_model=dict)
def notifications(only_unread: bool = Query(default=True)) -> dict[str, Any]:
    """通知入口：通知与工程待办、桥梁档案读同一批次结果。"""
    items = service.notifications(only_unread=only_unread)
    return {"total": len(items), "items": items}


@router.post("/notifications/{notification_id}/read", response_model=ActionResult)
def mark_notification_read(notification_id: int) -> ActionResult:
    entry = service.mark_notification_read(notification_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"通知 {notification_id} 不存在")
    return ActionResult(ok=True, message="通知已标记为已读", entry=entry)


# ------------------------------------------------------------ 存量迁移

@router.post("/migration", response_model=ActionResult)
def migrate_legacy() -> ActionResult:
    """把存量桥梁支座按支座编号迁移回填到谱系；重复执行只跳过、不追加事件。"""
    result = service.migrate_legacy_bearings()
    return ActionResult(
        ok=True,
        message=(
            f"存量迁移完成：回填 {result['迁移数量']} 个支座，"
            f"跳过已迁移 {result['跳过数量']} 个"
        ),
        entry=result,
    )


# ------------------------------------------------------------ 补检事务

@router.post("/inspections", response_model=ActionResult)
def submit_inspection(payload: InspectionBatchPayload) -> ActionResult:
    """提交补检批次：证据、结论、建议同事务落库；同批次号重复提交幂等忽略。

    返回 409 的语义是「提交被拒绝且整批回滚」（如桥梁档案缺失、结论非法、
    显式模拟失败），此时不会产生任何事件、待办或通知。
    """
    try:
        batch, message, duplicated = service.submit_inspection(payload.model_dump())
    except Exception as exc:  # 事务已在服务层回滚，这里只负责给出可读响应
        raise HTTPException(status_code=409, detail=f"补检批次未落地，已整体回滚：{exc}") from exc
    if batch is None:
        return ActionResult(ok=False, message=message)
    result = ActionResult(ok=True, message=message, entry=batch)
    if duplicated:
        # 幂等命中不是错误，但需要让调用方能区分「首次提交」与「重复忽略」。
        result.message = message
    return result


# ------------------------------------------------------------ 旧版单条接口

@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按支座编号检索"),
    status: str | None = Query(default=None, description="正常、锈蚀、偏位、需更换"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按支座编号与状态过滤支座维护列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条桥梁支座，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="桥梁支座已登记", entry=entry)


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


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条桥梁支座执行防锈处理、纠偏复位、安排更换；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
