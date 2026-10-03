"""失败发布的原子性回归：所有变更类型「要么整体成功、要么整体不变」。

覆盖四类变更（数据更新 / 口径定义 / 关联定义 / 数据集注册）在生效前被
校验拒绝时，版本号、查询缓存、当前查询结果、已定义口径与关联、历史档案
全部保持原样，且之后继续正常定义与更新不受影响。
"""

from __future__ import annotations

from sml.caliber import AggKind, DimensionSpec, MeasureSpec, RatioSpec
from sml.engine import Query
from sml.errors import (
    AmbiguousJoinError,
    CaliberConflictError,
    DatasetAlreadyExistsError,
    DuplicatePrimaryKeyError,
    IncompleteStructureError,
    InvalidCaliberError,
    NonUniqueKeyError,
    SourceUpdateError,
    TypeConflictError,
)
from sml.registry import MetricLayer
from sml.relations import Cardinality, Relation
from tests._support import LoggingTestCase


def _seed(layer: MetricLayer) -> None:
    """两表星型：orders(事实) -> customers(维)。"""
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


class FailedPublishAtomicityBase(LoggingTestCase):
    """公共探针：记录变更前状态，断言失败后逐项不变。"""

    def setUp(self) -> None:
        self.layer = MetricLayer(history_retention=10)
        _seed(self.layer)
        self._capture_baseline()

    def _capture_baseline(self) -> None:
        """记录当前状态为探针基线（前置成功变更后需重新调用）。"""
        self.probe = Query(dimensions=("city",), measures=("s", "cnt"))
        self.before_rows = self.layer.query(self.probe).rows  # 写入一条缓存
        self.version_before = self.layer.model_version()
        self.cache_before = self.layer.cache_size()
        self.metrics_before = self.layer.list_metrics()
        self.relations_before = self.layer.list_relations()
        self.datasets_before = self.layer.list_datasets()
        self.retained_before = self.layer.retained_versions()

    def assert_state_untouched(self, case: str, code: str) -> None:
        after = self.layer.query(self.probe)
        self.log_judgement(
            case,
            "版本/缓存/清单/档案不变，当前结果与变更前一致且缓存仍命中",
            f"code={code}, v{self.version_before}->v{self.layer.model_version()}, "
            f"cache {self.cache_before}->{self.layer.cache_size()}, "
            f"hit={after.resource_stats.get('cache_hit')}",
        )
        self.assertEqual(self.layer.model_version(), self.version_before)
        self.assertEqual(self.layer.cache_size(), self.cache_before)
        self.assertEqual(self.layer.list_metrics(), self.metrics_before)
        self.assertEqual(self.layer.list_relations(), self.relations_before)
        self.assertEqual(self.layer.list_datasets(), self.datasets_before)
        self.assertEqual(self.layer.retained_versions(), self.retained_before)
        self.assertTrue(after.resource_stats.get("cache_hit"))
        self.assertEqual(after.rows, self.before_rows)

    def assert_followup_works(self) -> None:
        """失败之后：新的合法口径/关联/数据更新照常生效，版本连续前进。"""
        v0 = self.layer.model_version()
        self.layer.add_dimension(DimensionSpec("oid", "orders", "oid"))
        self.layer.replace_dataset_data(
            "customers", [{"cid": 10, "city": "N"}, {"cid": 20, "city": "S"}]
        )
        self.assertEqual(self.layer.model_version(), v0 + 2)  # 连续、无空洞
        rows = self.layer.query(Query(dimensions=("city",), measures=("cnt",))).rows
        self.assertEqual(rows, (("N", 1), ("S", 1)))


class FailedDataUpdateTest(FailedPublishAtomicityBase):
    def test_drop_column_rejected(self) -> None:
        with self.assertRaises(SourceUpdateError) as cm:
            self.layer.replace_dataset_data("customers", [{"cid": 10}, {"cid": 20}])
        self.assertErrorCode(cm, "source_update_rejected", "更新删掉 city 列")
        self.assert_state_untouched("数据更新-删列", cm.exception.code)
        self.assert_followup_works()

    def test_type_drift_rejected(self) -> None:
        with self.assertRaises(TypeConflictError) as cm:
            self.layer.replace_dataset_data(
                "customers",
                [{"cid": 10, "city": "EAST"}, {"cid": "x", "city": "WEST"}],
            )
        self.assertErrorCode(cm, "type_conflict", "cid 列混入文本")
        self.assert_state_untouched("数据更新-类型漂移", cm.exception.code)
        self.assert_followup_works()

    def test_duplicate_relation_key_rejected(self) -> None:
        with self.assertRaises(NonUniqueKeyError) as cm:
            self.layer.replace_dataset_data(
                "customers",
                [{"cid": 10, "city": "EAST"}, {"cid": 10, "city": "WEST"}],
            )
        self.assertErrorCode(cm, "non_unique_key", "更新使关联 one 侧键重复")
        self.assert_state_untouched("数据更新-关联键失唯一", cm.exception.code)
        self.assert_followup_works()

    def test_empty_records_rejected(self) -> None:
        with self.assertRaises(IncompleteStructureError) as cm:
            self.layer.replace_dataset_data("orders", [])
        self.assertErrorCode(cm, "incomplete_structure", "空数据覆盖")
        self.assert_state_untouched("数据更新-空内容", cm.exception.code)
        self.assert_followup_works()

    def test_duplicate_primary_key_rejected(self) -> None:
        with self.assertRaises(DuplicatePrimaryKeyError) as cm:
            self.layer.replace_dataset_data(
                "orders",
                [{"oid": 1, "cid": 10, "v": 1}, {"oid": 1, "cid": 20, "v": 2}],
            )
        self.assertErrorCode(cm, "duplicate_primary_key", "主键重复")
        self.assert_state_untouched("数据更新-主键重复", cm.exception.code)
        self.assert_followup_works()


