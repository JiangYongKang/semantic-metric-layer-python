"""历史版本回查、保留/淘汰、失败发布不污染、并发只看完整版本的测试。"""

from __future__ import annotations

import threading
from decimal import Decimal

from tests._support import LoggingTestCase
from sml.caliber import AggKind, DimensionSpec, MeasureSpec, RatioSpec
from sml.engine import Filter, Query
from sml.errors import (
    CaliberConflictError,
    InvalidCaliberError,
    NonUniqueKeyError,
    SourceUpdateError,
    VersionEvictedError,
    VersionNeverExistedError,
    VersionOutOfRangeError,
)
from sml.limits import ResourceBudget
from sml.registry import MetricLayer
from sml.relations import Cardinality, Relation


def _seed_orders(layer: MetricLayer) -> None:
    """orders + customers + o_c 关联 + 常用维度/指标。"""
    layer.register_dataset(
        "orders",
        [
            {"oid": 1, "cid": 10, "region": "east", "amount": Decimal("100.00"), "qty": 2},
            {"oid": 2, "cid": 10, "region": "east", "amount": Decimal("0.10"), "qty": 1},
            {"oid": 3, "cid": 20, "region": "west", "amount": Decimal("200.30"), "qty": 3},
        ],
        primary_key=("oid",),
    )
    layer.register_dataset(
        "customers",
        [{"cid": 10, "city": "BJ"}, {"cid": 20, "city": "SH"}],
        primary_key=("cid",),
    )
    layer.add_relation(
        Relation("o_c", "orders", ("cid",), "customers", ("cid",),
                 Cardinality.MANY_TO_ONE)
    )
    layer.add_dimension(DimensionSpec("region", "orders", "region"))
    layer.add_dimension(DimensionSpec("city", "customers", "city"))
    layer.add_measure(MeasureSpec("total", "orders", AggKind.SUM, "amount"))
    layer.add_measure(MeasureSpec("cnt", "orders", AggKind.COUNT))
    layer.add_ratio(RatioSpec("avg_amt", "orders", "total", "cnt"))


