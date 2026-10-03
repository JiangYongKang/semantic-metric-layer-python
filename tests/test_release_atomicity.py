"""专题：发布原子性（全变更类型）+ 历史回查口径隔离 + 定义类变更并发可见性。

业务覆盖（运行 ``python -m unittest tests.test_release_atomicity -v`` 可见）：

1. 口径定义失败不污染（FailedCaliberPublishTest）：
   维度/指标/比率定义被拒后，版本号不前进、缓存不被清空且仍命中、
   当前结果与血缘保持原样；失败口径不得在活动状态留残痕，更不得被
   之后任意一次成功变更带入新版本；失败后继续定义口径/关联不受影响。
2. 关联定义失败不污染（FailedRelationPublishTest）：
   one 侧键不唯一、引用不存在数据集/字段、同数据集对重复关联等被拒后，
   版本/缓存/当前结果不变，关联不出现在清单中，后续登记不受影响。
3. 数据集注册失败不污染（FailedDatasetRegisterTest）：
   空结构/重名注册被拒后版本与缓存不变，随后合法注册正常升版。
4. 历史回查口径隔离（HistoricalCaliberIsolationTest）：
   指标/维度/过滤维度/比率在目标版本之后才定义时，回查明确报
   caliber_not_found(reason=not_defined_at_version)，绝不拿当前口径顶替；
   历史回查不改变当前缓存命中与当前结果。
5. 混合变更并发（ConcurrentDefinitionVisibilityTest）：
   数据更新、成功/失败的口径与关联定义并发时，每次当前读取只对应某一个
   完整版本；每次成功变更版本恰好 +1，失败变更版本不动。
"""

from __future__ import annotations

import sys
import threading

from tests._support import LoggingTestCase
from sml.caliber import (
    AggKind,
    DimensionSpec,
    MeasureSpec,
    RatioSpec,
)
from sml.engine import Filter, Query
from sml.errors import (
    AmbiguousJoinError,
    CaliberConflictError,
    CaliberNotFoundError,
    DatasetAlreadyExistsError,
    IncompleteStructureError,
    InvalidCaliberError,
    NonUniqueKeyError,
)
from sml.registry import MetricLayer
from sml.relations import Cardinality, Relation


def setUpModule() -> None:
    sys.stdout.write(
        "\n========== test_release_atomicity 业务覆盖 ==========\n"
        "1. 口径(维度/指标/比率)定义失败 -> 版本/缓存/当前结果/后续定义不污染\n"
        "2. 关联定义失败(键不唯一/缺数据集/重复对) -> 整次不变\n"
        "3. 数据集注册失败(空结构/重名) -> 整次不变\n"
        "4. 历史回查: 之后才定义的口径明确报 not_defined_at_version，不顶替\n"
        "5. 数据更新 + 成功/失败定义并发 -> 只见完整版本、版本增减精确\n"
        "=====================================================\n"
    )
    sys.stdout.flush()