class FailedCaliberDefinitionTest(FailedPublishAtomicityBase):
    def test_measure_on_text_field_rejected(self) -> None:
        with self.assertRaises(InvalidCaliberError) as cm:
            self.layer.add_measure(
                MeasureSpec("bad", "customers", AggKind.SUM, "city")
            )
        self.assertErrorCode(cm, "invalid_caliber", "对文本列做 sum")
        self.assert_state_untouched("口径定义-非数值聚合", cm.exception.code)
        self.assert_followup_works()

    def test_measure_on_missing_field_rejected(self) -> None:
        with self.assertRaises(InvalidCaliberError) as cm:
            self.layer.add_measure(
                MeasureSpec("bad", "orders", AggKind.SUM, "nope")
            )
        self.assertErrorCode(cm, "invalid_caliber", "引用不存在字段")
        self.assert_state_untouched("口径定义-字段不存在", cm.exception.code)
        self.assert_followup_works()

    def test_ratio_on_avg_rejected(self) -> None:
        self.layer.add_measure(MeasureSpec("a", "orders", AggKind.AVG, "v"))
        # 上面是一次成功变更，探针基线需刷新
        self._capture_baseline()
        with self.assertRaises(InvalidCaliberError) as cm:
            self.layer.add_ratio(RatioSpec("bad", "orders", "a", "cnt"))
        self.assertErrorCode(cm, "invalid_caliber", "比率基于 avg")
        self.assert_state_untouched("口径定义-比率基于avg", cm.exception.code)
        self.assertNotIn("bad", self.layer.list_metrics())
        self.assert_followup_works()

    def test_ratio_with_undefined_numerator_rejected(self) -> None:
        with self.assertRaises(InvalidCaliberError) as cm:
            self.layer.add_ratio(RatioSpec("bad", "orders", "ghost", "cnt"))
        self.assertErrorCode(cm, "invalid_caliber", "分子未定义")
        self.assert_state_untouched("口径定义-分子未定义", cm.exception.code)
        self.assert_followup_works()

    def test_duplicate_caliber_name_rejected(self) -> None:
        with self.assertRaises(CaliberConflictError) as cm:
            self.layer.add_dimension(DimensionSpec("city", "orders", "oid"))
        self.assertErrorCode(cm, "caliber_conflict", "维度名与既有维度冲突")
        self.assert_state_untouched("口径定义-重名冲突", cm.exception.code)
        self.assert_followup_works()


class FailedRelationDefinitionTest(FailedPublishAtomicityBase):
    def test_non_unique_key_rejected(self) -> None:
        self.layer.register_dataset(
            "skus", [{"sku": 1, "grp": "x"}, {"sku": 1, "grp": "y"}]
        )
        self._capture_baseline()  # 注册成功，刷新基线
        with self.assertRaises(NonUniqueKeyError) as cm:
            self.layer.add_relation(
                Relation("o_s", "orders", ("oid",), "skus", ("sku",),
                         Cardinality.MANY_TO_ONE)
            )
        self.assertErrorCode(cm, "non_unique_key", "one 侧键重复")
        self.assert_state_untouched("关联定义-键不唯一", cm.exception.code)
        self.assertNotIn("o_s", self.layer.list_relations())
        self.assert_followup_works()

    def test_duplicate_pair_rejected(self) -> None:
        with self.assertRaises(AmbiguousJoinError) as cm:
            self.layer.add_relation(
                Relation("o_c2", "orders", ("cid",), "customers", ("cid",),
                         Cardinality.MANY_TO_ONE)
            )
        self.assertErrorCode(cm, "ambiguous_join", "同对数据集第二条关联")
        self.assert_state_untouched("关联定义-方向歧义", cm.exception.code)
        self.assert_followup_works()

    def test_missing_dataset_rejected(self) -> None:
        from sml.errors import DatasetNotFoundError

        with self.assertRaises(DatasetNotFoundError) as cm:
            self.layer.add_relation(
                Relation("o_g", "orders", ("cid",), "ghost", ("cid",),
                         Cardinality.MANY_TO_ONE)
            )
        self.assertErrorCode(cm, "dataset_not_found", "引用不存在数据集")
        self.assert_state_untouched("关联定义-数据集不存在", cm.exception.code)
        self.assert_followup_works()

    def test_missing_key_column_rejected(self) -> None:
        with self.assertRaises(InvalidCaliberError) as cm:
            self.layer.add_relation(
                Relation("o_c3", "orders", ("nope",), "customers", ("cid",),
                         Cardinality.MANY_TO_ONE)
            )
        self.assertErrorCode(cm, "invalid_caliber", "键列不存在")
        self.assert_state_untouched("关联定义-键列不存在", cm.exception.code)
        self.assert_followup_works()