class HistoricalLookupTest(LoggingTestCase):
    def test_historical_query_returns_then_data_and_lineage(self) -> None:
        layer = MetricLayer(max_history=10)
        _seed_orders(layer)
        # 在历史版本上记录期望结果：按 city 聚合
        q = Query(dimensions=("city",), measures=("total", "cnt", "avg_amt"))
        past = layer.query(q)
        past_version = past.model_version
        past_rows = past.rows
        past_lineage = past.lineage.to_dict()

        # 用新数据更新 orders：cid=10 金额从 100.10 变成 999.00，并多一行
        layer.replace_dataset_data(
            "orders",
            [
                {"oid": 1, "cid": 10, "region": "east", "amount": Decimal("999.00"), "qty": 2},
                {"oid": 2, "cid": 10, "region": "east", "amount": Decimal("0.10"), "qty": 1},
                {"oid": 3, "cid": 20, "region": "west", "amount": Decimal("200.30"), "qty": 3},
                {"oid": 4, "cid": 20, "region": "west", "amount": Decimal("5.00"), "qty": 1},
            ],
        )
        now = layer.query(q)
        self.assertGreater(now.model_version, past_version)

        back = layer.query(q, at_version=past_version)
        self.log_judgement(
            f"回查版本 {past_version}（当前 {now.model_version}）",
            "结果与血缘逐字节等于当时当次，不被当前数据顶替",
            f"rows={back.rows}",
        )
        self.assertEqual(back.model_version, past_version)
        self.assertEqual(back.rows, past_rows)
        self.assertEqual(back.lineage.to_dict(), past_lineage)
        self.assertEqual(back.columns, past.columns)
        # BJ 总额当时为 100.10，当前为 999.10；明确不等于当前
        bj_past = next(r for r in back.rows if r[0] == "BJ")
        bj_now = next(r for r in now.rows if r[0] == "BJ")
        self.assertEqual(bj_past[1], Decimal("100.10"))
        self.assertEqual(bj_now[1], Decimal("999.10"))

    def test_historical_query_supports_filters_dimensions_and_budget(self) -> None:
        layer = MetricLayer()
        _seed_orders(layer)
        layer.add_measure(MeasureSpec("mx", "orders", AggKind.MAX, "amount"))
        pv = layer.model_version()
        # 之后再做一次不相关变更，使 pv 成为历史版本
        layer.add_measure(MeasureSpec("qty_sum", "orders", AggKind.SUM, "qty"))
        q = Query(
            dimensions=("region",),
            measures=("cnt", "mx"),
            filters=(Filter("region", ("east",)),),
        )
        res = layer.query(q, at_version=pv, budget=ResourceBudget(max_output_rows=5))
        self.log_judgement("历史版本 + 过滤 + 预算", "仅 east 1 组、金额最大值 100.00", res.rows)
        self.assertEqual(res.model_version, pv)
        self.assertEqual(res.rows, (("east", 2, Decimal("100.00")),))

    def test_historical_caliber_must_be_used_not_current(self) -> None:
        """历史版本上还不存在的口径，回查时不能用当前口径顶替。"""
        layer = MetricLayer()
        _seed_orders(layer)
        pv = layer.model_version()
        # 之后才新增的口径
        layer.add_measure(MeasureSpec("qty_sum", "orders", AggKind.SUM, "qty"))
        from sml.errors import CaliberNotFoundError

        with self.assertRaises(CaliberNotFoundError) as cm:
            layer.query(Query(measures=("qty_sum",)), at_version=pv)
        self.log_judgement("用历史版本查询之后才定义的口径",
                           "caliber_not_found，而不是拿当前口径计算",
                           cm.exception.code)
        self.assertEqual(cm.exception.code, "caliber_not_found")
        # 当前版本上同口径正常可用
        self.assertEqual(layer.query(Query(measures=("qty_sum",))).rows[0][0], 6)

    def test_historical_query_does_not_touch_current_cache(self) -> None:
        layer = MetricLayer()
        _seed_orders(layer)
        pv = layer.model_version()
        layer.replace_dataset_data(
            "orders",
            [
                {"oid": 1, "cid": 10, "region": "east", "amount": Decimal("1.00"), "qty": 1},
                {"oid": 2, "cid": 20, "region": "west", "amount": Decimal("2.00"), "qty": 1},
            ],
        )
        # 当前查询写入缓存
        q = Query(measures=("cnt",))
        first = layer.query(q)
        self.assertIsNone(first.resource_stats.get("cache_hit"))
        size_before = layer.cache_size()
        # 历史回查（同查询形状）：既不命中、也不写入
        hist = layer.query(q, at_version=pv)
        self.assertEqual(hist.rows[0][0], 3)  # 当时 3 行
        self.log_judgement("历史回查同形查询", "缓存大小不变，且历史不标记 cache_hit",
                           f"{size_before} -> {layer.cache_size()}, hit={hist.resource_stats.get('cache_hit')}")
        self.assertEqual(layer.cache_size(), size_before)
        self.assertIsNone(hist.resource_stats.get("cache_hit"))
        # 当前缓存仍命中
        second = layer.query(q)
        self.assertTrue(second.resource_stats.get("cache_hit"))
        self.assertEqual(second.rows[0][0], 2)

    def test_explicit_current_version_uses_current_cache(self) -> None:
        layer = MetricLayer()
        _seed_orders(layer)
        q = Query(measures=("cnt",))
        layer.query(q)
        cur = layer.model_version()
        hit = layer.query(q, at_version=cur)
        self.log_judgement("显式指定当前版本号", "走当前缓存并命中", hit.resource_stats.get("cache_hit"))
        self.assertTrue(hit.resource_stats.get("cache_hit"))


if __name__ == "__main__":
    unittest.main()


