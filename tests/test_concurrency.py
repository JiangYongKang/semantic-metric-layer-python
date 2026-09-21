"""并发与资源上限测试：快照隔离、缓存一致性、失败不污染。"""

from __future__ import annotations

import threading
from decimal import Decimal

from tests._support import LoggingTestCase
from sml.caliber import AggKind, CaliberBook, DimensionSpec, MeasureSpec
from sml.dataset import DatasetRegistry
from sml.engine import Query, QueryEngine
from sml.errors import ResourceLimitError
from sml.limits import LimitAction, ResourceBudget
from sml.model import build_model
from sml.registry import MetricLayer
from sml.relations import RelationGraph


def _seed(layer: MetricLayer, n: int = 200) -> None:
    layer.register_dataset(
        "orders",
        [{"oid": i, "g": "A" if i % 2 else "B", "v": i} for i in range(1, n + 1)],
        primary_key=("oid",),
    )
    layer.add_dimension(DimensionSpec("g", "orders", "g"))
    layer.add_measure(MeasureSpec("s", "orders", AggKind.SUM, "v"))
    layer.add_measure(MeasureSpec("c", "orders", AggKind.COUNT))


class LimitTest(LoggingTestCase):
    def test_output_rows_reject(self) -> None:
        layer = MetricLayer()
        _seed(layer)
        with self.assertRaises(ResourceLimitError) as cm:
            layer.query(
                Query(dimensions=("g",), measures=("c",)),
                budget=ResourceBudget(max_output_rows=1),
            )
        self.assertEqual(cm.exception.code, "resource_limit")
        self.log_judgement("2 个分组、预算 1 行、reject", "拒绝且 rejected=true",
                           cm.exception.details["rejected"])
        self.assertTrue(cm.exception.details["rejected"])

    def test_output_rows_truncate(self) -> None:
        layer = MetricLayer()
        _seed(layer)
        res = layer.query(
            Query(dimensions=("g",), measures=("c",)),
            budget=ResourceBudget(max_output_rows=1, on_overflow=LimitAction.TRUNCATE),
        )
        self.log_judgement("预算 1 行、truncate", "仅 1 行且 truncated", res.truncated)
        self.assertEqual(len(res.rows), 1)
        self.assertTrue(res.truncated)

    def test_high_cardinality_rejected_before_materialize(self) -> None:
        layer = MetricLayer()
        _seed(layer, n=200)
        with self.assertRaises(ResourceLimitError) as cm:
            layer.query(
                Query(dimensions=("g",), measures=("c",)),
                budget=ResourceBudget(max_groups=1),
            )
        self.assertEqual(cm.exception.limit_kind, "max_groups")
        self.log_judgement("max_groups=1", "高基数拒绝", cm.exception.code)

    def test_timeout_rejected(self) -> None:
        # 极小时间预算，保证执行过程中被发现
        layer = MetricLayer()
        _seed(layer, n=5)
        with self.assertRaises(ResourceLimitError) as cm:
            layer.query(
                Query(dimensions=("g",), measures=("c",)),
                budget=ResourceBudget(timeout_seconds=0.0),
            )
        self.assertEqual(cm.exception.limit_kind, "timeout_seconds")
        self.log_judgement("timeout=0", "超时拒绝", cm.exception.code)

    def test_failed_query_not_cached(self) -> None:
        layer = MetricLayer()
        _seed(layer)
        try:
            layer.query(Query(dimensions=("g",), measures=("c",)),
                        budget=ResourceBudget(max_output_rows=1))
        except ResourceLimitError:
            pass
        self.log_judgement("一次拒绝查询后", "缓存为空且后续查询正常", layer.cache_size())
        self.assertEqual(layer.cache_size(), 0)
        res = layer.query(Query(dimensions=("g",), measures=("c",)))
        self.assertEqual(len(res.rows), 2)


