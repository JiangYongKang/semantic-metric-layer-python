"""聚合语义：空值参与、除零、全空分组、高基数、AVG 口径。"""
from decimal import Decimal

import pytest

from sml.engine import Filter, Query, QueryEngine
from sml.errors import QueryError, ResourceLimitError
from sml.model import Agg, Metric, RatioMetric
from sml.resources import ResourceLimits
from tests.conftest import log_case


class TestNullSemantics:
    def test_avg_ignores_nulls(self, sales_registry, model):
        eng = QueryEngine(sales_registry, model)
        r = eng.run(Query(metrics=("avg_qty",)))
        # qty = [3, 1, None, 2] -> (3+1+2)/3 = 2，NULL 不参与
        log_case("AVG 忽略空值", [3, 1, None, 2], "分母为非空个数 3", r.rows)
        assert r.rows[0]["avg_qty"] == Decimal("2.0000000000")

    def test_count_counts_rows_including_nulls(self, sales_registry, model):
        eng = QueryEngine(sales_registry, model)
        r = eng.run(Query(metrics=("n_orders",)))
        log_case("COUNT 计行", "4 行含 1 行 qty 为空", "COUNT 与空值无关", r.rows)
        assert r.rows[0]["n_orders"] == 4

    def test_all_null_group_avg_is_none(self, sales_registry, model):
        eng = QueryEngine(sales_registry, model)
        r = eng.run(Query(metrics=("avg_qty",), dimensions=("customers.city",)))
        sh = next(row for row in r.rows if row["customers.city"] == "sh")
        log_case("全空分组", "sh 组 qty 全为 None", "AVG 结果为 None 而非 0 或报错",
                 sh["avg_qty"])
        assert sh["avg_qty"] is None

    def test_sum_of_all_null_is_none(self, registry):
        from sml.model import SemanticModel
        registry.register("t", [{"g": "a", "v": None}, {"g": "a", "v": None},
                                {"g": "b", "v": 1.5}])
        m = SemanticModel(registry)
        m.add_metric(Metric("s", "t", "v", Agg.SUM))
        r = QueryEngine(registry, m).run(Query(metrics=("s",), dimensions=("t.g",)))
        a = next(row for row in r.rows if row["t.g"] == "a")
        log_case("SUM 全空组", "组 a 的 v 全为 None", "结果为 None 而非 0", a["s"])
        assert a["s"] is None

    def test_null_dimension_forms_group(self, sales_registry, model):
        eng = QueryEngine(sales_registry, model)
        r = eng.run(Query(metrics=("n_orders",), dimensions=("customers.city",)))
        keys = [row["customers.city"] for row in r.rows]
        log_case("空维度成组", "cid=99 关联不到客户", "NULL 维度独立成组且排序在最前",
                 keys)
        assert keys[0] is None and len(keys) == 3

    def test_null_never_matches_filter(self, sales_registry, model):
        eng = QueryEngine(sales_registry, model)
        r = eng.run(Query(metrics=("n_orders",),
                          filters=(Filter("orders.qty", "ne", 3),)))
        # qty: 3,1,None,2 -> ne 3 命中 1,2；None 不匹配任何谓词
        log_case("空值不匹配谓词", "qty ne 3", "NULL 行被排除（含 ne）", r.rows)
        assert r.rows[0]["n_orders"] == 2


class TestRatioAndDivision:
    def test_ratio_basic(self, sales_registry, model):
        eng = QueryEngine(sales_registry, model)
        r = eng.run(Query(metrics=("amt_per_order",)))
        # total 1.6 / 4 = 0.4
        log_case("比率指标", "sum(amt)=1.6, count=4", "逐组由原始行计算后相除",
                 r.rows[0]["amt_per_order"])
        assert r.rows[0]["amt_per_order"] == Decimal("0.4000000000")

    def test_ratio_divide_by_zero_is_none(self, registry):
        from sml.model import SemanticModel
        registry.register("t", [{"g": "a", "v": 1}, {"g": "b", "v": 2}])
        m = SemanticModel(registry)
        m.add_metric(Metric("s", "t", "v", Agg.SUM))
        m.add_metric(Metric("z", "t", "v", Agg.COUNT_DISTINCT))
        m.add_ratio_metric(RatioMetric("r", "s", "z"))
        eng = QueryEngine(registry, m)
        # 过滤到空集 -> 无分组；改用分母为 0 的场景：sum 为 0
        registry.register("t2", [{"g": "a", "v": 0.0}])
        m.add_metric(Metric("s2", "t2", "v", Agg.SUM))
        m.add_metric(Metric("c2", "t2", None, Agg.COUNT))
        m.add_ratio_metric(RatioMetric("r2", "c2", "s2"))  # 分母 sum=0
        r = eng.run(Query(metrics=("r2",)))
        log_case("除零", "分母 sum(v)=0", "结果为 None，不报错不静默给 0",
                 r.rows[0]["r2"])
        assert r.rows[0]["r2"] is None

    def test_avg_not_from_preaggregates(self, registry):
        """AVG 必须由原始行计算：两组行数悬殊时，整体 AVG != 组 AVG 的平均。"""
        from sml.model import SemanticModel
        rows = [{"g": "a", "v": 100}] + [{"g": "b", "v": 1} for _ in range(99)]
        registry.register("t", rows)
        m = SemanticModel(registry)
        m.add_metric(Metric("avg_v", "t", "v", Agg.AVG))
        eng = QueryEngine(registry, m)
        overall = eng.run(Query(metrics=("avg_v",))).rows[0]["avg_v"]
        naive = (Decimal(100) + Decimal(1)) / 2  # 若由组均值再平均则得 50.5
        log_case("AVG 不再聚合", "组a:1行=100, 组b:99行=1",
                 "整体AVG=199/100=1.99，而非组均值的平均 50.5",
                 overall)
        assert overall == Decimal("1.9900000000")
        assert overall != naive


class TestHighCardinality:
    def test_output_row_limit(self, registry):
        from sml.model import SemanticModel
        registry.register("t", [{"g": i, "v": 1} for i in range(50)])
        m = SemanticModel(registry)
        m.add_metric(Metric("c", "t", None, Agg.COUNT))
        eng = QueryEngine(registry, m, ResourceLimits(max_output_rows=10))
        with pytest.raises(ResourceLimitError) as ei:
            eng.run(Query(metrics=("c",), dimensions=("t.g",)))
        log_case("高基数分组", "50 个不同 g，上限 10",
                 "超出 output_rows 预算即拒绝", ei.value.limit_kind)
        assert ei.value.limit_kind == "output_rows"

    def test_empty_result_is_not_error(self, sales_registry, model):
        eng = QueryEngine(sales_registry, model)
        r = eng.run(Query(metrics=("n_orders",),
                          filters=(Filter("orders.qty", "gt", 100),)))
        log_case("空结果", "无行满足 qty>100", "返回 0 行而非报错", r.rows)
        assert r.rows == []
