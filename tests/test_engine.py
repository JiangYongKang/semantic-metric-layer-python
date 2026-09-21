"""引擎结果语义测试：聚合、空值、除零、精度、连接与血缘。"""

from __future__ import annotations

from decimal import Decimal

from tests._support import LoggingTestCase
from sml.caliber import AggKind, CaliberBook, DimensionSpec, MeasureSpec, RatioSpec
from sml.dataset import DatasetRegistry
from sml.engine import Filter, Query, QueryEngine
from sml.errors import QueryError
from sml.model import build_model
from sml.relations import Cardinality, Relation, RelationGraph


def build_layer():
    """orders(事实) many->one customers。"""
    reg = DatasetRegistry()
    reg.register("orders", [
        {"oid": 1, "cid": 10, "amount": Decimal("100.00"), "qty": 2},
        {"oid": 2, "cid": 10, "amount": Decimal("0.10"), "qty": None},
        {"oid": 3, "cid": 20, "amount": Decimal("200.30"), "qty": 3},
        {"oid": 4, "cid": 99, "amount": None, "qty": 5},  # 匹配不到 customer
    ], primary_key=("oid",))
    reg.register("customers", [
        {"cid": 10, "city": "BJ"},
        {"cid": 20, "city": "SH"},
    ], primary_key=("cid",))
    book = CaliberBook(reg)
    book.add_dimension(DimensionSpec("city", "customers", "city"))
    book.add_measure(MeasureSpec("total_amount", "orders", AggKind.SUM, "amount"))
    book.add_measure(MeasureSpec("total_qty", "orders", AggKind.SUM, "qty"))
    book.add_measure(MeasureSpec("orders_cnt", "orders", AggKind.COUNT))
    book.add_measure(MeasureSpec("avg_amount", "orders", AggKind.AVG, "amount"))
    book.add_measure(MeasureSpec("cust_cnt", "orders", AggKind.COUNT_DISTINCT, "cid"))
    book.add_ratio(RatioSpec("avg_order", "orders", "total_amount", "orders_cnt"))
    graph = RelationGraph(reg)
    graph.add(Relation("o_c", "orders", ("cid",), "customers", ("cid",),
                       Cardinality.MANY_TO_ONE))
    model = build_model(1, reg, book, graph)
    return model


def to_map(result):
    return {row[0]: dict(zip(result.columns, row)) for row in result.rows}