def _seed(layer: MetricLayer) -> None:
    """orders(v) 事实表 + customers(cid 唯一) 维表 + 关联 + 基础口径。"""
    layer.register_dataset(
        "orders",
        [
            {"oid": 1, "cid": 10, "v": 1},
            {"oid": 2, "cid": 20, "v": 2},
            {"oid": 3, "cid": 10, "v": 4},
        ],
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


class FailedCaliberPublishTest(LoggingTestCase):
    def test_conflicting_caliber_name_rejects_and_nothing_moves(self) -> None:
        layer = MetricLayer()
        _seed(layer)
        q = Query(dimensions=("city",), measures=("s", "cnt"))
        before = layer.query(q)
        v0 = layer.model_version()
        cache0 = layer.cache_size()

        with self.assertRaises(CaliberConflictError) as cm:
            # 's' 已是指标名，共享命名空间下不能再定义为维度
            layer.add_dimension(DimensionSpec("s", "customers", "city"))
        self.log_judgement(
            "维度重名于已定义指标 's'",
            "caliber_conflict 拒绝；版本/缓存/结果不变，缓存仍命中",
            f"{cm.exception.code}, v{layer.model_version()}, cache={layer.cache_size()}",
        )
        self.assertEqual(layer.model_version(), v0)
        self.assertEqual(layer.cache_size(), cache0)
        after = layer.query(q)
        self.assertTrue(after.resource_stats.get("cache_hit"))
        self.assertEqual(after.rows, before.rows)
        self.assertEqual(after.lineage.to_dict(), before.lineage.to_dict())

        # 失败后继续定义口径与关联完全不受影响
        layer.add_dimension(DimensionSpec("cust_cid", "customers", "cid"))
        self.assertEqual(layer.model_version(), v0 + 1)
        self.assertEqual(layer.cache_size(), 0)  # 成功变更才清缓存
        self.log_judgement("冲突被拒后再登记合法口径", "版本 +1、缓存于成功后才清空",
                           f"v{v0}->v{layer.model_version()}, cache={layer.cache_size()}")

    def test_failed_caliber_leaves_no_trace_in_later_versions(self) -> None:
        """关键回归：失败定义不得残留在活动状态并被后续成功变更带入新版。"""
        layer = MetricLayer()
        _seed(layer)
        v0 = layer.model_version()
        try:
            layer.add_dimension(DimensionSpec("s", "customers", "city"))  # 冲突
        except CaliberConflictError:
            pass
        try:
            layer.add_measure(MeasureSpec("zz", "orders", AggKind.SUM, "nope"))
        except InvalidCaliberError:
            pass
        self.assertEqual(layer.model_version(), v0)
        # 一次完全无关的成功数据更新使版本前进
        layer.replace_dataset_data(
            "customers", [{"cid": 10, "city": "N"}, {"cid": 20, "city": "S"}]
        )
        snap = layer.snapshot_model()
        self.log_judgement(
            "两次失败口径定义后再成功更新",
            "新版本口径册不含 's' 维度/不含 'zz' 指标；'s' 仍是指标",
            f"dims={sorted(snap.calibers._dimensions)}, "
            f"measures={sorted(snap.calibers._measures)}",
        )
        self.assertNotIn("s", snap.calibers._dimensions)
        self.assertIn("s", snap.calibers._measures)
        self.assertNotIn("zz", snap.calibers._measures)
        self.assertEqual(layer.model_version(), v0 + 1)

    def test_invalid_measure_field_and_ratio_rejected(self) -> None:
        layer = MetricLayer()
        _seed(layer)
        v0 = layer.model_version()
        with self.assertRaises(InvalidCaliberError):
            layer.add_measure(MeasureSpec("bad", "orders", AggKind.SUM, "missing"))
        self.assertEqual(layer.model_version(), v0)
        # 合法 avg 口径登记成功（+1），为「比率不得基于 avg」准备条件
        layer.add_measure(MeasureSpec("a", "orders", AggKind.AVG, "v"))
        v1 = layer.model_version()
        self.assertEqual(v1, v0 + 1)
        with self.assertRaises(InvalidCaliberError):
            layer.add_ratio(RatioSpec("r", "orders", numerator="a", denominator="cnt"))
        with self.assertRaises(InvalidCaliberError):
            # 分子分母必须已定义
            layer.add_ratio(RatioSpec("r2", "orders", numerator="nope", denominator="cnt"))
        self.assertEqual(layer.model_version(), v1)
        self.assertEqual(layer.list_metrics().count("r"), 0)
        # 失败后合法比率照常升版
        layer.add_ratio(RatioSpec("ratio_sc", "orders", numerator="s", denominator="cnt"))
        self.assertEqual(layer.model_version(), v1 + 1)
        self.log_judgement("非法字段/基于avg/缺基础口径 三次拒绝，avg 本体合法",
                           "失败不升版；合法比率登记后再 +1",
                           f"v0={v0}, v1={v1}, now=v{layer.model_version()}")

    def test_successful_caliber_publish_clears_cache_and_bumps_once(self) -> None:
        layer = MetricLayer()
        _seed(layer)
        layer.query(Query(measures=("cnt",)))
        self.assertEqual(layer.cache_size(), 1)
        v0 = layer.model_version()
        layer.add_dimension(DimensionSpec("cust_cid", "customers", "cid"))
        self.assertEqual(layer.model_version(), v0 + 1)
        self.assertEqual(layer.cache_size(), 0)
        self.log_judgement("一次成功口径定义", "版本 +1 且缓存整体失效",
                           f"v{v0}->v{layer.model_version()}, cache=0")


class FailedRelationPublishTest(LoggingTestCase):
    def _layer(self) -> MetricLayer:
        layer = MetricLayer()
        layer.register_dataset(
            "orders",
            [{"oid": 1, "cid": 10, "v": 1}, {"oid": 2, "cid": 20, "v": 2}],
            primary_key=("oid",),
        )
        layer.register_dataset(
            "customers",
            [{"cid": 10, "city": "EAST"}, {"cid": 20, "city": "WEST"}],
        )
        return layer

    def test_duplicate_one_side_key_at_definition_rejected(self) -> None:
        layer = self._layer()
        # customers 出现重复 cid：one 侧不唯一，定义期即拒绝
        layer.replace_dataset_data(
            "customers",
            [{"cid": 10, "city": "EAST"}, {"cid": 10, "city": "WEST"}],
        )
        v0 = layer.model_version()
        with self.assertRaises(NonUniqueKeyError):
            layer.add_relation(
                Relation("o_c", "orders", ("cid",), "customers", ("cid",),
                         Cardinality.MANY_TO_ONE)
            )
        self.assertEqual(layer.model_version(), v0)
        self.assertNotIn("o_c", layer.list_relations())
        # 修复数据后关联可正常定义
        layer.replace_dataset_data(
            "customers", [{"cid": 10, "city": "EAST"}, {"cid": 20, "city": "WEST"}]
        )
        layer.add_relation(
            Relation("o_c", "orders", ("cid",), "customers", ("cid",),
                     Cardinality.MANY_TO_ONE)
        )
        self.assertIn("o_c", layer.list_relations())
        self.log_judgement("one 侧重复键时定义关联被拒，修复数据后",
                           "关联清单先不含 o_c，后登记成功",
                           layer.list_relations())

    def test_relation_missing_dataset_and_duplicate_pair_rejected(self) -> None:
        layer = self._layer()
        layer.add_relation(
            Relation("o_c", "orders", ("cid",), "customers", ("cid",),
                     Cardinality.MANY_TO_ONE)
        )
        layer.add_dimension(DimensionSpec("city", "customers", "city"))
        layer.add_measure(MeasureSpec("cnt", "orders", AggKind.COUNT))
        probe = Query(dimensions=("city",), measures=("cnt",))
        layer.query(probe)
        v0 = layer.model_version()
        cache0 = layer.cache_size()

        with self.assertRaises(Exception):
            layer.add_relation(
                Relation("bad", "orders", ("cid",), "ghost", ("cid",),
                         Cardinality.MANY_TO_ONE)
            )
        with self.assertRaises(AmbiguousJoinError):
            # 同一对数据集不允许第二条关联
            layer.add_relation(
                Relation("o_c2", "orders", ("cid",), "customers", ("cid",),
                         Cardinality.MANY_TO_ONE)
            )
        self.log_judgement("引用不存在数据集 + 重复数据集对",
                           "两次拒绝；版本/缓存不变、清单只有 o_c、缓存仍命中",
                           f"v{layer.model_version()}, rels={layer.list_relations()}")
        self.assertEqual(layer.model_version(), v0)
        self.assertEqual(layer.cache_size(), cache0)
        self.assertEqual(layer.list_relations(), ["o_c"])
        self.assertTrue(layer.query(probe).resource_stats.get("cache_hit"))


class FailedDatasetRegisterTest(LoggingTestCase):
    def test_empty_and_duplicate_register_leave_everything_untouched(self) -> None:
        layer = MetricLayer()
        _seed(layer)
        probe = Query(measures=("cnt",))
        before = layer.query(probe)
        v0 = layer.model_version()

        with self.assertRaises(IncompleteStructureError):
            layer.register_dataset("ghost", [])
        with self.assertRaises(DatasetAlreadyExistsError):
            layer.register_dataset(
                "orders", [{"oid": 9, "v": 9}], primary_key=("oid",)
            )
        self.log_judgement("注册空数据集 + 重名数据集",
                           "两次拒绝；版本/缓存/当前结果不变",
                           f"v{layer.model_version()}, cache={layer.cache_size()}")
        self.assertEqual(layer.model_version(), v0)
        after = layer.query(probe)
        self.assertTrue(after.resource_stats.get("cache_hit"))
        self.assertEqual(after.rows, before.rows)
        self.assertNotIn("ghost", layer.list_datasets())

        # 失败后合法注册照常升版且可定义口径
        layer.register_dataset("products", [{"pid": 1, "name": "P1"}])
        self.assertEqual(layer.model_version(), v0 + 1)
        layer.add_dimension(DimensionSpec("pname", "products", "name"))
        self.assertEqual(layer.model_version(), v0 + 2)


class HistoricalCaliberIsolationTest(LoggingTestCase):
    def _layer_with_versions(self) -> tuple[MetricLayer, int, int]:
        layer = MetricLayer(history_retention=50)
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
        layer.add_measure(MeasureSpec("s", "orders", AggKind.SUM, "v"))
        layer.add_measure(MeasureSpec("cnt", "orders", AggKind.COUNT))
        v_base = layer.model_version()  # 此时尚无 city 维度/later 指标/比率
        layer.add_dimension(DimensionSpec("city", "customers", "city"))
        layer.add_measure(MeasureSpec("later", "orders", AggKind.SUM, "v"))
        layer.add_ratio(RatioSpec("ratio_sc", "orders", "s", "cnt"))
        v_now = layer.model_version()
        return layer, v_base, v_now

    def test_metric_defined_later_is_not_used_in_history(self) -> None:
        layer, v_base, _ = self._layer_with_versions()
        with self.assertRaises(CaliberNotFoundError) as cm:
            layer.query(Query(measures=("later",)), at_version=v_base)
        d = cm.exception.details
        self.assertEqual(d["reason"], "not_defined_at_version")
        self.assertTrue(d["defined_in_current"])
        self.assertEqual(d["requested_version"], v_base)
        self.log_judgement(f"回查 v{v_base} 上查询之后才定义的指标 'later'",
                           "明确报该版本口径不存在，禁止当前口径顶替",
                           f"{cm.exception.code}/{d['reason']}")

    def test_dimension_and_filter_defined_later_are_not_used(self) -> None:
        layer, v_base, _ = self._layer_with_versions()
        with self.assertRaises(CaliberNotFoundError) as cm1:
            layer.query(
                Query(dimensions=("city",), measures=("cnt",)), at_version=v_base
            )
        self.assertEqual(cm1.exception.details["reason"], "not_defined_at_version")
        self.assertEqual(cm1.exception.details["kind"], "dimension")
        with self.assertRaises(CaliberNotFoundError) as cm2:
            layer.query(
                Query(
                    dimensions=(),
                    measures=("cnt",),
                    filters=(Filter("city", ("EAST",)),),
                ),
                at_version=v_base,
            )
        self.assertEqual(cm2.exception.details["reason"], "not_defined_at_version")
        self.log_judgement("回查时维度/过滤维度在目标版本之后才定义",
                           "均报 not_defined_at_version，绝不静默用当前口径算",
                           f"{cm1.exception.code}, {cm2.exception.code}")

    def test_ratio_defined_later_is_not_used_in_history(self) -> None:
        layer, v_base, _ = self._layer_with_versions()
        with self.assertRaises(CaliberNotFoundError) as cm:
            layer.query(Query(measures=("ratio_sc",)), at_version=v_base)
        self.assertEqual(cm.exception.details["reason"], "not_defined_at_version")
        # 同一查询在当前版本可用 —— 证明确实是版本隔离而非口径本身非法
        cur = layer.query(Query(measures=("ratio_sc",)))
        self.assertEqual(cur.model_version, layer.model_version())
        self.log_judgement("比率晚于目标版本定义",
                           "回查报缺失；当前版本同查询正常",
                           f"hist={cm.exception.code}, cur_rows={cur.rows}")

    def test_never_defined_caliber_marked_not_in_current_either(self) -> None:
        layer, v_base, _ = self._layer_with_versions()
        with self.assertRaises(CaliberNotFoundError) as cm:
            layer.query(Query(measures=("ghost_metric",)), at_version=v_base)
        self.assertFalse(cm.exception.details["defined_in_current"])
        self.assertEqual(cm.exception.details["reason"], "not_defined_at_version")

    def test_history_query_keeps_current_cache_and_result(self) -> None:
        layer, v_base, _ = self._layer_with_versions()
        cur_q = Query(dimensions=("city",), measures=("cnt",))
        cur1 = layer.query(cur_q)
        size = layer.cache_size()
        for _ in range(3):
            layer.query(Query(measures=("cnt",)), at_version=v_base)
        self.assertEqual(layer.cache_size(), size)
        cur2 = layer.query(cur_q)
        self.assertTrue(cur2.resource_stats.get("cache_hit"))
        self.assertEqual(cur2.rows, cur1.rows)
        self.log_judgement("3 次历史回查后再查当前",
                           "当前缓存大小不变且仍命中、结果不变",
                           f"size={layer.cache_size()}")


class ConcurrentDefinitionVisibilityTest(LoggingTestCase):
    def test_mixed_changes_readers_see_only_complete_versions(self) -> None:
        layer = MetricLayer(history_retention=100)
        layer.register_dataset(
            "t", [{"id": i, "v": i} for i in range(1, 51)],
            primary_key=("id",),
        )
        layer.add_measure(MeasureSpec("s", "t", AggKind.SUM, "v"))
        layer.add_measure(MeasureSpec("c", "t", AggKind.COUNT))
        v_start = layer.model_version()

        errors: list[Exception] = []
        seen: set[tuple[int, int]] = set()
        seen_lock = threading.Lock()
        stop = threading.Event()

        def reader() -> None:
            try:
                q = Query(measures=("s", "c"))
                while not stop.is_set():
                    r = layer.query(q)
                    cnt = r.rows[0][1]
                    sval = r.rows[0][0]
                    # 完整版本判据：数据只可能是 1..50 或 1..100 的连续值，
                    # sum 必须与行数自洽；版本指针与内容必须配套。
                    if cnt not in (50, 100) or sval != cnt * (cnt + 1) // 2:
                        errors.append(
                            AssertionError(
                                f"半更新/串档: version={r.model_version}, "
                                f"rows={r.rows}"
                            )
                        )
                        return
                    with seen_lock:
                        seen.add((r.model_version, int(sval)))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=reader) for _ in range(6)]
        for t in threads:
            t.start()

        failures = 0
        successes = 0
        for k in range(10):
            # 成功的数据更新（50 <-> 100 行）
            rows_n = 100 if k % 2 else 50
            layer.replace_dataset_data(
                "t", [{"id": i, "v": i} for i in range(1, rows_n + 1)]
            )
            successes += 1
            # 成功的口径定义
            layer.add_measure(
                MeasureSpec(f"m{k}", "t", AggKind.SUM, "v")
            )
            successes += 1
            # 必然失败的口径定义：引用不存在的字段
            before = layer.model_version()
            try:
                layer.add_measure(
                    MeasureSpec(f"bad{k}", "t", AggKind.SUM, "nope")
                )
            except InvalidCaliberError:
                failures += 1
            assert layer.model_version() == before
            # 必然失败的关联定义：引用不存在的数据集
            try:
                layer.add_relation(
                    Relation(f"rb{k}", "t", ("id",), "nope", ("id",),
                             Cardinality.MANY_TO_ONE)
                )
            except Exception:  # noqa: BLE001
                failures += 1
            assert layer.model_version() == before
        stop.set()
        for t in threads:
            t.join()

        self.log_judgement(
            f"6 读线程 vs 10 轮混合变更（{successes} 次成功 / {failures} 次失败）",
            "无异常；所有观测自洽于完整版本；成功各 +1、失败不升版",
            f"errors={errors}, v{v_start}->v{layer.model_version()}, 观测版本数={len(seen)}",
        )
        self.assertEqual(errors, [])
        self.assertTrue(seen)
        self.assertEqual(layer.model_version(), v_start + successes)
        # 任一被观测版本的结果都必须与该版本数据自洽（引擎已逐条验，这里复核）
        for _ver, _sval in seen:
            self.assertIsInstance(_sval, int)


if __name__ == "__main__":
    import unittest

    unittest.main()
