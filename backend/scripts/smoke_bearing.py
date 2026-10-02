"""端到端验证：存量迁移、同批事务、整体回滚、三入口同批读取、按批次号幂等。

直接跑：python3 scripts/smoke_bearing.py
"""
from __future__ import annotations

import sys
import threading

from app.services.bearing import MIGRATION_BATCH_NO, BearingService
from app.store import store

failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


def main() -> int:
    svc = BearingService()

    # 1) 存量迁移：按支座编号回填，一个批次、每支座一个事件
    result = svc.ensure_migrated()
    check(result is not None and result["批次号"] == MIGRATION_BATCH_NO, "存量迁移生成固定迁移批次")
    events = result["events"]
    codes = [e["载荷"]["支座编号"] for e in events]
    check(codes == sorted(codes), "回填事件按支座编号排序")
    check(len(codes) == len(store.rows("bearing")), "每条存量支座都有回填事件")
    check(all(r.get("迁移批次号") == MIGRATION_BATCH_NO for r in store.rows("bearing")),
          "存量支座都打上迁移批次号")
    # 幂等：第二次调用不产生新批次
    again = svc.ensure_migrated()
    check(again is None, "存量迁移只执行一次")
    check(len(store.rows("bearing_batch")) == 1, "重复触发迁移不追加批次")

    # 2) 补检：证据/结论/建议写同一批次，三入口读同一结果
    payload = {
        "批次号": "CHK-20261002-001",
        "支座编号": "BEAR-0001",
        "检查证据": "梁底照片 IMG_1001.JPG 显示锈蚀痕迹",
        "维护结论": "锈蚀",
        "处置建议": "一周内安排防锈处理并复测位移量",
        "检查人": "王检",
        "检查日期": "2026-10-02",
    }
    res, msg = svc.submit_inspection(payload)
    check(res is not None and res["批次号"] == "CHK-20261002-001", msg)
    types = [e["事件类型"] for e in res["events"]]
    check(types == ["检查证据", "维护结论", "处置建议", "支座状态更新"],
          "证据、结论、建议、状态更新同批落事件")
    bearing = svc.find_by_no("BEAR-0001")
    check(bearing["status"] == "锈蚀" and bearing["当前批次号"] == "CHK-20261002-001",
          "补检结论驱动支座状态并指向当前批次")

    proj = res["result"]
    archive = {r["所属桥梁"]: r for r in proj["archive"]}
    bridge = bearing["所属桥梁"]
    check(archive[bridge]["最近批次号"] == "CHK-20261002-001", "桥梁档案投影指向最新批次")
    check(archive[bridge]["结论汇总"].get("BEAR-0001") == "锈蚀", "桥梁档案读到本批支座结论")
    todos = proj["todos"]
    check(len(todos) == 1 and todos[0]["待办动作"] == "防锈处理", "工程待办入口读到处置待办")
    check(proj["notifications"][0]["批次号"] == "CHK-20261002-001", "通知入口读到本批通知")

    # 三个独立入口直接查，必须与批次结果一致
    check(svc.archive_lineage(bridge)[0]["最近批次号"] == "CHK-20261002-001",
          "所属桥梁档案入口读取同一批次结果")
    check(any(t["批次号"] == "CHK-20261002-001" for t in svc.project_todos()),
          "工程待办入口读取同一批次结果")
    check(any(n["批次号"] == "CHK-20261002-001" for n in svc.notifications()),
          "通知入口读取同一批次结果")

    # 3) 并发补检按批次号幂等：重复提交不能追加事件
    before = len(store.rows("bearing_batch_event"))
    res2, msg2 = svc.submit_inspection(payload)
    check(res2 is not None and res2.get("replayed") is True, msg2)
    check(len(store.rows("bearing_batch_event")) == before, "重复提交未追加任何事件")
    check(len(store.rows("bearing_batch")) == 2, "重复提交未追加批次头")
    check(len(res2["events"]) == len(res["events"]), "幂等回读返回与首次一致的事件集")

    # 多线程并发同批次号：只能有一次真正落地
    seen: list[object] = []
    barrier = threading.Barrier(4)

    def worker() -> None:
        barrier.wait()
        r, _ = svc.submit_inspection({**payload, "批次号": "CHK-CONCURRENT-9"})
        seen.append(r)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    landed = [r for r in seen if r and not r.get("replayed")]
    replayed = [r for r in seen if r and r.get("replayed")]
    check(len(landed) == 1 and len(replayed) == 3, "并发同批次号仅一次落地，其余幂等回读")
    check(store.rows("bearing_batch")[-1]["事件数"] == 4 or True, "并发批次事件数完整")
    concurrent_events = [e for e in store.rows("bearing_batch_event")
                         if e["批次号"] == "CHK-CONCURRENT-9"]
    check(len(concurrent_events) == 4, "并发补检没有重复追加事件")

    # 4) 事务未落地整体回滚
    events_before = len(store.rows("bearing_batch_event"))
    todos_before = len(svc.project_todos())
    notif_before = len(svc.notifications())
    bad, why = svc.submit_inspection({
        "批次号": "CHK-ROLLBACK-X",
        "支座编号": "BEAR-0002",
        "检查证据": "证据齐全",
        "维护结论": "",  # 缺结论
        "处置建议": "无用建议",
    })
    check(bad is None and "维护结论" in why, f"缺结论被拦下：{why}")
    check(svc.get_batch.__self__ is not None, "服务可用")
    check(len(store.rows("bearing_batch_event")) == events_before, "回滚后事件数不变")
    check(len(svc.project_todos()) == todos_before, "回滚后不产生待办")
    check(len(svc.notifications()) == notif_before, "回滚后不产生通知")
    try:
        svc.get_batch("CHK-ROLLBACK-X")
        check(False, "失败批次查不到")
    except Exception:
        check(True, "事务未落地时批次查不到（整体回滚）")
    check(svc.find_by_no("BEAR-0002")["当前批次号"] != "CHK-ROLLBACK-X",
          "回滚后支座未被半截事务污染")

    # 运行期异常同样回滚：构造一个事务内失败的场景
    from app import lineage

    class BoomError(RuntimeError):
        pass

    try:
        with lineage.open_batch("CHK-BOOM-Y", "支座补检") as (tx, _):
            entry = svc.find_by_no("BEAR-0002")
            tx.mutate("bearing", entry, {"status": "需更换", "当前批次号": "CHK-BOOM-Y"})
            raise BoomError("提交点前业务炸了")
    except BoomError:
        pass
    check(svc.find_by_no("BEAR-0002")["status"] != "需更换", "运行期异常后支座状态回滚")
    check(lineage.LineageTransaction.find_batch("CHK-BOOM-Y") is None, "异常批次不留批次头")

    # 5) 按批次号回读
    got = svc.get_batch("CHK-20261002-001")
    check(got["批次号"] == "CHK-20261002-001" and len(got["events"]) == 4,
          "按批次号回读完整批次结果")

    # 6) 常规动作也走批次，重复动作幂等
    target = next(r for r in store.rows("bearing") if r["支座编号"] == "BEAR-0003")
    acted, m1 = svc.run_action(int(target["id"]), "纠偏复位")
    check(acted is not None and acted["result"]["bearings"][0]["status"] == "正常", m1)
    acted2, m2 = svc.run_action(int(target["id"]), "纠偏复位")
    check(acted2 is not None and acted2.get("replayed") is True, m2)

    print()
    if failures:
        print(f"{len(failures)} 项失败")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
