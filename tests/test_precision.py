"""数值精度与大数：Decimal 精确累积、固定舍入、多次关联聚合无漂移、结果可复现。"""
from decimal import Decimal

from sml.engine import Query, QueryEngine
from sml.model import Agg, Join, Metric, RatioMetric, SemanticModel
from tests.conftest import log_case


def _make(registry, amounts):
    registry.register("orders", [{"oid": i, "cid": 1, "amt": a}
                                 for i, a in enumerate(amounts)])
    registry.register("customers", [{"cid": 1, "city": "hz"}])
    m = SemanticModel(registry)
    m.add_join(Join("o2c", "orders", "cid", "customers", "cid"))
    m.add_metric(Metric("total", "orders", "amt", Agg.SUM))
    m.add_metric(Metric("avg", "orders", "amt", Agg.AVG))
    m.add_metric(Metric("n", "orders", None, Agg.COUNT))
    m.add_ratio_metric(RatioMetric("per_order", "total", "n"))
    return m


class TestDecimalPrecision:
    def test_float_error_eliminated(self, registry):
        m = _make(registry, [0.1, 0.2])
        r = QueryEngine(registry, m).run(Query(metrics=("total",)))
        log_case("0.1+0.2", "float 下为 0.30000000000000004",
                 "Decimal(str) 规范化 + 精确累积", r.rows[0]["total"])
        assert r.rows[0]["total"] == Decimal("0.3")

    def test_many_small_amounts_no_drift(self, registry):
        m = _make(registry, [0.01] * 1000)
        r = QueryEngine(registry, m).run(Query(metrics=("total",)))
        log_case("1000 个 0.01 累加", "float 累加会漂移",
                 "Decimal 精确求和", r.rows[0]["total"])
        assert r.rows[0]["total"] == Decimal("10.00")

    def test_large_numbers_exact(self, registry):
        big = [Decimal("999999999999999999.99"), Decimal("0.01")]
        m = _make(registry, big)
        r = QueryEngine(registry, m).run(Query(metrics=("total",)))
        log_case("大数+小数", big, "Decimal 任意精度，不丢末位",
                 r.rows[0]["total"])
        assert r.rows[0]["total"] == Decimal("1000000000000000000.00")

    def test_avg_fixed_rounding_half_even(self, registry):
        # sum=1, count=3 -> 1/3：固定 10 位小数、ROUND_HALF_EVEN
        m = _make(registry, [Decimal("0.5"), Decimal("0.25"), Decimal("0.25")])
        r = QueryEngine(registry, m).run(Query(metrics=("avg",)))
        log_case("1/3 舍入", "sum=1, count=3", "固定 10 位小数 HALF_EVEN",
                 r.rows[0]["avg"])
        assert r.rows[0]["avg"] == Decimal("0.3333333333")
        assert r.rows[0]["avg"].as_tuple().exponent == -10

    def test_join_then_aggregate_no_inflation_no_drift(self, registry):
        m = _make(registry, [0.1, 0.2, 0.3])
        eng = QueryEngine(registry, m)
        joined = eng.run(Query(metrics=("total",), dimensions=("customers.city",)))
        plain = eng.run(Query(metrics=("total",)))
        log_case("关联后聚合", "3 行订单关联 1 行客户",
                 "关联不改变金额合计", (joined.rows[0]["total"], plain.rows[0]["total"]))
        assert joined.rows[0]["total"] == plain.rows[0]["total"] == Decimal("0.6")


class TestReproducibility:
    def test_same_query_same_result(self, sales_registry, model):
        eng = QueryEngine(sales_registry, model)
        q = Query(metrics=("total_amt", "avg_qty", "amt_per_order"),
                  dimensions=("customers.city",))
        r1, r2 = eng.run(q), eng.run(q)
        log_case("重复查询", q.canonical(), "结果逐行相等（缓存命中同一对象）",
                 r1 is r2)
        assert r1.rows == r2.rows and r1 is r2

    def test_row_order_deterministic(self, registry):
        m = _make(registry, [0.5] * 3)
        registry.update("customers", [{"cid": 1, "city": "sh"}])  # 换维度值
        eng = QueryEngine(registry, m)
        q = Query(metrics=("total",), dimensions=("customers.city",))
        orders = [tuple(row.items()) for row in eng.run(q).rows]
        log_case("输出顺序", "按维度键排序", "与插入顺序无关", orders)
        assert orders == sorted(orders)