class EngineBasicTest(LoggingTestCase):
    def test_group_join_and_aggregates(self) -> None:
        model = build_layer()
        eng = QueryEngine(model)
        q = Query(
            dimensions=("city",),
            measures=("total_amount", "orders_cnt", "avg_amount",
                      "total_qty", "cust_cnt", "avg_order"),
        )
        res = eng.run(q)
        m = to_map(res)
        self.log_judgement("按 city 分组（cid=99 无匹配）",
                           "BJ:100.10/2; SH:200.30/1; 空桶:金额NULL行",
                           {k: {kk: str(vv) for kk, vv in v.items()} for k, v in m.items()})
        # BJ = 100.00 + 0.10
        self.assertEqual(m["BJ"]["total_amount"], Decimal("100.10"))
        self.assertEqual(m["BJ"]["orders_cnt"], 2)
        # avg 在明细上计算: 100.10/2 = 50.05（量化10位）
        self.assertEqual(m["BJ"]["avg_amount"], Decimal("50.0500000000"))
        self.assertEqual(m["BJ"]["total_qty"], 2)  # 第二行 qty NULL 被忽略
        self.assertEqual(m["BJ"]["cust_cnt"], 1)
        # 比率同 avg(sum/count)
        self.assertEqual(m["BJ"]["avg_order"], Decimal("50.0500000000"))
        # 空值桶：amount 全 NULL -> sum=0, avg=None
        null_bucket = next(v for k, v in m.items() if k is None)
        self.assertEqual(null_bucket["total_amount"], 0)
        self.assertIsNone(null_bucket["avg_amount"])
        self.assertEqual(null_bucket["orders_cnt"], 1)

    def test_no_row_inflation(self) -> None:
        model = build_layer()
        res = QueryEngine(model).run(Query(measures=("orders_cnt",)))
        total = res.rows[0][0]
        self.log_judgement("无分组全表 count", "事实表 4 行，连接不膨胀", total)
        self.assertEqual(total, 4)

    def test_null_bucket_sorts_first(self) -> None:
        model = build_layer()
        res = QueryEngine(model).run(Query(dimensions=("city",), measures=("orders_cnt",)))
        first = res.rows[0][0]
        self.log_judgement("分组排序", "NULL 桶在最前，随后 BJ/SH",
                           [r[0] for r in res.rows])
        self.assertIsNone(first)
        self.assertEqual([r[0] for r in res.rows], [None, "BJ", "SH"])

    def test_filter_null_and_in(self) -> None:
        model = build_layer()
        res = QueryEngine(model).run(
            Query(dimensions=("city",), measures=("orders_cnt",),
                  filters=(Filter("city", (None,)),),))
        counts = {r[0]: r[1] for r in res.rows}
        self.log_judgement("过滤 city IS NULL（无匹配+左连接）", "仅 1 行", counts)
        self.assertEqual(counts, {None: 1})
        res2 = QueryEngine(model).run(
            Query(measures=("orders_cnt",), filters=(Filter("city", ("BJ",)),)))
        self.assertEqual(res2.rows[0][0], 2)
        res3 = QueryEngine(model).run(
            Query(measures=("orders_cnt",), filters=(Filter("city", ("BJ",), negate=True),),))
        self.log_judgement("city NOT IN (BJ)：in 的确定补集",
                           "SH 1 行 + NULL 空桶 1 行 = 2", res3.rows[0][0])
        self.assertEqual(res3.rows[0][0], 2)
        res4 = QueryEngine(model).run(
            Query(measures=("orders_cnt",),
                  filters=(Filter("city", ("BJ", None), negate=True),),))
        self.log_judgement("city NOT IN (BJ, NULL)：空桶被显式排除",
                           "仅 SH 1 行", res4.rows[0][0])
        self.assertEqual(res4.rows[0][0], 1)

    def test_cross_fact_measures_rejected(self) -> None:
        reg = DatasetRegistry()
        reg.register("a", [{"id": 1, "x": Decimal("1")}], primary_key=("id",))
        reg.register("b", [{"id": 1, "y": Decimal("1")}], primary_key=("id",))
        book = CaliberBook(reg)
        book.add_measure(MeasureSpec("mx", "a", AggKind.SUM, "x"))
        book.add_measure(MeasureSpec("my", "b", AggKind.SUM, "y"))
        model = build_model(1, reg, book, RelationGraph(reg))
        with self.assertRaises(QueryError) as cm:
            QueryEngine(model).run(Query(measures=("mx", "my")))
        self.assertErrorCode(cm, "query_error", "跨数据集两个指标")

    def test_filter_type_mismatch(self) -> None:
        model = build_layer()
        with self.assertRaises(QueryError) as cm:
            QueryEngine(model).run(
                Query(measures=("orders_cnt",),
                      filters=(Filter("unknown_dim", ("x",),),),))
        self.assertErrorCode(cm, "query_error", "过滤维度不存在")
        # 数值列过滤文本也应被拒绝
        reg = DatasetRegistry()
        reg.register("t", [{"id": 1, "v": Decimal("1")}], primary_key=("id",))
        b2 = CaliberBook(reg)
        b2.add_dimension(DimensionSpec("v", "t", "v"))
        b2.add_measure(MeasureSpec("c", "t", AggKind.COUNT))
        m2 = build_model(2, reg, b2, RelationGraph(reg))
        with self.assertRaises(QueryError) as cm2:
            QueryEngine(m2).run(
                Query(measures=("c",), filters=(Filter("v", ("abc",),),),))
        self.assertErrorCode(cm2, "query_error", "decimal 维度过滤值 abc 不可解析")

    def test_all_null_group_semantics(self) -> None:
        reg = DatasetRegistry()
        reg.register("t", [
            {"id": 1, "g": None, "v": None},
            {"id": 2, "g": None, "v": None},
            {"id": 3, "g": "other", "v": 7},
        ], primary_key=("id",))
        book = CaliberBook(reg)
        book.add_dimension(DimensionSpec("g", "t", "g"))
        book.add_measure(MeasureSpec("s", "t", AggKind.SUM, "v"))
        book.add_measure(MeasureSpec("mn", "t", AggKind.MIN, "v"))
        book.add_measure(MeasureSpec("mx", "t", AggKind.MAX, "v"))
        book.add_measure(MeasureSpec("c", "t", AggKind.COUNT))
        book.add_measure(MeasureSpec("cd", "t", AggKind.COUNT_DISTINCT, "v"))
        model = build_model(1, reg, book, RelationGraph(reg))
        res = QueryEngine(model).run(
            Query(dimensions=("g",), measures=("s", "mn", "mx", "c", "cd")))
        vals = res.rows[0]
        self.log_judgement("全空分组", "sum=0,min=None,max=None,count=2,count_distinct=0",
                           [str(x) for x in vals])
        self.assertEqual(vals, (None, 0, None, None, 2, 0))


