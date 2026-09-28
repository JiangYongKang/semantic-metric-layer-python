"""历史版本回查、保留淘汰、失败发布、并发完整版本可见性测试。"""

from __future__ import annotations

import threading

from sml.caliber import AggKind, DimensionSpec, MeasureSpec
from sml.engine import Filter, Query
from sml.errors import (
    NonUniqueKeyError,
    RetentionConfigError,
    SourceUpdateError,
    VersionEvictedError,
    VersionNotFoundError,
    VersionOutOfRangeError,
)
from sml.history import VersionArchive
from sml.limits import LimitAction, ResourceBudget
from sml.model import build_model
from sml.caliber import CaliberBook
from sml.dataset import DatasetRegistry
from sml.registry import MetricLayer
from sml.relations import Cardinality, Relation, RelationGraph
from tests._support import LoggingTestCase


def _simple_model(version: int, value: int):
    reg = DatasetRegistry()
    reg.register("t", [{"id": 1, "v": value}], primary_key=("id",))
    book = CaliberBook(reg)
    book.add_measure(MeasureSpec("s", "t", AggKind.SUM, "v"))
    return build_model(version, reg, book, RelationGraph(reg))


class ArchiveRetentionTest(LoggingTestCase):
    def test_fifo_eviction_boundary(self) -> None:
        arc = VersionArchive(retention=2)
        evicted_all: list[int] = []
        for v, val in enumerate((10, 20, 30, 40), start=1):
            evicted_all += arc.publish(_simple_model(v, val))
        # 发布 1,2,3,4 后只剩 [3,4]；v1 在发 v3 时淘汰，v2 在发 v4 时淘汰
        self.log_judgement("retention=2，依次发布 4 个版本",
                           "淘汰 [1,2]，留存 [3,4]",
                           f"evicted={evicted_all}, kept={arc.retained_versions()}")
        self.assertEqual(evicted_all, [1, 2])
        self.assertEqual(arc.retained_versions(), [3, 4])
        self.assertEqual(arc.oldest_version(), 3)

    def test_retention_zero_keeps_nothing(self) -> None:
        arc = VersionArchive(retention=0)
        evicted = arc.publish(_simple_model(1, 1))
        self.assertEqual(evicted, [1])
        self.assertEqual(arc.retained_versions(), [])
        self.assertIsNone(arc.oldest_version())
        # 从未留存 -> ever_archived_min 不记录
        self.assertIsNone(arc.ever_archived_min())

    def test_retention_none_unbounded(self) -> None:
        arc = VersionArchive(retention=None)
        for v in range(1, 20):
            self.assertEqual(arc.publish(_simple_model(v, v)), [])
        self.assertEqual(len(arc.retained_versions()), 19)

    def test_shrink_retention_evicts_oldest_immediately(self) -> None:
        arc = VersionArchive(retention=None)
        for v in range(1, 5):
            arc.publish(_simple_model(v, v))
        evicted = arc.set_retention(1)
        self.log_judgement("留存 4 版后把上限降到 1", "淘汰 [1,2,3]，留存 [4]",
                           f"{evicted}, {arc.retained_versions()}")
        self.assertEqual(evicted, [1, 2, 3])
        self.assertEqual(arc.retained_versions(), [4])
        # 曾经留存过的版本仍被记忆为「曾归档」
        self.assertEqual(arc.ever_archived_min(), 1)

    def test_invalid_retention_rejected(self) -> None:
        for bad in (-1, 1.5, "2", True):
            with self.assertRaises(RetentionConfigError):
                VersionArchive(retention=bad)
        with self.assertRaises(RetentionConfigError):
            VersionArchive().set_retention(-2)


def _seed_star(layer: MetricLayer, cities: tuple[str, str] = ("EAST", "WEST")) -> None:
    layer.register_dataset(
        "orders",
        [{"oid": 1, "cid": 10, "v": 1}, {"oid": 2, "cid": 20, "v": 2}],
        primary_key=("oid",),
    )
    layer.register_dataset(
        "customers",
        [{"cid": 10, "city": cities[0]}, {"cid": 20, "city": cities[1]}],
    )
    layer.add_relation(
        Relation("o_c", "orders", ("cid",), "customers", ("cid",),
                 Cardinality.MANY_TO_ONE)
    )
    layer.add_dimension(DimensionSpec("city", "customers", "city"))
    layer.add_measure(MeasureSpec("s", "orders", AggKind.SUM, "v"))
    layer.add_measure(MeasureSpec("cnt", "orders", AggKind.COUNT))


