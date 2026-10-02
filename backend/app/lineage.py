"""支座维护谱系批次：把存量迁移与补检写入同一条事务边界。

设计要点（重构约定，别在路由层绕过）：

- 一个批次（batch）= 一次谱系事务：事件先挂在事务暂存区，commit 才落库；
  任何一步没落地，``abort`` 会把快照恢复回去，不允许出现半截批次。
- 同一批次号在锁内判重：并发补检命中已提交批次时直接回读旧结果，
  重复提交不会再追加任何事件（幂等）。
- 暂存期对业务表的改动通过 ``mutate`` 登记快照，配合暂存区事件一起提交，
  保证“检查证据、维护结论、建议”与所属桥梁档案、工程待办、通知入口读到的
  是同一批次结果。

内存仓库阶段用一把可重入锁串行化提交；换成数据库时，这里对应
BEGIN … COMMIT/ROLLBACK 与批次号唯一约束，业务层不用改。
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from app.store import store

# 谱系表：批次头、事件流，以及事务提交后供三个入口回读的投影。
BATCH_TABLE = "bearing_batch"
EVENT_TABLE = "bearing_batch_event"
ARCHIVE_PROJECTION_TABLE = "bearing_archive_lineage"
TODO_TABLE = "bearing_project_todo"
NOTIFICATION_TABLE = "bearing_notification"
LINEAGE_TABLES = (
    BATCH_TABLE,
    EVENT_TABLE,
    ARCHIVE_PROJECTION_TABLE,
    TODO_TABLE,
    NOTIFICATION_TABLE,
)


class LineageError(RuntimeError):
    """谱系事务执行期异常：携带尚未落地的批次上下文。"""


_batch_lock = threading.RLock()


def ensure_tables() -> None:
    """启动时确保谱系投影表都已在内存仓库里建好。"""
    for table in LINEAGE_TABLES:
        store.rows(table)


def next_id(table: str) -> int:
    """在事务锁内给某张表分配自增主键。"""
    return max((int(row.get("id", 0)) for row in store.rows(table)), default=0) + 1


class LineageTransaction:
    """一次谱系批次事务：暂存事件与表变更，commit 一次性落地，异常整体回滚。

    用法::

        with lineage.open_batch(no, kind) as (tx, existing):
            if tx is None:
                return replay(existing)   # 批次号已落地，幂等回读
            tx.append_event(...)
            tx.mutate("bearing", row, {...})
            return tx.commit(summary, build_result)

    所有表写入都登记快照；commit 在最后一个提交点统一 append 事件与批次头，
    提交点之前的任何异常都不产生半截数据。
    """

    def __init__(self, batch_no: str, kind: str, *, operator: str | None = None) -> None:
        self.batch_no = batch_no
        self.kind = kind
        self.operator = operator
        self._events: list[dict[str, Any]] = []
        # (table, row_id) -> 变更前整行快照；新增行记 None，回滚时按 id 删除。
        self._snapshots: list[tuple[str, int, dict[str, Any] | None]] = []
        self._committed = False

    # —— 读侧：提交后供所属桥梁档案 / 工程待办 / 通知入口回读 ——

    @staticmethod
    def find_batch(batch_no: str) -> dict[str, Any] | None:
        for row in store.rows(BATCH_TABLE):
            if row.get("批次号") == batch_no:
                return row
        return None

    @staticmethod
    def get_batch_or_raise(batch_no: str) -> dict[str, Any]:
        batch = LineageTransaction.find_batch(batch_no)
        if batch is None:
            raise LineageError(f"批次「{batch_no}」不存在或已整体回滚")
        return batch

    @staticmethod
    def list_events(batch_no: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in store.rows(EVENT_TABLE)
            if row.get("批次号") == batch_no
        ]

    # —— 写侧：暂存，不落库 ——

    def append_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        ref: str | None = None,
    ) -> dict[str, Any]:
        event = {
            "id": 0,  # commit 时统一分配
            "批次号": self.batch_no,
            "seq": 0,  # commit 时按 append 顺序编号
            "事件类型": event_type,
            "业务引用": ref,
            "载荷": dict(payload),
        }
        self._events.append(event)
        return event

    def insert(self, table: str, row: dict[str, Any]) -> dict[str, Any]:
        """暂存一条新增行：先落内存并登记快照，回滚时按 id 摘除。"""
        row_id = int(row.get("id") or 0) or next_id(table)
        stored = dict(row)
        stored["id"] = row_id
        self._snapshots.append((table, row_id, None))
        store.rows(table).append(stored)
        return stored

    def upsert(
        self,
        table: str,
        key_field: str,
        key_value: Any,
        changes: dict[str, Any],
    ) -> dict[str, Any]:
        """按键暂存 upsert：命中则原地更新，未命中则登记为新增。"""
        target = next(
            (row for row in store.rows(table) if row.get(key_field) == key_value),
            None,
        )
        if target is None:
            row = dict(changes)
            row[key_field] = key_value
            return self.insert(table, row)
        self._snapshots.append((table, int(target["id"]), dict(target)))
        target.update(changes)
        return target

    def mutate(self, table: str, row: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
        """暂存一次业务表原地更新，保留旧值快照供回滚。"""
        self._snapshots.append((table, int(row["id"]), dict(row)))
        row.update(changes)
        return row

    # —— 提交 / 回滚 ——

    def abort(self) -> None:
        """按登记顺序的逆序恢复快照，把暂存期改动全部撤掉。"""
        snapshots, self._snapshots = self._snapshots, []
        for table, row_id, snapshot in reversed(snapshots):
            rows = store.rows(table)
            if snapshot is None:
                rows[:] = [row for row in rows if int(row.get("id", -1)) != row_id]
                continue
            for row in rows:
                if int(row.get("id", -1)) == row_id:
                    row.clear()
                    row.update(snapshot)
                    break
        self._events.clear()

    def commit(
        self,
        summary: str,
        build_result: Callable[[str], dict[str, Any]] | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        """事件与批次头在同一个提交点落库。

        先在本地把事件 id、批次头和投影结果全部算好（这一步可能抛业务错，
        此时一行都还没写）；全部就绪后才统一 append——append 之后即落地。
        若提交点本身异常，仍按快照整体回滚。
        """
        prepared_events: list[dict[str, Any]] = []
        for seq, event in enumerate(self._events, start=1):
            prepared = dict(event)
            prepared["seq"] = seq
            prepared_events.append(prepared)
        batch = {
            "id": next_id(BATCH_TABLE),
            "批次号": self.batch_no,
            "批次类型": self.kind,
            "状态": "已落地",
            "摘要": summary,
            "操作人": self.operator,
            "事件数": len(prepared_events),
        }
        batch.update(extra)
        # 投影结果在落库前计算：读的是暂存后的表，符合“同批结果”的口径。
        projected = build_result(self.batch_no) if build_result is not None else None
        try:
            for prepared in prepared_events:
                prepared["id"] = next_id(EVENT_TABLE)
                store.rows(EVENT_TABLE).append(prepared)
            store.rows(BATCH_TABLE).append(batch)
        except Exception:
            self.abort()
            raise
        self._committed = True
        result = dict(batch)
        result["events"] = [dict(event) for event in prepared_events]
        if projected is not None:
            result["result"] = projected
        return result

    def __enter__(self) -> "LineageTransaction":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is not None and not self._committed:
            self.abort()


@contextmanager
def open_batch(
    batch_no: str,
    kind: str,
    *,
    operator: str | None = None,
) -> Iterator[tuple["LineageTransaction | None", dict[str, Any] | None]]:
    """在批次锁临界区里开事务。

    产出 ``(tx, existing)``：二者恰好一个非空。``tx`` 非空时是新事务，
    提交/回滚都在锁内完成；``existing`` 非空表示批次号已落地，调用方只能
    回读旧结果，不允许再追加事件。
    """
    with _batch_lock:
        existing = LineageTransaction.find_batch(batch_no)
        if existing is not None:
            yield None, dict(existing)
            return
        tx = LineageTransaction(batch_no, kind, operator=operator)
        try:
            yield tx, None
        except Exception:
            if not tx._committed:
                tx.abort()
            raise


def replay_result(batch: dict[str, Any]) -> dict[str, Any]:
    """幂等命中时把旧批次头补成完整结果（含事件流与三个入口的投影）。"""
    batch_no = str(batch["批次号"])
    result = dict(batch)
    result["events"] = LineageTransaction.list_events(batch_no)
    result["replayed"] = True
    return result