class ConcurrencyTest(LoggingTestCase):
    def test_concurrent_queries_see_consistent_snapshot(self) -> None:
        layer = MetricLayer()
        _seed(layer)
        errors: list[Exception] = []
        results: set[tuple] = set()

        def worker() -> None:
            try:
                for _ in range(50):
                    res = layer.query(Query(dimensions=("g",), measures=("s", "c")))
                    # 结果要么对应 200 行版本，要么对应 400 行版本；二者皆确定
                    totals = tuple(sorted((row[0], row[1], row[2]) for row in res.rows))
                    results.add(totals)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        def updater() -> None:
            try:
                for i in range(5):
                    layer.replace_dataset_data(
                        "orders",
                        [{"oid": k, "g": "A" if k % 2 else "B", "v": k}
                         for k in range(1, 401)],
                    )
                    layer.replace_dataset_data(
                        "orders",
                        [{"oid": k, "g": "A" if k % 2 else "B", "v": k}
                         for k in range(1, 201)],
                    )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        threads.append(threading.Thread(target=updater))
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 200 行版本: A=1+3+...+199(100个,和10000), B=2+4+...+200(和10100)
        v200 = (("A", 10000, 100), ("B", 10100, 100))
        # 400 行版本
        a400 = sum(range(1, 400, 2))   # 奇数
        b400 = sum(range(2, 401, 2))   # 偶数
        v400 = (("A", a400, 200), ("B", b400, 200))
        self.log_judgement("6 查询线程 + 1 更新线程并发",
                           f"无异常；结果只可能是 v200={v200} 或 v400={v400}",
                           f"errors={errors}, 观测结果集={results}")
        self.assertEqual(errors, [])
        self.assertTrue(results)
        self.assertTrue(results <= {v200, v400})

    def test_cache_invalidated_on_update(self) -> None:
        layer = MetricLayer()
        _seed(layer)
        res1 = layer.query(Query(measures=("s",)))
        old = res1.rows[0][0]
        layer.replace_dataset_data(
            "orders",
            [{"oid": k, "g": "A", "v": 1} for k in range(1, 11)],
        )
        self.log_judgement("更新后缓存大小", "缓存已清空", layer.cache_size())
        self.assertEqual(layer.cache_size(), 0)
        res2 = layer.query(Query(measures=("s",)))
        self.assertNotEqual(res2.rows[0][0], old)
        self.assertEqual(res2.rows[0][0], 10)
        self.assertGreater(res2.model_version, res1.model_version)

    def test_failed_update_preserves_version_and_cache(self) -> None:
        layer = MetricLayer()
        _seed(layer)
        res = layer.query(Query(measures=("c",)))
        version_before = layer.model_version()
        try:
            layer.replace_dataset_data("orders", [{"oid": 1}])  # 删列
        except Exception:
            pass
        res2 = layer.query(Query(measures=("c",)))
        self.log_judgement("失败更新后", "版本不变、缓存仍命中、行数=200",
                           f"{version_before}->{layer.model_version()}, "
                           f"{res2.resource_stats.get('cache_hit')}, {res2.rows[0][0]}")
        self.assertEqual(layer.model_version(), version_before)
        self.assertTrue(res2.resource_stats.get("cache_hit"))
        self.assertEqual(res2.rows[0][0], 200)

    def test_held_snapshot_is_stable_after_mutation(self) -> None:
        reg = DatasetRegistry()
        reg.register("t", [{"id": 1, "v": 1}], primary_key=("id",))
        book = CaliberBook(reg)
        book.add_measure(MeasureSpec("c", "t", AggKind.COUNT))
        snap = build_model(1, reg, book, RelationGraph(reg))
        reg.replace_data("t", [{"id": i, "v": i} for i in range(1, 51)])
        res = QueryEngine(snap).run(Query(measures=("c",)))
        self.log_judgement("持有旧快照，注册中心已更新到 50 行", "快照查询仍为 1 行",
                           res.rows[0][0])
        self.assertEqual(res.rows[0][0], 1)


if __name__ == "__main__":
    unittest.main()
