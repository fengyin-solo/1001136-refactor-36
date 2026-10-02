"""支座维护业务规则：存量迁移、谱系批次与事务边界都收在这里。

重构后的领域分三层，全部以「谱系批次号」串联：

1. 存量迁移（``migrate_legacy_bearings``）
   把存量桥梁支座按「支座编号」逐条迁移回填：每个支座生成一个迁移批次号，
   回填谱系批次号/迁移时间，并写入一条「存量迁移」谱系事件。迁移幂等，
   已回填的支座重复执行不会再追加事件。

2. 补检批次（``submit_inspection``）
   一次补检是一个事务：检查证据、支座维护结论、维护建议三类事件必须写进
   同一谱系批次；同时把结论投影到所属桥梁档案、生成工程待办和通知。
   事务未落地（任何一步失败）时整体回滚，不留半截批次。

3. 同批次读取（``get_batch`` / ``bridge_archive`` / ``pending_todos`` /
   ``notifications``）
   所属桥梁档案、工程待办、通知入口都只从已提交批次派生，读的是同一份
   批次结果。并发补检按批次号幂等：重复提交直接返回既有批次，不追加事件。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import TransactionAborted, store

MODULE = "bearing"
REQUIRED_FIELDS = ["支座编号", "所属桥梁", "支座类型"]
STATUS_ORDER = ["正常", "锈蚀", "偏位", "需更换"]
# 结论严重度排序：取一批检查里最重的结论作为支座与桥梁的维护结论。
_SEVERITY = {status: index for index, status in enumerate(STATUS_ORDER)}
ACTION_RULES = {"防锈处理": "正常", "纠偏复位": "正常", "安排更换": "需更换"}
NEGATIVE_ACTIONS = []

# 谱系批次派生表（与 bearing 主表在同一事务里读写，保证原子）。
BATCH_TABLE = "bearing_batch"
EVENT_TABLE = "bearing_batch_event"
TODO_TABLE = "bearing_pending_todo"
NOTIFICATION_TABLE = "bearing_notification"

LINEAGE_FIELD = "谱系批次号"
MIGRATED_AT_FIELD = "迁移回填时间"
MIGRATION_BATCH_PREFIX = "MIG-"
INSPECTION_BATCH_PREFIX = "INSP-"

# 证据/结论/建议三类事件的中文名，读侧直接按它分组。
EVENT_EVIDENCE = "检查证据"
EVENT_CONCLUSION = "支座维护结论"
EVENT_SUGGESTION = "维护建议"
EVENT_MIGRATION = "存量迁移"

_ACTION_BY_STATUS = {"锈蚀": "防锈处理", "偏位": "纠偏复位", "需更换": "安排更换"}
_SUGGESTION_BY_STATUS = {
    "锈蚀": "对支座钢构件进行除锈防腐处理，复紧连接螺栓。",
    "偏位": "对偏位支座实施纠偏复位，并复查垫石与梁体限位。",
    "需更换": "安排更换同型号支座，同步检查相邻支座受力情况。",
    "正常": "维持正常巡检频次，无需专项处置。",
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _new_batch_no(prefix: str) -> str:
    sequence = sum(1 for row in store.rows(BATCH_TABLE) if str(row.get("批次号", "")).startswith(prefix)) + 1
    return f"{prefix}{datetime.now().strftime('%Y%m%d')}-{sequence:04d}"


def _append_event(
    *,
    batch_no: str,
    bearing_no: str,
    kind: str,
    content: str,
    source: str,
) -> dict[str, Any]:
    event = {
        "id": store.next_id(EVENT_TABLE),
        "批次号": batch_no,
        "支座编号": bearing_no,
        "事件类型": kind,
        "内容": content,
        "来源": source,
        "记录时间": _now(),
    }
    store.rows(EVENT_TABLE).append(event)
    return event


def _find_batch(batch_no: str) -> dict[str, Any] | None:
    for row in store.rows(BATCH_TABLE):
        if row.get("批次号") == batch_no:
            return row
    return None


class BearingService:
    # ------------------------------------------------------------------ 列表

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("支座编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        with store.batch_lock, store.transaction():
            rows = store.rows(MODULE)
            entry = {"id": store.next_id(MODULE)}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"桥梁支座 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于支座维护可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"桥梁支座已{action}"

    # ------------------------------------------------------------ 存量迁移

    def migrate_legacy_bearings(self) -> dict[str, Any]:
        """把存量支座按支座编号迁移回填到谱系，整体一个事务，可重复执行。

        - 已有谱系批次号的存量支座视为已迁移，直接跳过（幂等，不追加事件）；
        - 存量行缺支座编号属于脏数据：整个迁移放弃并回滚，不允许迁一半；
        - 返回迁移/跳过计数，供接口和启动日志核对。
        """
        migrated: list[str] = []
        skipped: list[str] = []
        with store.batch_lock, store.transaction():
            for row in store.rows(MODULE):
                bearing_no = str(row.get("支座编号") or "").strip()
                if not bearing_no:
                    raise TransactionAborted("存量数据存在缺少支座编号的记录，已整体回滚本次迁移")
                if row.get(LINEAGE_FIELD):
                    skipped.append(bearing_no)
                    continue
                batch_no = _new_batch_no(MIGRATION_BATCH_PREFIX)
                migrated_at = _now()
                store.rows(BATCH_TABLE).append({
                    "id": store.next_id(BATCH_TABLE),
                    "批次号": batch_no,
                    "批次类型": EVENT_MIGRATION,
                    "所属桥梁": str(row.get("所属桥梁") or "").strip(),
                    "支座编号": [bearing_no],
                    "维护结论": str(row.get("status") or STATUS_ORDER[0]),
                    "状态": "已提交",
                    "创建时间": migrated_at,
                    "提交时间": migrated_at,
                })
                _append_event(
                    batch_no=batch_no,
                    bearing_no=bearing_no,
                    kind=EVENT_MIGRATION,
                    content=f"存量支座按支座编号 {bearing_no} 迁移回填，迁入时状态：{row.get('status') or STATUS_ORDER[0]}",
                    source="存量迁移",
                )
                row[LINEAGE_FIELD] = batch_no
                row[MIGRATED_AT_FIELD] = migrated_at
                migrated.append(bearing_no)
        return {
            "迁移": migrated,
            "跳过": skipped,
            "迁移数量": len(migrated),
            "跳过数量": len(skipped),
            "批次数量": len(migrated),
        }

    # ---------------------------------------------------------- 补检批次

    def submit_inspection(self, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str, bool]:
        """提交一次支座补检，三类事件与派生物写入同一谱系批次。

        返回 ``(批次, 消息, 是否重复提交)``：
        - 同一批次号并发/重复提交：只有第一次落库，后续返回既有批次且
          ``是否重复提交=True``，不会再追加任何事件、待办或通知；
        - 任一步校验/投影失败：抛错路径在事务内，整批回滚。
        """
        batch_no = str(payload.get("批次号") or "").strip()
        bridge_name = str(payload.get("所属桥梁") or "").strip()
        checks = payload.get("检查明细") or []
        simulate_failure = bool(payload.get("模拟失败"))

        if not bridge_name:
            return None, "缺少所属桥梁，无法建立谱系批次", False
        if not isinstance(checks, list) or not checks:
            return None, "检查明细为空，至少提交一个支座的检查证据", False
        if any(not str(item.get("支座编号") or "").strip() for item in checks if isinstance(item, dict)):
            return None, "存在缺少支座编号的检查明细，已拒绝提交", False

        # 判重与落库必须在同一把锁的连续持有区间内：两个携带相同批次号的并发
        # 补检会被串行成「先到提交、后到命中」，不会各写一份事件。
        with store.batch_lock:
            if batch_no:
                existing = _find_batch(batch_no)
                if existing is not None:
                    return existing, f"批次 {batch_no} 已提交，重复提交已忽略", True

            with store.transaction():
                if not batch_no:
                    batch_no = _new_batch_no(INSPECTION_BATCH_PREFIX)

                bearing_nos = [str(item["支座编号"]).strip() for item in checks]
                if len(set(bearing_nos)) != len(bearing_nos):
                    raise TransactionAborted("同一批次内支座编号重复，已整体回滚")

                submitted_at = _now()
                event_count_before = len(store.rows(EVENT_TABLE))

                # 1) 检查证据、维护结论、维护建议写入同一批次（同一事务）。
                worst = STATUS_ORDER[0]
                for item in checks:
                    bearing_no = str(item["支座编号"]).strip()
                    evidence = str(item.get("检查证据") or "").strip()
                    status = str(item.get("检查结论") or "").strip()
                    if not evidence:
                        raise TransactionAborted(f"支座 {bearing_no} 缺少检查证据，已整体回滚")
                    if status not in _SEVERITY:
                        raise TransactionAborted(
                            f"支座 {bearing_no} 的检查结论「{status}」不在允许范围"
                            f"（{'、'.join(STATUS_ORDER)}），已整体回滚"
                        )
                    suggestion = str(item.get("维护建议") or "").strip() or _SUGGESTION_BY_STATUS[status]

                    _append_event(
                        batch_no=batch_no,
                        bearing_no=bearing_no,
                        kind=EVENT_EVIDENCE,
                        content=evidence,
                        source=str(item.get("检查人") or "现场补检"),
                    )
                    _append_event(
                        batch_no=batch_no,
                        bearing_no=bearing_no,
                        kind=EVENT_CONCLUSION,
                        content=f"检查结论：{status}；建议动作：{_ACTION_BY_STATUS.get(status, '持续观察')}",
                        source="支座维护",
                    )
                    _append_event(
                        batch_no=batch_no,
                        bearing_no=bearing_no,
                        kind=EVENT_SUGGESTION,
                        content=suggestion,
                        source="支座维护",
                    )
                    if _SEVERITY[status] > _SEVERITY[worst]:
                        worst = status

                    self._apply_bearing_result(bearing_no, bridge_name, status, batch_no, submitted_at)

                # 测试/演练钩子：三类事件写完、提交前显式失败，验证事务整体回滚。
                if simulate_failure:
                    raise TransactionAborted("收到模拟失败指令，批次未落地，已整体回滚")

                batch = {
                    "id": store.next_id(BATCH_TABLE),
                    "批次号": batch_no,
                    "批次类型": "补检",
                    "所属桥梁": bridge_name,
                    "支座编号": bearing_nos,
                    "维护结论": worst,
                    "状态": "已提交",
                    "创建时间": submitted_at,
                    "提交时间": submitted_at,
                }
                store.rows(BATCH_TABLE).append(batch)

                # 2) 结论投影到所属桥梁档案（档案、待办、通知读同一批次结果）。
                self._project_bridge_archive(bridge_name, worst, batch_no, submitted_at)
                # 3) 需跟进的结论生成工程待办；每次结论都生成通知入口。
                self._project_todos(batch_no, bridge_name, checks, worst, submitted_at)
                self._project_notification(batch_no, bridge_name, bearing_nos, worst, submitted_at)

                event_count_after = len(store.rows(EVENT_TABLE))

            batch["事件数量"] = event_count_after - event_count_before
            return batch, f"批次 {batch_no} 已提交，检查证据、维护结论与维护建议已写入同一谱系", False

    def _apply_bearing_result(
        self,
        bearing_no: str,
        bridge_name: str,
        status: str,
        batch_no: str,
        submitted_at: str,
    ) -> None:
        rows = store.rows(MODULE)
        matched = next((row for row in rows if str(row.get("支座编号") or "").strip() == bearing_no), None)
        if matched is None:
            # 补检发现的新支座，在同一事务里建档，避免结论落到不存在的支座上。
            matched = {"id": store.next_id(MODULE)}
            rows.append(matched)
        matched.update({
            "支座编号": bearing_no,
            "所属桥梁": bridge_name,
            "支座类型": matched.get("支座类型") or "补检建档",
            "status": status,
            "支座状态": status,
            "最近检查": submitted_at,
            "pending": status == "需更换",
            "abnormal": status != "正常",
            LINEAGE_FIELD: batch_no,
        })

    def _project_bridge_archive(
        self,
        bridge_name: str,
        conclusion: str,
        batch_no: str,
        submitted_at: str,
    ) -> None:
        archive_rows = store.rows("bridge_info")
        matched = next(
            (
                row
                for row in archive_rows
                if bridge_name in str(row.get("桥梁名称") or "")
                or bridge_name in str(row.get("桥梁编号") or "")
            ),
            None,
        )
        if matched is None:
            # 档案缺失时不许静默丢结论：事务失败，强制先补档案或核对桥梁名称。
            raise TransactionAborted(
                f"所属桥梁「{bridge_name}」在桥梁档案中不存在，结论无法归档，已整体回滚"
            )
        previous = str(matched.get("status") or "正常")
        if _SEVERITY.get(conclusion, 0) >= _SEVERITY.get(previous, 0):
            matched["status"] = conclusion
        matched["桥梁状态"] = matched["status"]
        matched[LINEAGE_FIELD] = batch_no
        matched["最近支座批次"] = batch_no
        matched["最近支座检查"] = submitted_at
        matched["pending"] = conclusion != "正常"

    def _project_todos(
        self,
        batch_no: str,
        bridge_name: str,
        checks: list[dict[str, Any]],
        worst: str,
        submitted_at: str,
    ) -> None:
        for item in checks:
            status = str(item.get("检查结论") or "").strip()
            if status == "正常":
                continue
            bearing_no = str(item["支座编号"]).strip()
            store.rows(TODO_TABLE).append({
                "id": store.next_id(TODO_TABLE),
                "批次号": batch_no,
                "待办编号": f"TODO-{batch_no}-{bearing_no}",
                "所属桥梁": bridge_name,
                "支座编号": bearing_no,
                "处置动作": _ACTION_BY_STATUS.get(status, "持续观察"),
                "优先级": "高" if status == "需更换" else "中",
                "状态": "待处理",
                "创建时间": submitted_at,
            })

    def _project_notification(
        self,
        batch_no: str,
        bridge_name: str,
        bearing_nos: list[str],
        worst: str,
        submitted_at: str,
    ) -> None:
        needs_replace = worst == "需更换"
        store.rows(NOTIFICATION_TABLE).append({
            "id": store.next_id(NOTIFICATION_TABLE),
            "批次号": batch_no,
            "所属桥梁": bridge_name,
            "标题": f"【{'更换预警' if needs_replace else '补检结论'}】{bridge_name} 支座批次 {batch_no}",
            "内容": (
                f"批次 {batch_no} 已提交，涉及支座 {len(bearing_nos)} 个，维护结论：{worst}。"
                + ("存在需更换支座，请尽快在工程待办中安排更换。" if needs_replace else "请按维护建议跟进处置。")
            ),
            "级别": "预警" if needs_replace else "提示",
            "已读": False,
            "创建时间": submitted_at,
        })

    # ------------------------------------------------------------ 同批次读取

    def list_batches(self, *, bridge_name: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(BATCH_TABLE)
        if bridge_name:
            rows = [row for row in rows if bridge_name in str(row.get("所属桥梁", ""))]
        return sorted(rows, key=lambda row: str(row.get("提交时间", "")), reverse=True)

    def get_batch(self, batch_no: str) -> dict[str, Any] | None:
        batch = _find_batch(batch_no)
        if batch is None:
            return None
        events = [dict(row) for row in store.rows(EVENT_TABLE) if row.get("批次号") == batch_no]
        todos = [dict(row) for row in store.rows(TODO_TABLE) if row.get("批次号") == batch_no]
        notifications = [dict(row) for row in store.rows(NOTIFICATION_TABLE) if row.get("批次号") == batch_no]
        archive = [
            dict(row)
            for row in store.rows("bridge_info")
            if row.get(LINEAGE_FIELD) == batch_no or row.get("最近支座批次") == batch_no
        ]
        result = dict(batch)
        result["检查证据"] = [event for event in events if event["事件类型"] == EVENT_EVIDENCE]
        result["支座维护结论"] = [event for event in events if event["事件类型"] == EVENT_CONCLUSION]
        result["维护建议"] = [event for event in events if event["事件类型"] == EVENT_SUGGESTION]
        result["存量迁移事件"] = [event for event in events if event["事件类型"] == EVENT_MIGRATION]
        result["桥梁档案"] = archive
        result["工程待办"] = todos
        result["通知"] = notifications
        return result

    def bridge_archive(self, bridge_name: str) -> list[dict[str, Any]]:
        """所属桥梁档案：只返回被支座批次投影过、带批次号的档案。"""
        return [
            dict(row)
            for row in store.rows("bridge_info")
            if (
                bridge_name in str(row.get("桥梁名称", ""))
                or bridge_name in str(row.get("桥梁编号", ""))
            )
            and (row.get(LINEAGE_FIELD) or row.get("最近支座批次"))
        ]

    def pending_todos(self, *, batch_no: str | None = None, only_pending: bool = True) -> list[dict[str, Any]]:
        rows = store.rows(TODO_TABLE)
        if batch_no:
            rows = [row for row in rows if row.get("批次号") == batch_no]
        if only_pending:
            rows = [row for row in rows if row.get("状态") == "待处理"]
        return [dict(row) for row in rows]

    def notifications(self, *, only_unread: bool = False) -> list[dict[str, Any]]:
        rows = store.rows(NOTIFICATION_TABLE)
        if only_unread:
            rows = [row for row in rows if not row.get("已读")]
        return sorted(
            (dict(row) for row in rows),
            key=lambda row: str(row.get("创建时间", "")),
            reverse=True,
        )

    def mark_notification_read(self, notification_id: int) -> dict[str, Any] | None:
        for row in store.rows(NOTIFICATION_TABLE):
            if int(row.get("id", 0)) == notification_id:
                row["已读"] = True
                return dict(row)
        return None