class HistoricalReplayTest(LoggingTestCase):
    def test_historical_result_matches_then(self) -> None:
        layer = MetricLayer()
        _seed_star(layer)
        q = Query(dimensions=("city",), measures=("s", "cnt"))
        v_old = layer.model_version()
        old = layer.query(q)
        layer.replace_dataset_data(
            "customers", [{"cid": 10, "city": "NORTH"}, {"cid": 20, "city": "SOUTH"}]
        )
        layer.replace_dataset_data(
            "orders", [{"oid": 1, "cid": 10, "v": 100}, {"oid": 2, "cid": 20, "v": 200}]
        )
        cur = layer.query(q)
        hist = layer.query(q, at_version=v_old)
        self.log_judgement(
            f"旧版 v{v_old} 与当前 v{cur.model_version}",
            "回查行/列/血缘与当时逐字节一致；当前不受影响",
            f"old={old.rows} hist={hist.rows} cur={cur.rows}",
        )
        self.assertEqual(hist.rows, old.rows)
        self.assertEqual(hist.columns, old.columns)
        self.assertEqual(hist.lineage.to_dict(), old.lineage.to_dict())
        self.assertEqual(hist.model_version, v_old)
        self.assertNotEqual(cur.rows, old.rows)
        self.assertEqual(cur.rows, (("NORTH", 100, 1), ("SOUTH", 200, 1)))

    def test_historical_filters_and_budget_share_semantics(self) -> None:
        layer = MetricLayer()
        _seed_star(layer)
        v_old = layer.model_version()
        layer.replace_dataset_data(
            "customers", [{"cid": 10, "city": "X"}, {"cid": 20, "city": "Y"}]
        )
        q = Query(
            dimensions=("city",),
            measures=("cnt",),
            filters=(Filter("city", ("X", "Y"), negate=True),),
        )
        # 旧版数据没有 X/Y：not-in 选全部 2 行；新版两城市都是 X/Y -> 0 行
        hist = layer.query(q, at_version=v_old)
        cur = layer.query(q)
        self.log_judgement("not-in 过滤在历史/当前分别执行",
                           "历史 2 行、当前 0 行", f"{hist.rows} vs {cur.rows}")
        self.assertEqual(hist.rows, (("EAST", 1), ("WEST", 1)))
        self.assertEqual(cur.rows, ())
        # 资源预算语义在历史版本上同样生效（2 分组、预算 1、reject）
        with self.assertRaises(Exception) as cm:
            layer.query(
                Query(dimensions=("city",), measures=("cnt",)),
                budget=ResourceBudget(max_output_rows=1),
                at_version=v_old,
            )
        self.assertEqual(cm.exception.code, "resource_limit")
        # truncate 预算也一致
        trunc = layer.query(
            Query(dimensions=("city",), measures=("cnt",)),
            budget=ResourceBudget(max_output_rows=1, on_overflow=LimitAction.TRUNCATE),
            at_version=v_old,
        )
        self.assertEqual(len(trunc.rows), 1)
        self.assertTrue(trunc.truncated)

    def test_history_query_does_not_touch_current_cache(self) -> None:
        layer = MetricLayer()
        _seed_star(layer)
        v_old = layer.model_version()
        layer.replace_dataset_data(
            "customers", [{"cid": 10, "city": "X"}, {"cid": 20, "city": "Y"}]
        )
        cur_q = Query(dimensions=("city",), measures=("s",))
        cur1 = layer.query(cur_q)
        cache_size_after_cur = layer.cache_size()
        # 多次历史查询：缓存大小不增加
        for _ in range(3):
            h = layer.query(cur_q, at_version=v_old)
            self.assertTrue(h.resource_stats["historical"])
            self.assertFalse(h.resource_stats["cache_hit"])
        self.assertEqual(layer.cache_size(), cache_size_after_cur)
        # 当前查询仍命中自己的缓存
        cur2 = layer.query(cur_q)
        self.assertTrue(cur2.resource_stats.get("cache_hit"))
        self.assertEqual(cur2.rows, cur1.rows)
        self.log_judgement("3 次历史查询后", "当前缓存大小不变且仍命中",
                           f"size={layer.cache_size()} hit={cur2.resource_stats.get('cache_hit')}")

    def test_version_error_distinctions(self) -> None:
        layer = MetricLayer(history_retention=1)
        _seed_star(layer)  # 多版变更
        versions = [layer.model_version()]
        layer.replace_dataset_data(
            "customers", [{"cid": 10, "city": "A"}, {"cid": 20, "city": "B"}]
        )
        versions.append(layer.model_version())
        layer.replace_dataset_data(
            "customers", [{"cid": 10, "city": "C"}, {"cid": 20, "city": "D"}]
        )
        versions.append(layer.model_version())
        oldest_kept = versions[-2]
        evicted_v = versions[0]
        q = Query(measures=("cnt",))
        # 命中留存版本
        self.assertEqual(layer.query(q, at_version=oldest_kept).model_version,
                         oldest_kept)
        # 曾经存在后被淘汰
        with self.assertRaises(VersionEvictedError) as cm:
            layer.query(q, at_version=evicted_v)
        self.log_judgement(f"被淘汰版本 {evicted_v}", "version_evicted",
                           cm.exception.code)
        self.assertEqual(cm.exception.details["reason"], "evicted_by_retention")
        self.assertEqual(cm.exception.details["oldest_available"], oldest_kept)
        # 从未存在：未来版本
        with self.assertRaises(VersionNotFoundError) as cm2:
            layer.query(q, at_version=layer.model_version() + 10)
        self.assertEqual(cm2.exception.details["reason"], "future_version")
        # 从未存在：非正/非整数
        for bad in (0, -3):
            with self.assertRaises(VersionNotFoundError):
                layer.query(q, at_version=bad)

    def test_retention_zero_out_of_range_not_evicted(self) -> None:
        layer = MetricLayer(history_retention=0)
        _seed_star(layer)
        old = layer.model_version()
        layer.replace_dataset_data(
            "customers", [{"cid": 10, "city": "A"}, {"cid": 20, "city": "B"}]
        )
        with self.assertRaises(VersionOutOfRangeError) as cm:
            layer.query(Query(measures=("cnt",)), at_version=old)
        self.log_judgement("retention=0 下查发布时就未留存的版本",
                           "version_out_of_range（不冒充 evicted）", cm.exception.code)
        self.assertEqual(cm.exception.code, "version_out_of_range")
        self.assertEqual(layer.retained_versions(), [])

    def test_shrink_retention_then_evicted_distinction(self) -> None:
        layer = MetricLayer()  # 默认不限
        _seed_star(layer)
        old = layer.model_version()
        layer.replace_dataset_data(
            "customers", [{"cid": 10, "city": "A"}, {"cid": 20, "city": "B"}]
        )
        # 旧版曾可回查
        self.assertTrue(layer.retained_versions())
        evicted = layer.set_history_retention(0)
        self.assertIn(old, evicted)
        with self.assertRaises(VersionEvictedError):
            layer.query(Query(measures=("cnt",)), at_version=old)
        # 调大上限不影响已淘汰版本
        layer.set_history_retention(5)
        with self.assertRaises(VersionEvictedError):
            layer.query(Query(measures=("cnt",)), at_version=old)
        self.log_judgement("曾经可回查后被缩容淘汰，再调大上限", "仍为 evicted，不复活",
                           "version_evicted")