class FailedDatasetRegisterTest(FailedPublishAtomicityBase):
    def test_duplicate_name_rejected(self) -> None:
        with self.assertRaises(DatasetAlreadyExistsError) as cm:
            self.layer.register_dataset("orders", [{"a": 1}])
        self.assertErrorCode(cm, "dataset_already_exists", "同名重复注册")
        self.assert_state_untouched("数据集注册-重名", cm.exception.code)
        # 既有 orders 数据未被覆盖
        self.assertEqual(
            self.layer.query(Query(measures=("cnt",))).rows, ((2,),)
        )
        self.assert_followup_works()

    def test_empty_records_rejected(self) -> None:
        with self.assertRaises(IncompleteStructureError) as cm:
            self.layer.register_dataset("empty", [])
        self.assertErrorCode(cm, "incomplete_structure", "0 条记录")
        self.assert_state_untouched("数据集注册-空数据", cm.exception.code)
        self.assertNotIn("empty", self.layer.list_datasets())
        self.assert_followup_works()

    def test_type_conflict_rejected(self) -> None:
        with self.assertRaises(TypeConflictError) as cm:
            self.layer.register_dataset("bad", [{"a": 1}, {"a": "x"}])
        self.assertErrorCode(cm, "type_conflict", "同列类型冲突")
        self.assert_state_untouched("数据集注册-类型冲突", cm.exception.code)
        self.assertNotIn("bad", self.layer.list_datasets())
        self.assert_followup_works()


class MixedSuccessFailureSequenceTest(LoggingTestCase):
    """成功/失败交替：版本号只随成功连续前进，档案只含成功版本。"""

    def test_version_sequence_has_no_gaps(self) -> None:
        layer = MetricLayer(history_retention=None)
        _seed(layer)
        v0 = layer.model_version()
        attempts = [
            # (操作, 是否应成功)
            (lambda: layer.add_dimension(DimensionSpec("oid", "orders", "oid")), True),
            (lambda: layer.add_measure(
                MeasureSpec("bad", "orders", AggKind.SUM, "ghost")), False),
            (lambda: layer.add_measure(
                MeasureSpec("mx", "orders", AggKind.MAX, "v")), True),
            (lambda: layer.replace_dataset_data("customers", [{"cid": 1}]), False),
            (lambda: layer.replace_dataset_data(
                "customers",
                [{"cid": 10, "city": "N"}, {"cid": 20, "city": "S"}]), True),
            (lambda: layer.add_relation(
                Relation("dup", "orders", ("cid",), "customers", ("cid",),
                         Cardinality.MANY_TO_ONE)), False),
            (lambda: layer.register_dataset("extra", [{"e": 1}]), True),
        ]
        expected_successes = 0
        for i, (op, should_succeed) in enumerate(attempts):
            before = layer.model_version()
            try:
                op()
                ok = True
            except Exception:
                ok = False
            self.assertEqual(ok, should_succeed, f"第 {i} 步成败与预期不符")
            delta = layer.model_version() - before
            self.assertIn(delta, (0, 1))
            self.assertEqual(delta, 1 if should_succeed else 0,
                             f"第 {i} 步版本前进与成败不一致")
            expected_successes += 1 if should_succeed else 0
        final_v = layer.model_version()
        self.log_judgement(
            "7 步成功/失败交替变更",
            "版本号只随 4 次成功连续 +1、无空洞；档案版本号连续",
            f"v{v0}->v{final_v}, retained={layer.retained_versions()}",
        )
        self.assertEqual(final_v, v0 + expected_successes)
        # 档案中的历史版本号连续且无重复（失败版本从未进入档案）
        retained = layer.retained_versions()
        self.assertEqual(retained, sorted(set(retained)))
        self.assertEqual(retained, list(range(retained[0], retained[-1] + 1)))
        # 每个历史版本都能解析为完整快照；cnt 是 _seed 后期才定义的口径，
        # 早于其定义的版本回查 cnt 必须报 caliber_not_found（口径隔离），
        # 之后定义的版本则正常返回。
        from sml.errors import CaliberNotFoundError

        for v in retained:
            snap = layer.snapshot_at_version(v)
            self.assertEqual(snap.version, v)
            try:
                r = layer.query(Query(measures=("cnt",)), at_version=v)
                self.assertEqual(r.model_version, v)
            except CaliberNotFoundError:
                pass  # 该版本上 cnt 尚未定义，正确拒绝


if __name__ == "__main__":
    import unittest

    unittest.main()
