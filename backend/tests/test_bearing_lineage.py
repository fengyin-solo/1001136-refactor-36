"""支座维护重构测试：存量迁移、事务回滚、同批次读取与并发幂等。"""
from __future__ import annotations

import importlib
import threading

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def service():
    # store 是进程级单例，每个用例前重载，保证互不污染。
    import app.store as store_module
    importlib.reload(store_module)
    import app.services.bearing as bearing_module
    importlib.reload(bearing_module)
    yield bearing_module.BearingService()


def _payload(batch_no: str = "INSP-20261002-0100", bridge: str = "桥梁档案样例1") -> dict:
    return {
        "批次号": batch_no,
        "所属桥梁": bridge,
        "检查明细": [
            {"支座编号": "BEAR-0001", "检查证据": "表面完好，无明显病害", "检查结论": "正常", "检查人": "张三"},
            {"支座编号": "BEAR-0007", "检查证据": "底板锈蚀严重，有掉渣", "检查结论": "锈蚀", "检查人": "张三"},
        ],
    }


# ---------------------------------------------------------------- 存量迁移

def test_migration_backfills_by_bearing_no_and_is_idempotent(service):
    first = service.migrate_legacy_bearings()
    assert first["迁移数量"] == 3
    assert first["跳过数量"] == 0
    # 每个支座按支座编号拿到自己的迁移批次与事件
    from app.store import store
    for row in store.rows("bearing"):
        assert row["谱系批次号"].startswith("MIG-")
        assert row.get("迁移回填时间")
    assert len(store.rows("bearing_batch")) == 3
    assert len(store.rows("bearing_batch_event")) == 3

    second = service.migrate_legacy_bearings()
    assert second["迁移数量"] == 0
    assert second["跳过数量"] == 3
    # 幂等：重复执行不追加事件
    assert len(store.rows("bearing_batch_event")) == 3


# ---------------------------------------------------------------- 事务提交

def test_submit_writes_three_event_kinds_in_one_batch_and_projects(service):
    service.migrate_legacy_bearings()
    batch, message, duplicated = service.submit_inspection(_payload())

    assert not duplicated
    assert batch["批次号"] == "INSP-20261002-0100"
    assert batch["维护结论"] == "锈蚀"  # 取最重结论
    assert batch["事件数量"] == 6       # 2 个支座 × 证据/结论/建议

    detail = service.get_batch("INSP-20261002-0100")
    assert len(detail["检查证据"]) == 2
    assert len(detail["支座维护结论"]) == 2
    assert len(detail["维护建议"]) == 2
    # 桥梁档案、工程待办、通知都挂同一批次号，读同一份结果
    assert [a["最近支座批次"] for a in detail["桥梁档案"]] == ["INSP-20261002-0100"]
    assert {t["批次号"] for t in detail["工程待办"]} == {"INSP-20261002-0100"}
    assert {n["批次号"] for n in detail["通知"]} == {"INSP-20261002-0100"}

    # 正常结论不产生待办；锈蚀结论产生 1 条中优先级待办
    assert len(detail["工程待办"]) == 1
    assert detail["工程待办"][0]["支座编号"] == "BEAR-0007"
    assert detail["通知"][0]["级别"] == "提示"


def test_replace_conclusion_creates_high_priority_todo_and_alert(service):
    service.migrate_legacy_bearings()
    batch, _, _ = service.submit_inspection({
        "批次号": "INSP-20261002-0101",
        "所属桥梁": "桥梁档案样例2",
        "检查明细": [
            {"支座编号": "BEAR-0002", "检查证据": "支座压溃失效", "检查结论": "需更换"},
        ],
    })
    detail = service.get_batch(batch["批次号"])
    assert detail["工程待办"][0]["优先级"] == "高"
    assert detail["通知"][0]["级别"] == "预警"
    assert "更换" in detail["通知"][0]["标题"]


# ---------------------------------------------------------------- 回滚边界

def test_failure_after_events_rolls_back_everything(service):
    service.migrate_legacy_bearings()
    from app.store import store
    events_before = len(store.rows("bearing_batch_event"))
    batches_before = len(store.rows("bearing_batch"))

    with pytest.raises(Exception):
        service.submit_inspection({**_payload("INSP-ROLL-0001"), "模拟失败": True})

    assert service.get_batch("INSP-ROLL-0001") is None
    assert len(store.rows("bearing_batch_event")) == events_before
    assert len(store.rows("bearing_batch")) == batches_before
    assert len(store.rows("bearing_pending_todo")) == 0
    assert len(store.rows("bearing_notification")) == 0
    # 事件里改过的支座状态也随事务回滚
    row = next(r for r in store.rows("bearing") if r["支座编号"] == "BEAR-0001")
    assert row["status"] == "正常"
    assert row.get("谱系批次号", "").startswith("MIG-")