if __name__ == "__main__":
    import unittest
    unittest.main()


class FailedPublishTest(LoggingTestCase):
    def _layer(self) -> MetricLayer:
        layer = MetricLayer()
        # customers 不声明主键：关联 one 侧唯一性是唯一防线，预检必须拦住
        layer.register_dataset(
            "orders",
            [{"oid": 1, "cid": 10, "v": 1}, {"oid": 2, "cid": 20, "v": 2}],
            primary_key=("oid",),
        )
        layer.register_dataset(
            "customers",
            [{"cid": 10, "city": "EAST"}, {"cid": 20, "city": "WEST"}],
        )
        layer.add_relation(
            Relation("o_c", "orders", ("cid",), "customers", ("cid",),
                     Cardinality.MANY_TO_ONE)
        )
        layer.add_dimension(DimensionSpec("city", "customers", "city"))
        layer.add_measure(MeasureSpec("s", "orders", AggKind.SUM, "v"))
        layer.add_measure(MeasureSpec("cnt", "orders", AggKind.COUNT))
        return layer

    def test_relation_key_duplicate_rejects_and_state_untouched(self) -> None:
        layer = self._layer()
        q = Query(dimensions=("city",), measures=("s", "cnt"))
        before = layer.query(q)
        version_before = layer.model_version()
        cache_before = layer.cache_size()
        with self.assertRaises(NonUniqueKeyError) as cm:
            layer.replace_dataset_data(
                "customers",
                [{"cid": 10, "city": "EAST"}, {"cid": 10, "city": "WEST"}],
            )
        self.log_judgement("更新使关联 one 侧键重复",
                           "non_unique_key 拒绝且版本/缓存/结果不变",
                           f"{cm.exception.code}, v{layer.model_version()}")
        self.assertEqual(layer.model_version(), version_before)
        self.assertEqual(layer.cache_size(), cache_before)
        after = layer.query(q)
        self.assertTrue(after.resource_stats.get("cache_hit"))
        self.assertEqual(after.rows, before.rows)
        self.assertEqual(after.lineage.to_dict(), before.lineage.to_dict())

    def test_null_relation_key_rejects(self) -> None:
        layer = self._layer()
        v0 = layer.model_version()
        with self.assertRaises(NonUniqueKeyError):
            layer.replace_dataset_data(
                "customers",
                [{"cid": None, "city": "EAST"}, {"cid": 20, "city": "WEST"}],
            )
        self.assertEqual(layer.model_version(), v0)
        # 当前数据仍可正常跨表查询
        rows = layer.query(
            Query(dimensions=("city",), measures=("cnt",))
        ).rows
        self.assertEqual(rows, (("EAST", 1), ("WEST", 1)))

    def test_drop_column_rejects_and_state_untouched(self) -> None:
        layer = self._layer()
        v0 = layer.model_version()
        probe = Query(dimensions=("city",), measures=("cnt",))
        layer.query(probe)
        with self.assertRaises(SourceUpdateError):
            layer.replace_dataset_data("customers", [{"cid": 10}, {"cid": 20}])
        self.assertEqual(layer.model_version(), v0)
        # 缓存仍命中（city 口径仍有效）
        hit = layer.query(probe).resource_stats.get("cache_hit")
        self.assertTrue(hit)

    def test_post_failure_definitions_still_work(self) -> None:
        layer = self._layer()
        v0 = layer.model_version()
        try:
            layer.replace_dataset_data(
                "customers",
                [{"cid": 10, "city": "EAST"}, {"cid": 10, "city": "WEST"}],
            )
        except NonUniqueKeyError:
            pass
        # 失败后新增口径/关联与正常数据更新必须照旧可用
        layer.add_dimension(DimensionSpec("cust_cid", "customers", "cid"))
        layer.replace_dataset_data(
            "customers", [{"cid": 10, "city": "NORTH"}, {"cid": 20, "city": "SOUTH"}]
        )
        self.assertGreater(layer.model_version(), v0)
        rows = layer.query(
            Query(dimensions=("city",), measures=("cnt",))
        ).rows
        self.log_judgement("失败发布后继续登记口径并成功更新",
                           "新数据生效，结果为 NORTH/SOUTH", rows)
        self.assertEqual(rows, (("NORTH", 1), ("SOUTH", 1)))

    def test_history_not_contaminated_by_failed_publish(self) -> None:
        layer = MetricLayer(history_retention=5)
        _seed_star(layer)
        old = layer.model_version()
        old_rows = layer.query(Query(measures=("cnt",))).rows
        layer.replace_dataset_data(
            "customers", [{"cid": 10, "city": "A"}, {"cid": 20, "city": "B"}]
        )
        v_after_ok = layer.model_version()
        try:
            layer.replace_dataset_data(
                "customers", [{"cid": 9, "city": "A"}, {"cid": 9, "city": "B"}]
            )
        except NonUniqueKeyError:
            pass
        # 失败更新：版本不前进、档案不新增、历史回查内容不变
        self.assertEqual(layer.model_version(), v_after_ok)
        self.assertNotIn(v_after_ok + 1, layer.retained_versions())
        self.assertEqual(
            layer.query(Query(measures=("cnt",)), at_version=old).rows, old_rows
        )