class PrecisionTest(LoggingTestCase):
    def test_large_decimal_and_repeated_join_precision(self) -> None:
        # 大数 + 不可被二进制精确表示的小数
        reg = DatasetRegistry()
        reg.register("o", [
            {"id": i, "amt": Decimal("99999999999999999999.99"), "extra": Decimal("0.1")}
            for i in range(1, 4)
        ], primary_key=("id",))
        reg.register("d", [{"id": 1, "z": Decimal("1")}], primary_key=("id",))
        book = CaliberBook(reg)
        book.add_measure(MeasureSpec("s", "o", AggKind.SUM, "amt"))
        book.add_measure(MeasureSpec("a", "o", AggKind.AVG, "extra"))
        graph = RelationGraph(reg)
        # 同键 one_to_one 连接（这里只为携带路径，不影响事实）
        graph.add(Relation("o_d", "o", ("id",), "d", ("id",), Cardinality.ONE_TO_ONE))
        model = build_model(1, reg, book, graph)
        res = QueryEngine(model).run(Query(measures=("s", "a")))
        s, a = res.rows[0]
        self.log_judgement("3 个 99999999999999999999.99 求和, 0.1 求均",
                           "299999999999999999999.97 / 0.1000000000",
                           f"{s}, {a}")
        self.assertEqual(s, Decimal("299999999999999999999.97"))
        self.assertEqual(a, Decimal("0.1000000000"))

    def test_ratio_zero_denominator_is_null(self) -> None:
        reg = DatasetRegistry()
        reg.register("o", [
            {"id": 1, "g": "x", "num": Decimal("5"), "den": 0},
            {"id": 2, "g": "x", "num": Decimal("5"), "den": 0},
        ], primary_key=("id",))
        book = CaliberBook(reg)
        book.add_dimension(DimensionSpec("g", "o", "g"))
        book.add_measure(MeasureSpec("n", "o", AggKind.SUM, "num"))
        book.add_measure(MeasureSpec("d", "o", AggKind.SUM, "den"))
        book.add_ratio(RatioSpec("r", "o", "n", "d"))
        model = build_model(1, reg, book, RelationGraph(reg))
        res = QueryEngine(model).run(Query(dimensions=("g",), measures=("r",)))
        self.log_judgement("分母合计为 0", "比率为 None（不产生 inf）", res.rows[0][1])
        self.assertIsNone(res.rows[0][1])


class LineageTest(LoggingTestCase):
    def test_lineage_matches_columns(self) -> None:
        model = build_layer()
        res = QueryEngine(model).run(
            Query(dimensions=("city",), measures=("total_amount", "avg_order")))
        self.log_judgement("结果列与血缘", "列名一一对应且顺序一致",
                           (res.columns, res.lineage.output_names()))
        self.assertEqual(res.lineage.output_names(), res.columns)
        lin = {c.output_column: c for c in res.lineage.columns}
        self.assertEqual(lin["city"].sources, ("customers.city",))
        self.assertEqual(lin["city"].via_relations, ("o_c",))
        self.assertEqual(lin["total_amount"].sources, ("orders.amount",))
        self.assertEqual(lin["avg_order"].kind, "ratio")
        self.assertIn("orders.amount", lin["avg_order"].sources)
        self.assertTrue(any("detail rows" in t for t in lin["avg_order"].transforms))


if __name__ == "__main__":
    unittest.main()
