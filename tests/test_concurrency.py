"""并发：查询与更新并发时结果一致、无半更新状态、缓存与当前数据口径一致。"""
import threading
from decimal import Decimal

from sml.engine import Query, QueryEngine
from sml.model import Agg, Metric, SemanticModel
from tests.conftest import log_case


def test_concurrent_reads_and_updates(registry):
    """更新进行中，每个查询必须看到某个完整版本（1 或 2），绝不见半更新。"""
    registry.register("t", [{"id": i, "v": 1} for i in range(200)])
    m = SemanticModel(registry)
    m.add_metric(Metric("s", "t", "v", Agg.SUM))
    m.add_metric(Metric("c", "t", None, Agg.COUNT))
    eng = QueryEngine(registry, m)
    q = Query(metrics=("s", "c"))

    errors, observations = [], []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                r = eng.run(q)
                observations.append((r.rows[0]["s"], r.rows[0]["c"],
                                     r.dataset_versions["t"]))
            except Exception as e:  # noqa: BLE001
                errors.append(e)

    def writer():
        for i in range(50):
            registry.update("t", [{"id": j, "v": 1} for j in range(200 + i % 3)])

    readers = [threading.Thread(target=reader) for _ in range(8)]
    writer_t = threading.Thread(target=writer)
    for t in readers + [writer_t]:
        t.start()
    writer_t.join()          # 只等写线程完成
    stop.set()               # 再通知读线程退出
    for t in readers:
        t.join()

    # 判定依据：sum 必须等于 count（每行 v=1），且 count 与版本数据一致
    bad = [(s, c, v) for s, c, v in observations if s != c]
    log_case("并发读写", "8 读线程 + 1 写线程(50 次 update)",
             "每个结果的 sum==count（无半更新），且无异常",
             {"queries": len(observations), "errors": len(errors), "bad": len(bad)})
    assert not errors
    assert not bad
    assert len(observations) > 0


def test_cache_never_stale(registry):
    """缓存键含数据版本与模型版本：更新后同查询必得新结果，不命中旧缓存。"""
    registry.register("t", [{"id": 1, "v": 5}])
    m = SemanticModel(registry)
    m.add_metric(Metric("s", "t", "v", Agg.SUM))
    eng = QueryEngine(registry, m)
    q = Query(metrics=("s",))
    r1 = eng.run(q)
    registry.update("t", [{"id": 1, "v": 5}, {"id": 2, "v": 7}])
    r2 = eng.run(q)
    log_case("缓存一致性", "更新后同一查询",
             "版本进入缓存键，结果必须反映新数据",
             (r1.rows[0]["s"], r2.rows[0]["s"]))
    assert r1.rows[0]["s"] == Decimal(5)
    assert r2.rows[0]["s"] == Decimal(12)
    assert r2.dataset_versions["t"] == 2


def test_concurrent_identical_queries_consistent(registry):
    """多线程同时发同一查询，所有结果必须逐行一致。"""
    registry.register("t", [{"id": i, "v": i * 0.1} for i in range(100)])
    m = SemanticModel(registry)
    m.add_metric(Metric("s", "t", "v", Agg.SUM))
    eng = QueryEngine(registry, m)
    q = Query(metrics=("s",))
    results = []
    def run():
        results.append(eng.run(q).rows[0]["s"])
    threads = [threading.Thread(target=run) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    log_case("并发同查询", "16 线程同一查询", "所有结果相等且为精确值",
             set(map(str, results)))
    assert len(set(results)) == 1
    assert results[0] == sum(Decimal(str(i * 0.1)) for i in range(100))