class ConcurrentVersionVisibilityTest(LoggingTestCase):
    def test_concurrent_readers_see_only_complete_versions(self) -> None:
        layer = MetricLayer()
        layer.register_dataset(
            "orders",
            [{"oid": i, "v": i} for i in range(1, 51)],
            primary_key=("oid",),
        )
        layer.add_measure(MeasureSpec("s", "orders", AggKind.SUM, "v"))
        layer.add_measure(MeasureSpec("cnt", "orders", AggKind.COUNT))

        # 每个完整版本「v 之和」的期望值集合：每次替换都是全量 1..n*50 的连续值
        expected: dict[int, int] = {}

        def total_for(n: int) -> int:
            return sum(range(1, 50 * n + 1))

        errors: list[Exception] = []
        seen: set[tuple[int, int]] = set()
        seen_lock = threading.Lock()
        stop = threading.Event()

        def reader() -> None:
            try:
                q = Query(measures=("s", "cnt"))
                while not stop.is_set():
                    r = layer.query(q)
                    # 计数必须恰好是 50*k，且 sum 与计数自洽 —— 完整版本判据
                    cnt = r.rows[0][1]
                    sval = r.rows[0][0]
                    k = cnt // 50
                    if cnt % 50 != 0 or sval != total_for(k):
                        errors.append(
                            AssertionError(f"半更新状态: version={r.model_version}, "
                                           f"cnt={cnt}, sum={sval}")
                        )
                        return
                    with seen_lock:
                        seen.add((r.model_version, int(sval), cnt))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=reader) for _ in range(6)]
        for t in threads:
            t.start()
        for k in range(1, 9):
            layer.replace_dataset_data(
                "orders", [{"oid": i, "v": i} for i in range(1, 50 * k + 1)]
            )
        stop.set()
        for t in threads:
            t.join()
        # 每个被观测到的 (版本, 结果) 必须与该版本的完整数据对应
        self.log_judgement("6 读线程与 8 次全量更新并发",
                           "无异常；每个观测结果都自洽于某完整版本",
                           f"errors={errors}, 观测={sorted(seen)}")
        self.assertEqual(errors, [])
        self.assertTrue(seen)
        for _ver, sval, cnt in seen:
            k = cnt // 50
            self.assertEqual((sval, cnt), (total_for(k), 50 * k))

    def test_concurrent_historical_and_current_queries_isolated(self) -> None:
        layer = MetricLayer(history_retention=10)
        layer.register_dataset("t", [{"id": 1, "v": 1}], primary_key=("id",))
        layer.add_measure(MeasureSpec("c", "t", AggKind.COUNT))
        versions: list[int] = []
        for n in range(2, 12):
            layer.replace_dataset_data(
                "t", [{"id": i, "v": i} for i in range(1, n + 1)]
            )
            versions.append((layer.model_version(), n))
        errors: list[Exception] = []

        def historical_reader() -> None:
            try:
                for ver, n in versions:
                    r = layer.query(Query(measures=("c",)), at_version=ver)
                    if r.rows != ((n,),):
                        errors.append(AssertionError((ver, r.rows)))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        def current_reader() -> None:
            try:
                for _ in range(100):
                    r = layer.query(Query(measures=("c",)))
                    if r.rows != ((11,),):
                        errors.append(AssertionError(("current", r.rows)))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        ts = [threading.Thread(target=historical_reader) for _ in range(4)]
        ts += [threading.Thread(target=current_reader) for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.log_judgement("4 历史回查线程 + 4 当前查询线程并发",
                           "历史逐版本行数正确，当前恒为最新 11 行，无异常",
                           f"errors={errors}")
        self.assertEqual(errors, [])

    def test_mixed_concurrent_updates_with_historical_reads(self) -> None:
        layer = MetricLayer(history_retention=50)
        layer.register_dataset("t", [{"id": 1, "v": 1}], primary_key=("id",))
        layer.add_measure(MeasureSpec("s", "t", AggKind.SUM, "v"))
        layer.add_measure(MeasureSpec("c", "t", AggKind.COUNT))
        # 口径齐备之后的版本才能用 (s,c) 回查；早于此版本指标尚不存在
        baseline = layer.model_version()

        errors: list[Exception] = []
        stop = threading.Event()

        def expect_sum(n: int) -> int:
            return n * (n + 1) // 2

        def mixed_reader() -> None:
            import random
            try:
                while not stop.is_set():
                    if random.random() < 0.5:
                        r = layer.query(Query(measures=("s", "c")))
                        n = r.rows[0][1]
                        if r.rows[0][0] != expect_sum(n):
                            errors.append(AssertionError(
                                f"当前版本半更新: {r.rows}"))
                            return
                    else:
                        hist_vers = [v for v in layer.retained_versions()
                                     if v >= baseline]
                        if not hist_vers:
                            continue
                        ver = random.choice(hist_vers)
                        r = layer.query(Query(measures=("s", "c")), at_version=ver)
                        n = r.rows[0][1]
                        if r.rows[0][0] != expect_sum(n) or r.model_version != ver:
                            errors.append(AssertionError(
                                f"历史回查串档: v{ver} -> {r.rows}"))
                            return
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        readers = [threading.Thread(target=mixed_reader) for _ in range(6)]
        for t in readers:
            t.start()
        for n in range(2, 40):
            layer.replace_dataset_data(
                "t", [{"id": i, "v": i} for i in range(1, n + 1)]
            )
        stop.set()
        for t in readers:
            t.join()
        self.log_judgement("39 次更新与 6 个混合（当前/历史）读线程并发",
                           "无串档、无半更新、无异常", f"errors={errors}")
        self.assertEqual(errors, [])
