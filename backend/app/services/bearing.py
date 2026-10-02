"""支座维护业务规则。

重构后所有写入都收敛到“谱系批次（lineage batch）”这一条事务边界：

1. 存量迁移：服务首次使用时按支座编号把旧表支座逐条回填进谱系，
   整批要么全部落地、要么整体回滚，只做一次（``MIGRATION_BATCH_NO`` 幂等）。
2. 补检提交：检查证据、支座维护结论、处置建议必须写在调用方给定的
   同一批次号下；事务未落地前任何异常都会把已暂存的改动撤回。
3. 三个读入口（所属桥梁档案、工程待办、通知入口）都只读批次提交后的
   投影，保证看到的是同一批次结果。
4. 并发补检按批次号幂等：批次号已落地时直接回读旧结果，
   重复提交不会再追加事件。

状态流转（防锈处理 / 纠偏复位 / 安排更换）仍保留，单条动作自动落一个
小型谱系批次，保证支座状态与事件流始终一致。
"""
from __future__ import annotations

import threading
from typing import Any

from app import lineage
from app.store import store

MODULE = "bearing"
REQUIRED_FIELDS = ["支座编号", "所属桥梁", "支座类型"]
STATUS_ORDER = ["正常", "锈蚀", "偏位", "需更换"]
ACTION_RULES = {"防锈处理": "正常", "纠偏复位": "正常", "安排更换": "需更换"}
# 结论 -> 对应维护动作，证据结论异常时驱动状态流转与待办生成。
CONCLUSION_ACTIONS = {"锈蚀": "防锈处理", "偏位": "纠偏复位", "需更换": "安排更换"}
NEGATIVE_ACTIONS: list[str] = []

MIGRATION_BATCH_NO = "MIG-BEARING-STOCK"
MIGRATION_KIND = "存量迁移"
INSPECTION_KIND = "支座补检"
ACTION_KIND = "状态流转"

_migration_lock = threading.Lock()