class RetentionEvictionTest(LoggingTestCase):
    def test_fifo_eviction_boundary_and_distinguishable_errors(self) -> None:
        layer = MetricLayer(max_history=2)
        layer.register_dataset(
            "t", [{"id": 1, "v": 1}], primary_key=("id",))
        layer.add_measure(MeasureSpec("c", "t", AggKind.COUNT))
        versions = [layer.model_version()]  # v2
        # 继续发布 4 次（每次改数据值），版本 -> 3..6
        for k in range(1, 5):
            layer.replace_dataset_data("t", [{"id": 1, "v": k}])
            versions.append(layer.model_version())
        self.assertEqual(versions, [2, 3, 4, 5, 6])
        current = layer.model_version()  # 6
        retained = layer.history_versions()
        self.log_judgement("max_history=2，发布到 v6", "历史仅保留 (4,5)，当前 6",
                           f"retained={retained}")
        self.assertEqual(retained, (4, 5))

        # 保留栈内：可回查
        r = layer.query(Query(measures=("c",)), at_version=4)
        self.assertEqual(r.model_version, 4)

        # v3 曾发布但被淘汰 -> version_evicted，且 details 可区分
        with self.assertRaises(VersionEvictedError) as cm:
            layer.query(Query(measures=("c",)), at_version=3)
        self.assertEqual(cm.exception.code, "version_evicted")
        self.log_judgement("回查已淘汰的 v3", "version_evicted + retained_versions 清单",
                           cm.exception.details)
        self.assertEqual(cm.exception.details["retained_versions"], [4, 5])
        self.assertEqual(cm.exception.details["current_version"], current)
        self.assertEqual(cm.exception.details["max_history"], 2)

        # v999 从未存在（大于当前）-> version_never_existed
        with self.assertRaises(VersionNeverExistedError) as cm2:
            layer.query(Query(measures=("c",)), at_version=999)
        self.assertEqual(cm2.exception.code, "version_never_existed")
        self.log_judgement("回查 v999", "version_never_existed", cm2.exception.details)

        # 0 / 负数 / 非整数 -> version_out_of_range
        for bad in (0, -1):
            with self.assertRaises(VersionOutOfRangeError) as cm3:
                layer.query(Query(measures=("c",)), at_version=bad)
            self.assertEqual(cm3.exception.code, "version_out_of_range")
        self.log_judgement("回查 0/-1", "version_out_of_range", "已校验")

    def test_evicted_result_must_not_fallback_to_current(self) -> None:
        layer = MetricLayer(max_history=1)
        layer.register_dataset(
            "t", [{"id": i, "v": i} for i in range(1, 4)], primary_key=("id",))
        layer.add_measure(MeasureSpec("c", "t", AggKind.COUNT))
        v2 = layer.model_version()
        # 再发布两次把 v2 淘汰
        layer.replace_dataset_data("t", [{"id": 1, "v": 1}])
        layer.replace_dataset_data("t", [{"id": 1, "v": 1}, {"id": 2, "v": 2}])
        self.assertNotIn(v2, layer.history_versions())
        # 淘汰版本必须报错，绝不返回当前 2 行结果
        with self.assertRaises(VersionEvictedError):
            layer.query(Query(measures=("c",)), at_version=v2)
        self.log_judgement(f"v{v2} 已淘汰", "回查报错而非返回当前结果", "version_evicted")
        # 当前版本正常返回 2
        self.assertEqual(layer.query(Query(measures=("c",))).rows[0][0], 2)

    def test_zero_retention_evicts_immediately(self) -> None:
        layer = MetricLayer(max_history=0)
        layer.register_dataset("t", [{"id": 1, "v": 1}], primary_key=("id",))
        v1 = layer.model_version()
        layer.add_measure(MeasureSpec("c", "t", AggKind.COUNT))
        self.assertEqual(layer.history_versions(), ())
        self.assertEqual(layer.history_limit(), 0)
        with self.assertRaises(VersionEvictedError) as cm:
            layer.snapshot_model(at_version=v1)
        self.log_judgement("max_history=0 时回查上一版",
                           "立即淘汰 -> version_evicted", cm.exception.code)
        # 无历史时 oldest_available 即当前
        self.assertEqual(layer.snapshot_model().version, layer.model_version())

    def test_shrink_limit_evicts_immediately_and_grow_keeps(self) -> None:
        layer = MetricLayer(max_history=5)
        layer.register_dataset("t", [{"id": 1, "v": 1}], primary_key=("id",))
        for k in range(1, 6):
            layer.replace_dataset_data("t", [{"id": 1, "v": k}])
        self.assertEqual(len(layer.history_versions()), 5)
        evicted = layer.set_history_limit(2)
        self.log_judgement("容量从 5 调到 2", "立即 FIFO 淘汰最旧 3 个", evicted)
        self.assertEqual(evicted, (1, 2, 3))
        self.assertEqual(len(layer.history_versions()), 2)
        # 再调大不影响已淘汰版本
        self.assertEqual(layer.set_history_limit(10), ())
        self.assertEqual(len(layer.history_versions()), 2)
        for v in (1, 2, 3):
            with self.assertRaises(VersionEvictedError):
                layer.snapshot_model(at_version=v)

    def test_default_query_is_current_and_versions_listing(self) -> None:
        layer = MetricLayer()  # 默认容量
        self.assertEqual(layer.history_limit(), 10)
        layer.register_dataset("t", [{"id": 1, "v": 1}], primary_key=("id",))
        layer.add_measure(MeasureSpec("c", "t", AggKind.COUNT))
        cur = layer.model_version()
        no_ver = layer.query(Query(measures=("c",)))
        explicit = layer.query(Query(measures=("c",)), at_version=cur)
        self.assertEqual(no_ver.model_version, cur)
        self.assertEqual(explicit.model_version, cur)
        self.assertEqual(layer.history_versions(), tuple(range(1, cur)))
        self.log_judgement("缺省查询 / 版本清单",
                           f"当前 {cur}，历史 {layer.history_versions()}", "ok")


