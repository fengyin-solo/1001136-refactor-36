"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

支座维护重构后，仓库额外承担「事务边界」的职责：
- ``transaction`` 让业务在工作副本上写入，只有正常退出才发布；异常或主动
  放弃时整批回滚，外部始终读到已提交的数据；
- ``batch_lock`` 是谱系批次号的串行闸门，并发补检靠它做「判重 + 落库」的
  原子幂等，重复提交命中既有批次，不会追加事件。
"""
from __future__ import annotations

import copy
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from app.seed import SEED_ROWS


class TransactionAborted(Exception):
    """业务在事务内主动声明失败，触发整批回滚。"""


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        # 批次号是并发补检的幂等键，所有「判重 + 落库」必须在同一把锁内完成；
        # 事务发布也走这把锁，避免两份工作副本同时提交时互相覆盖。
        self.batch_lock = threading.RLock()
        # 每个线程一份未提交工作副本；None 表示当前线程不在事务里。
        self._local = threading.local()

    def module_names(self) -> list[str]:
        return sorted(self._active_tables())

    def _active_tables(self) -> dict[str, list[dict[str, Any]]]:
        working = getattr(self._local, "working", None)
        return working if working is not None else self._tables

    def rows(self, module: str) -> list[dict[str, Any]]:
        tables = self._active_tables()
        return tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def next_id(self, module: str) -> int:
        return max((int(row.get("id", 0)) for row in self.rows(module)), default=0) + 1

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """在已提交数据的深拷贝工作副本上写入：正常退出才发布，异常整体回滚。

        - 隔离：事务期间其他线程（以及本线程之外的读请求）只能读到上一个
          已提交版本，看不到半成品；
        - 原子：上下文内抛出任何异常（含 ``TransactionAborted``）都会丢弃
          工作副本，谱系批次、事件、待办、通知要么全部落地，要么全部不留；
        - 串行：事务持有 ``batch_lock``，配合服务层先查批次号再落库的写法，
          保证同一批次号的并发补检只有第一次会真正写入。
        """
        if getattr(self._local, "working", None) is not None:
            raise RuntimeError("当前线程已有未结束的事务，不支持嵌套")
        with self.batch_lock:
            self._local.working = copy.deepcopy(self._tables)
            try:
                yield
            except Exception:
                self._local.working = None
                raise
            committed = self._local.working
            self._local.working = None
            # 发布前再拷贝一次，避免事务外仍持有工作副本引用时污染已提交数据。
            self._tables = copy.deepcopy(committed)

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