def test_missing_bridge_archive_rolls_back_batch(service):
    service.migrate_legacy_bearings()
    with pytest.raises(Exception):
        service.submit_inspection({
            "批次号": "INSP-ROLL-0002",
            "所属桥梁": "未建档桥梁",
            "检查明细": [{"支座编号": "BEAR-X", "检查证据": "证据", "检查结论": "正常"}],
        })
    assert service.get_batch("INSP-ROLL-0002") is None
    from app.store import store
    assert not [r for r in store.rows("bearing") if r["支座编号"] == "BEAR-X"]


def test_invalid_conclusion_rolls_back(service):
    service.migrate_legacy_bearings()
    with pytest.raises(Exception):
        service.submit_inspection({
            "所属桥梁": "桥梁档案样例1",
            "检查明细": [{"支座编号": "BEAR-0001", "检查证据": "证据", "检查结论": "报废"}],
        })
    assert service.list_batches() == [b for b in service.list_batches() if b["批次类型"] != "补检"]


# ---------------------------------------------------------------- 幂等并发

def test_duplicate_batch_no_is_idempotent(service):
    service.migrate_legacy_bearings()
    payload = _payload()
    first, _, dup1 = service.submit_inspection(payload)
    second, _, dup2 = service.submit_inspection(payload)
    assert dup1 is False and dup2 is True
    assert first["批次号"] == second["批次号"]
    from app.store import store
    # 重复提交没有追加事件、待办、通知
    assert len(store.rows("bearing_batch_event")) == 3 + 6
    assert len(store.rows("bearing_pending_todo")) == 1
    assert len(store.rows("bearing_notification")) == 1


def test_concurrent_same_batch_no_writes_once(service):
    service.migrate_legacy_bearings()
    results: list[tuple[str, bool]] = []
    errors: list[Exception] = []
    start = threading.Barrier(8)

    def worker() -> None:
        start.wait()
        try:
            batch, _, duplicated = service.submit_inspection(_payload("INSP-CONC-0001"))
            results.append((batch["批次号"], duplicated))
        except Exception as exc:  # 并发下不允许有异常逃逸
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(results) == 8
    assert {batch_no for batch_no, _ in results} == {"INSP-CONC-0001"}
    duplicated_flags = sorted(dup for _, dup in results)
    assert duplicated_flags == [False] + [True] * 7
    from app.store import store
    # 只有第一次提交写入 6 个事件和 1 条通知
    assert [e["批次号"] for e in store.rows("bearing_batch_event")].count("INSP-CONC-0001") == 6
    assert [n["批次号"] for n in store.rows("bearing_notification")].count("INSP-CONC-0001") == 1
    assert [t["批次号"] for t in store.rows("bearing_pending_todo")].count("INSP-CONC-0001") == 1


# ---------------------------------------------------------------- HTTP 层

def test_http_flow_migration_inspection_batch_reads_and_rollback():
    # 用全新的 app 实例，触发 lifespan 里的启动迁移
    import app.store as store_module
    importlib.reload(store_module)
    import app.services.bearing as bearing_module
    importlib.reload(bearing_module)
    import app.main as main_module
    importlib.reload(main_module)

    with TestClient(main_module.app) as client:
        # 启动迁移已经回填
        resp = client.get("/api/bearing")
        assert resp.status_code == 200
        assert all(row.get("谱系批次号", "").startswith("MIG-") for row in resp.json()["items"])

        # 显式再迁一次：全部跳过
        resp = client.post("/api/bearing/migration")
        assert resp.json()["entry"]["迁移数量"] == 0
        assert resp.json()["entry"]["跳过数量"] == 3

        # 提交补检批次
        resp = client.post("/api/bearing/inspections", json=_payload())
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        batch_no = body["entry"]["批次号"]

        # 重复提交：幂等，不追加事件
        resp_dup = client.post("/api/bearing/inspections", json=_payload())
        assert "重复提交已忽略" in resp_dup.json()["message"]

        # 三个入口读到同一批次结果
        detail = client.get(f"/api/bearing/batches/{batch_no}").json()
        assert len(detail["检查证据"]) == 2
        archive = client.get("/api/bearing/bridge-archive", params={"bridge": "桥梁档案样例1"}).json()
        todos = client.get("/api/bearing/todos").json()
        notifications = client.get("/api/bearing/notifications").json()
        assert archive["items"][0]["最近支座批次"] == batch_no
        assert todos["items"][0]["批次号"] == batch_no
        assert notifications["items"][0]["批次号"] == batch_no

        # 失败批次：409 + 整体回滚，读侧查不到
        resp = client.post("/api/bearing/inspections", json={**_payload("INSP-HTTP-FAIL"), "模拟失败": True})
        assert resp.status_code == 409
        assert client.get(f"/api/bearing/batches/INSP-HTTP-FAIL").status_code == 404
        # 回滚后事件总数没有增加
        assert client.get(f"/api/bearing/batches/{batch_no}").json()["检查证据"]