class AtomicPublishTest(LoggingTestCase):
    def test_relation_key_breaks_on_data_update_is_rejected(self) -> None:
        """数据更新使既有 many_to_one 的 one 侧键不再唯一 -> 整次拒绝。"""
        layer = MetricLayer()
        # customers 不设主键：唯一键完全由关联在发布期校验
        layer.register_dataset(
            "customers", [{"cid": 10, "city": "BJ"}, {"cid": 20, "city": "SH"}])
        layer.register_dataset(
            "orders",
            [{"oid": 1, "cid": 10, "amount": Decimal("1.00")},
             {"oid": 2, "cid": 20, "amount": Decimal("2.00")}],
            primary_key=("oid",))
        layer.add_relation(
            Relation("o_c", "orders", ("cid",), "customers", ("cid",),
                     Cardinality.MANY_TO_ONE))
        layer.add_dimension(DimensionSpec("city", "customers", "city"))
        layer.add_measure(MeasureSpec("c", "orders", AggKind.COUNT))

        ver = layer.model_version()
        # 建立缓存
        layer.query(Query(measures=("c",)))
        cache_before = layer.cache_size()

        with self.assertRaises(NonUniqueKeyError) as cm:
            layer.replace_dataset_data(
                "customers", [{"cid": 10, "city": "BJ"}, {"cid": 10, "city": "X"}])
        self.assertEqual(cm.exception.details["reason"], "duplicate_key")
        self.log_judgement("更新使 one 侧键重复", "non_unique_key 整次拒绝", cm.exception.code)

        # 版本号、缓存、数据、关联全部保持更新前
        self.assertEqual(layer.model_version(), ver)
        self.assertEqual(layer.cache_size(), cache_before)
        snap = layer.snapshot_model()
        self.assertEqual(
            [r["city"] for r in snap.datasets["customers"].records], ["BJ", "SH"])
        # 关联仍可用于分组
        rows = layer.query(Query(dimensions=("city",), measures=("c",))).rows
        self.assertEqual(dict(rows), {"BJ": 1, "SH": 1})
        # 失败后仍可正常定义口径与关联
        layer.add_measure(MeasureSpec("total", "orders", AggKind.SUM, "amount"))
        self.assertEqual(layer.model_version(), ver + 1)

    def test_drop_column_rejected_preserves_everything(self) -> None:
        """删列（结构不兼容）拒绝：版本/缓存/当前结果不变。"""
        layer = MetricLayer()
        _seed_orders(layer)
        ver = layer.model_version()
        before = layer.query(Query(measures=("total", "cnt")))
        cache_before = layer.cache_size()
        with self.assertRaises(SourceUpdateError):
            layer.replace_dataset_data("orders", [{"oid": 1}])
        self.log_judgement("删列更新被拒",
                           "版本/缓存/结果与更新前一致",
                           f"ver={layer.model_version()} cache={layer.cache_size()}")
        self.assertEqual(layer.model_version(), ver)
        self.assertEqual(layer.cache_size(), cache_before)
        after = layer.query(Query(measures=("total", "cnt")))
        self.assertEqual(after.rows, before.rows)
        self.assertTrue(after.resource_stats.get("cache_hit"))

    def test_invalid_caliber_add_does_not_bump_or_clear_cache(self) -> None:
        """新增口径引用不存在字段（口径失效）-> 拒绝且零副作用。"""
        layer = MetricLayer()
        _seed_orders(layer)
        ver = layer.model_version()
        layer.query(Query(measures=("cnt",)))
        cache_before = layer.cache_size()
        with self.assertRaises(InvalidCaliberError):
            layer.add_measure(MeasureSpec("bad", "orders", AggKind.SUM, "nope"))
        self.log_judgement("引用不存在字段的口径",
                           "invalid_caliber 拒绝、不升版不清缓存",
                           f"ver={layer.model_version()} cache={layer.cache_size()}")
        self.assertEqual(layer.model_version(), ver)
        self.assertEqual(layer.cache_size(), cache_before)
        # 坏口径确实不存在，且好口径仍能加
        with self.assertRaises(Exception):
            layer.query(Query(measures=("bad",)))
        layer.add_measure(MeasureSpec("qsum", "orders", AggKind.SUM, "qty"))
        self.assertEqual(layer.model_version(), ver + 1)

    def test_duplicate_caliber_name_rejected_without_side_effect(self) -> None:
        layer = MetricLayer()
        _seed_orders(layer)
        ver = layer.model_version()
        with self.assertRaises(CaliberConflictError):
            layer.add_dimension(DimensionSpec("total", "orders", "region"))
        self.log_judgement("占用已有指标名定义维度", "caliber_conflict、不升版",
                           f"ver={layer.model_version()}")
        self.assertEqual(layer.model_version(), ver)
        # 原 total 仍是指标而非维度
        self.assertEqual(layer.query(Query(measures=("total",))).rows[0][0],
                         Decimal("300.40"))

    def test_duplicate_dataset_registration_rejected(self) -> None:
        layer = MetricLayer()
        _seed_orders(layer)
        ver = layer.model_version()
        from sml.errors import DatasetAlreadyExistsError

        with self.assertRaises(DatasetAlreadyExistsError):
            layer.register_dataset("orders", [{"oid": 1}])
        self.assertEqual(layer.model_version(), ver)
        self.log_judgement("重复注册同名数据集",
                           "dataset_already_exists、版本不变", f"ver={ver}")