class BearingService:
    def __init__(self) -> None:
        self._migrated = False

    # ------------------------------------------------------------------
    # 存量迁移：按支座编号回填，整批一个事务，只做一次
    # ------------------------------------------------------------------

    def ensure_migrated(self) -> dict[str, Any] | None:
        """启动/调用时惰性迁移；已迁移或已落地过迁移批次时直接跳过。"""
        if self._migrated:
            return None
        with _migration_lock:
            if self._migrated:
                return None
            existing = lineage.LineageTransaction.find_batch(MIGRATION_BATCH_NO)
            if existing is not None:
                self._migrated = True
                return None
            result = self._migrate_stock()
            self._migrated = True
            return result

    def _migrate_stock(self) -> dict[str, Any]:
        rows = store.rows(MODULE)
        # 按支座编号稳定排序回填，保证同一份存量每次迁移出的事件顺序一致。
        ordered = sorted(rows, key=lambda row: str(row.get("支座编号", "")))
        with lineage.open_batch(MIGRATION_BATCH_NO, MIGRATION_KIND) as (tx, existing):
            if existing is not None:
                return self._replay(existing)

            for row in ordered:
                bearing_no = str(row.get("支座编号", "")).strip()
                tx.append_event(
                    "存量支座回填",
                    {"支座编号": bearing_no, "所属桥梁": row.get("所属桥梁"), "支座类型": row.get("支座类型")},
                    ref=bearing_no,
                )
                tx.mutate(
                    MODULE,
                    row,
                    {"迁移批次号": MIGRATION_BATCH_NO, "当前批次号": MIGRATION_BATCH_NO},
                )
                self._touch_archive(tx, str(row.get("所属桥梁", "")), MIGRATION_BATCH_NO)

            return tx.commit(
                f"按支座编号回填存量支座 {len(ordered)} 条",
                self._build_result,
                迁移数量=len(ordered),
            )

    # ------------------------------------------------------------------
    # 读侧
    # ------------------------------------------------------------------

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        bridge: str | None = None,
        batch_no: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        self.ensure_migrated()
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("支座编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if bridge:
            rows = [row for row in rows if bridge in str(row.get("所属桥梁", ""))]
        if batch_no:
            rows = [row for row in rows if row.get("当前批次号") == batch_no]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [dict(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        self.ensure_migrated()
        row = store.find(MODULE, entry_id)
        return dict(row) if row else None

    def find_by_no(self, bearing_no: str) -> dict[str, Any] | None:
        for row in store.rows(MODULE):
            if str(row.get("支座编号", "")) == bearing_no:
                return row
        return None

    # —— 三个统一入口：只读已落地批次的投影 ——

    def archive_lineage(self, bridge: str | None = None) -> list[dict[str, Any]]:
        """所属桥梁档案读取的谱系：桥梁 -> 最近批次与批次内支座结论。"""
        self.ensure_migrated()
        rows = store.rows(lineage.ARCHIVE_PROJECTION_TABLE)
        if bridge:
            rows = [row for row in rows if bridge in str(row.get("所属桥梁", ""))]
        return [dict(row) for row in rows]

    def project_todos(self, status_filter: str | None = None) -> list[dict[str, Any]]:
        """工程待办入口：只列批次提交后生成的待办。"""
        self.ensure_migrated()
        rows = store.rows(lineage.TODO_TABLE)
        if status_filter:
            rows = [row for row in rows if row.get("状态") == status_filter]
        return [dict(row) for row in rows]

    def notifications(self, only_unread: bool = False) -> list[dict[str, Any]]:
        """通知入口：批次落地/回滚结论。"""
        self.ensure_migrated()
        rows = store.rows(lineage.NOTIFICATION_TABLE)
        if only_unread:
            rows = [row for row in rows if not row.get("已读")]
        return [dict(row) for row in rows]

    def get_batch(self, batch_no: str) -> dict[str, Any]:
        """按批次号回读完整结果（事件流 + 三入口投影）。"""
        batch = lineage.LineageTransaction.get_batch_or_raise(batch_no)
        result = lineage.replay_result(batch)
        result["result"] = self._build_result(batch_no)
        return result

    def list_batches(self) -> list[dict[str, Any]]:
        self.ensure_migrated()
        return [dict(row) for row in store.rows(lineage.BATCH_TABLE)]

    def _replay(self, batch: dict[str, Any]) -> dict[str, Any]:
        """命中已落地批次：补全事件流与三个入口投影，供幂等原样回读。"""
        result = lineage.replay_result(batch)
        result["result"] = self._build_result(str(batch["批次号"]))
        return result

    # ------------------------------------------------------------------
    # 写侧：登记 / 补检 / 动作，全部走谱系事务
    # ------------------------------------------------------------------

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        self.ensure_migrated()
        bearing_no = str(values["支座编号"]).strip()
        if self.find_by_no(bearing_no) is not None:
            return None, [f"支座编号「{bearing_no}」已存在，不能重复登记"]
        batch_no = f"REG-{bearing_no}"
        with lineage.open_batch(batch_no, "支座登记", operator=str(values.get("操作人") or "")) as (tx, existing):
            if existing is not None:
                replayed = self._replay(existing)
                return replayed["result"]["bearings"][0], []
            entry = tx.insert(MODULE, {
                "支座编号": bearing_no,
                "所属桥梁": values.get("所属桥梁"),
                "支座类型": values.get("支座类型"),
                "status": STATUS_ORDER[0],
                "pending": True,
                "abnormal": False,
                "迁移批次号": None,
                "当前批次号": batch_no,
            })
            tx.append_event("支座登记", {"支座编号": bearing_no}, ref=bearing_no)
            self._touch_archive(tx, str(values.get("所属桥梁", "")), batch_no)
            self._notify(tx, batch_no, f"支座 {bearing_no} 已登记建档", level="信息")
            result = tx.commit(f"登记支座 {bearing_no}", self._build_result)
            return result["result"]["bearings"][0], []

    def submit_inspection(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """并发补检入口：按批次号幂等，证据/结论/建议同批落地，失败整体回滚。"""
        self.ensure_migrated()
        batch_no = str(values.get("批次号") or "").strip()
        bearing_no = str(values.get("支座编号") or "").strip()
        if not batch_no:
            return None, "补检必须携带批次号，重复提交按批次号去重"
        if not bearing_no:
            return None, "缺少支座编号，无法定位被检支座"

        evidence = str(values.get("检查证据") or "").strip()
        if not evidence:
            return None, "检查证据不能为空：照片、记录或检查人至少补齐一项"
        conclusion = str(values.get("维护结论") or "").strip()
        if not conclusion:
            return None, "维护结论不能为空：正常、锈蚀、偏位、需更换四选一"
        if conclusion not in STATUS_ORDER:
            return None, f"维护结论「{conclusion}」不在允许口径：{'、'.join(STATUS_ORDER)}"
        recommendation = str(values.get("处置建议") or "").strip()
        if not recommendation:
            return None, "处置建议不能为空，需与维护结论写入同一批次"
        inspector = str(values.get("检查人") or "").strip()
        if inspector:
            evidence = f"{evidence}（检查人：{inspector}）"

        # 业务对象存在性在事务外先判一次，事务内靠 upsert 兜底并发。
        entry = self.find_by_no(bearing_no)
        if entry is None:
            return None, f"支座 {bearing_no} 不存在或已归档，无法补检"

        with lineage.open_batch(batch_no, INSPECTION_KIND, operator=inspector or None) as (tx, existing):
            if existing is not None:
                # 幂等回读：重复提交原样返回，绝不追加事件。
                return self._replay(existing), "批次已落地，按批次号幂等回读，未重复追加事件"

            entry = self.find_by_no(bearing_no)  # 锁内重读，防止窗口期变化
            if entry is None:
                raise lineage.LineageError(f"支座 {bearing_no} 在事务内消失，触发整体回滚")

            bridge = str(entry.get("所属桥梁", ""))
            tx.append_event(
                "检查证据",
                {"支座编号": bearing_no, "证据": evidence},
                ref=bearing_no,
            )
            tx.append_event(
                "维护结论",
                {"支座编号": bearing_no, "结论": conclusion},
                ref=bearing_no,
            )
            tx.append_event(
                "处置建议",
                {"支座编号": bearing_no, "建议": recommendation},
                ref=bearing_no,
            )

            action = CONCLUSION_ACTIONS.get(conclusion)
            changes: dict[str, Any] = {
                "status": conclusion,
                "pending": conclusion != STATUS_ORDER[-1],
                "abnormal": conclusion != STATUS_ORDER[0],
                "最近检查": str(values.get("检查日期") or ""),
                "锈蚀程度": conclusion if conclusion == "锈蚀" else entry.get("锈蚀程度"),
                "当前批次号": batch_no,
            }
            tx.mutate(MODULE, entry, changes)
            tx.append_event(
                "支座状态更新",
                {"支座编号": bearing_no, "动作": action or "保持评定", "目标状态": conclusion},
                ref=bearing_no,
            )

            self._touch_archive(tx, bridge, batch_no, bearing_no=bearing_no, conclusion=conclusion)
            if action is not None:
                self._add_todo(
                    tx,
                    batch_no,
                    bridge=bridge,
                    bearing_no=bearing_no,
                    action=action,
                    recommendation=recommendation,
                )
            self._notify(
                tx,
                batch_no,
                f"支座 {bearing_no} 补检结论「{conclusion}」已随批次落地",
                level="预警" if action else "信息",
            )

            result = tx.commit(
                f"支座 {bearing_no} 补检：{conclusion}",
                self._build_result,
                支座编号=bearing_no,
                所属桥梁=bridge,
            )
            return result, "检查证据、维护结论与处置建议已写入同一谱系批次"

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        self.ensure_migrated()
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"桥梁支座 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于支座维护可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"

        bearing_no = str(entry.get("支座编号", ""))
        batch_no = f"ACT-{bearing_no}-{action}"
        with lineage.open_batch(batch_no, ACTION_KIND) as (tx, existing):
            if existing is not None:
                return self._replay(existing), "该动作已提交过，按批次号幂等回读"
            entry = store.find(MODULE, entry_id)
            tx.append_event(
                "维护动作",
                {"支座编号": bearing_no, "动作": action, "目标状态": target},
                ref=bearing_no,
            )
            tx.mutate(
                MODULE,
                entry,
                {
                    "status": target,
                    "pending": target != STATUS_ORDER[-1],
                    "abnormal": action in NEGATIVE_ACTIONS,
                    "当前批次号": batch_no,
                },
            )
            bridge = str(entry.get("所属桥梁", ""))
            self._touch_archive(tx, bridge, batch_no)
            self._notify(tx, batch_no, f"支座 {bearing_no} 已{action}", level="信息")
            result = tx.commit(f"支座 {bearing_no} 已{action}", self._build_result)
            return result, f"桥梁支座已{action}"

    # ------------------------------------------------------------------
    # 投影写入：三个入口共享同一批次提交结果
    # ------------------------------------------------------------------

    def _touch_archive(
        self,
        tx: lineage.LineageTransaction,
        bridge: str,
        batch_no: str,
        *,
        bearing_no: str | None = None,
        conclusion: str | None = None,
    ) -> None:
        """刷新“所属桥梁档案”的谱系投影：同一桥梁只保留一行，指向最新批次。"""
        if not bridge:
            return
        findings: dict[str, str] = {}
        if bearing_no and conclusion:
            findings[bearing_no] = conclusion

        table = lineage.ARCHIVE_PROJECTION_TABLE
        existing = next(
            (row for row in store.rows(table) if row.get("所属桥梁") == bridge),
            None,
        )
        history = list((existing or {}).get("批次历史") or [])
        if batch_no not in history:
            history.append(batch_no)
        merged = dict((existing or {}).get("结论汇总") or {})
        merged.update(findings)
        changes = {
            "最近批次号": batch_no,
            "本批结论": findings,
            "更新次数": int((existing or {}).get("更新次数", 0)) + 1,
            "批次历史": history,
            "结论汇总": merged,
        }
        tx.upsert(table, "所属桥梁", bridge, changes)

    def _add_todo(
        self,
        tx: lineage.LineageTransaction,
        batch_no: str,
        *,
        bridge: str,
        bearing_no: str,
        action: str,
        recommendation: str,
    ) -> None:
        tx.insert(lineage.TODO_TABLE, {
            "批次号": batch_no,
            "所属桥梁": bridge,
            "支座编号": bearing_no,
            "待办动作": action,
            "处置建议": recommendation,
            "状态": "待安排",
        })

    def _notify(
        self,
        tx: lineage.LineageTransaction,
        batch_no: str,
        message: str,
        *,
        level: str,
    ) -> None:
        tx.insert(lineage.NOTIFICATION_TABLE, {
            "批次号": batch_no,
            "级别": level,
            "内容": message,
            "已读": False,
        })

    # ------------------------------------------------------------------
    # 批次结果：三个入口的读取都由这份结果派生
    # ------------------------------------------------------------------

    def _build_result(self, batch_no: str) -> dict[str, Any]:
        return {
            "batch_no": batch_no,
            "bearings": [
                dict(row)
                for row in store.rows(MODULE)
                if row.get("当前批次号") == batch_no
            ],
            "archive": [
                dict(row)
                for row in store.rows(lineage.ARCHIVE_PROJECTION_TABLE)
                if batch_no in (row.get("批次历史") or [])
            ],
            "todos": [
                dict(row)
                for row in store.rows(lineage.TODO_TABLE)
                if row.get("批次号") == batch_no
            ],
            "notifications": [
                dict(row)
                for row in store.rows(lineage.NOTIFICATION_TABLE)
                if row.get("批次号") == batch_no
            ],
        }