class ConcurrentVersionTest(LoggingTestCase):
    def test_concurrent_writers_and_readers_only_see_complete_versions(self) -> None:
        layer = MetricLayer(max_history=100)
        layer.register_dataset(
            "t", [{"id": i, "g": "A", "v": i} for i in range(1, 51)],
            primary_key=("id",))
        layer.add_dimension(DimensionSpec("g", "t", "g"))
        layer.add_measure(MeasureSpec("s", "t", AggKind.SUM, "v"))
        layer.add_measure(MeasureSpec("c", "t", AggKind.COUNT))

        errors: list[Exception] = []
        observed: dict[int, tuple] = {}  # 每个被观察版本 -> (sum, count)

        def expected(total_rows: int) -> tuple:
            # v = 1..50 时 sum = 1+...+n；行数 n
            return (sum(range(1, total_rows + 1)), total_rows)

        stop = threading.Event()

        def writer() -> None:
            try:
                n = 50
                while not stop.is_set():
                    n = 20 if n == 50 else 50  # 在 20/50 行两个完整状态间切换
                    layer.replace_dataset_data(
                        "t", [{"id": i, "g": "A", "v": i} for i in range(1, n + 1)])
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        def reader_current() -> None:
            try:
                for _ in range(200):
                    res = layer.query(Query(dimensions=("g",), measures=("s", "c")))
                    key = (res.rows[0][1], res.rows[0][2])
                    # 只能是 20 行版本或 50 行版本的完整结果，不可能是中间值
                    if key not in (expected(20), expected(50)):
                        errors.append(AssertionError(f"观察到非完整版本结果 {key}"))
                    observed[res.model_version] = key
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        def reader_history() -> None:
            try:
                for _ in range(100):
                    vers = layer.history_versions()
                    if not vers:
                        continue
                    v = vers[len(vers) // 2]  # 任选一个保留中的历史版本
                    res = layer.query(
                        Query(dimensions=("g",), measures=("s", "c")), at_version=v)
                    # 历史结果必须自标历史版本，且该版本快照不可变 -> 结果恒定
                    assert res.model_version == v
                    if v in observed:
                        assert observed[v] == (res.rows[0][1], res.rows[0][2])
                    observed[v] = (res.rows[0][1], res.rows[0][2])
            except VersionEvictedError:
                # 与淘汰并发时可能恰好被淘汰：这是明确错误，不允许用当前顶替
                pass
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        tw = threading.Thread(target=writer)
        tw.start()
        threads = [threading.Thread(target=reader_current) for _ in range(4)]
        threads += [threading.Thread(target=reader_history) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        stop.set()
        tw.join()

        self.log_judgement(
            "1 写线程在 20/50 行间切换 + 4 当前读 + 2 历史读",
            "无异常；每个版本的 (sum,count) 与其行数自洽，无半更新结果",
            f"errors={errors}, 观察到 {len(observed)} 个版本")
        self.assertEqual(errors, [])
        self.assertTrue(observed)
        for version, key in observed.items():
            s, cnt = key
            self.assertEqual(s, cnt * (cnt + 1) // 2, f"版本 {version} 结果不自洽 {key}")
            self.assertIn(cnt, (20, 50))

    def test_failed_write_during_concurrent_reads_never_visible(self) -> None:
        layer = MetricLayer()
        layer.register_dataset(
            "t", [{"id": 1, "g": "A", "v": 1}], primary_key=("id",))
        layer.add_dimension(DimensionSpec("g", "t", "g"))
        layer.add_measure(MeasureSpec("c", "t", AggKind.COUNT))
        errors: list[Exception] = []
        seen_counts: set[int] = set()
        stop = threading.Event()

        def bad_writer() -> None:
            try:
                while not stop.is_set():
                    try:
                        # 删列：永久失败的发布，绝不允许出现「版本前进但无 g 列」
                        layer.replace_dataset_data("t", [{"id": 1}])
                    except SourceUpdateError:
                        pass
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        def reader() -> None:
            try:
                for _ in range(200):
                    res = layer.query(Query(dimensions=("g",), measures=("c",)))
                    seen_counts.add(res.rows[0][1])
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        tb = threading.Thread(target=bad_writer)
        tb.start()
        threads = [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        stop.set()
        tb.join()
        self.log_judgement("持续失败发布 + 4 读线程",
                           "读到的行数恒为 1，无异常", f"counts={seen_counts}")
        self.assertEqual(errors, [])
        self.assertEqual(seen_counts, {1})
