"""关联歧义、键不唯一、扇出膨胀测试。"""

from __future__ import annotations

from decimal import Decimal

from tests._support import LoggingTestCase
from sml.dataset import DatasetRegistry
from sml.errors import AmbiguousJoinError, NonUniqueKeyError, RelationError
from sml.relations import Cardinality, Relation, RelationGraph


def _reg() -> DatasetRegistry:
    reg = DatasetRegistry()
    reg.register("orders", [
        {"oid": 1, "cid": 10, "amount": Decimal("100")},
        {"oid": 2, "cid": 10, "amount": Decimal("200")},
        {"oid": 3, "cid": 20, "amount": Decimal("300")},
    ], primary_key=("oid",))
    reg.register("customers", [
        {"cid": 10, "city": "BJ"},
        {"cid": 20, "city": "SH"},
    ], primary_key=("cid",))
    reg.register("cities", [
        {"city": "BJ", "region": "N"},
        {"city": "SH", "region": "E"},
    ], primary_key=("city",))
    return reg


class RelationTest(LoggingTestCase):
    def test_many_to_one_ok_and_path(self) -> None:
        reg = _reg()
        g = RelationGraph(reg)
        g.add(Relation("o_c", "orders", ("cid",), "customers", ("cid",),
                       Cardinality.MANY_TO_ONE))
        g.add(Relation("c_city", "customers", ("city",), "cities", ("city",),
                       Cardinality.MANY_TO_ONE))
        path = g.resolve_path("orders", "cities")
        self.log_judgement("orders->customers->cities", "唯一上行路径含2条关联",
                           [r.name for r in path])
        self.assertEqual([r.name for r in path], ["o_c", "c_city"])

    def test_non_unique_one_side_rejected(self) -> None:
        # 不声明主键，仅靠关联键唯一约束兜底
        reg = DatasetRegistry()
        reg.register("orders", [
            {"oid": 1, "cid": 10}, {"oid": 2, "cid": 20},
        ])
        reg.register("customers", [
            {"cid": 10, "city": "BJ"},
            {"cid": 10, "city": "SH"},
        ])
        g = RelationGraph(reg)
        with self.assertRaises(NonUniqueKeyError) as cm:
            g.add(Relation("o_c", "orders", ("cid",), "customers", ("cid",),
                           Cardinality.MANY_TO_ONE))
        self.assertErrorCode(cm, "non_unique_key", "customers.cid 重复且无主键约束")
        self.assertEqual(cm.exception.details["reason"], "duplicate_key")

    def test_null_fk_rejected(self) -> None:
        # one_to_one 要求两侧键唯一非空：用空键的一侧触发 null_key
        reg = DatasetRegistry()
        reg.register("a", [{"id": 1, "k": 1}, {"id": 2, "k": None}], )
        reg.register("b", [{"id": 1, "k": 9}])
        g = RelationGraph(reg)
        with self.assertRaises(NonUniqueKeyError) as cm:
            g.add(Relation("a_b", "a", ("k",), "b", ("k",),
                           Cardinality.ONE_TO_ONE))
        self.assertErrorCode(cm, "non_unique_key", "one_to_one 侧 a.k 为空")
        self.assertEqual(cm.exception.details["reason"], "null_key")

    def test_fanout_from_one_side_rejected(self) -> None:
        # customers(one) 同时挂向 orders 方向解析 cities->orders：
        # cities one 侧 -> customers one_to_one 后再 one->many 扇出到 orders
        reg = _reg()
        g = RelationGraph(reg)
        g.add(Relation("o_c", "orders", ("cid",), "customers", ("cid",),
                       Cardinality.MANY_TO_ONE))
        g.add(Relation("c_city", "customers", ("city",), "cities", ("city",),
                       Cardinality.MANY_TO_ONE))
        with self.assertRaises(AmbiguousJoinError) as cm:
            g.resolve_path("cities", "orders")
        self.assertErrorCode(cm, "ambiguous_join", "从 cities 下行到 orders 需扇出")

    def test_no_path(self) -> None:
        reg = _reg()
        g = RelationGraph(reg)
        with self.assertRaises(AmbiguousJoinError) as cm:
            g.resolve_path("orders", "cities")
        self.assertErrorCode(cm, "ambiguous_join", "两表无关联路径")
        self.assertEqual(cm.exception.details["reason"], "no_path")

    def test_duplicate_relation_pair_rejected(self) -> None:
        reg = _reg()
        g = RelationGraph(reg)
        g.add(Relation("o_c", "orders", ("cid",), "customers", ("cid",),
                       Cardinality.MANY_TO_ONE))
        with self.assertRaises(AmbiguousJoinError) as cm:
            g.add(Relation("o_c2", "orders", ("cid",), "customers", ("cid",),
                           Cardinality.MANY_TO_ONE))
        self.assertErrorCode(cm, "ambiguous_join", "同两表第二条关联")

    def test_multiple_paths_rejected(self) -> None:
        # customers 与 cities 之间再造一条经由 orders 的间接路径不可行（orders无city），
        # 改为构造菱形：orders 直接到 cities，且经 customers 到 cities。
        reg = _reg()
        # orders 加 city 列
        reg.replace_data("orders", [
            {"oid": 1, "cid": 10, "amount": Decimal("100"), "city": "BJ"},
            {"oid": 2, "cid": 10, "amount": Decimal("200"), "city": "BJ"},
            {"oid": 3, "cid": 20, "amount": Decimal("300"), "city": "SH"},
        ])
        g = RelationGraph(reg)
        g.add(Relation("o_c", "orders", ("cid",), "customers", ("cid",),
                       Cardinality.MANY_TO_ONE))
        g.add(Relation("c_city", "customers", ("city",), "cities", ("city",),
                       Cardinality.MANY_TO_ONE))
        g.add(Relation("o_city", "orders", ("city",), "cities", ("city",),
                       Cardinality.MANY_TO_ONE))
        with self.assertRaises(AmbiguousJoinError) as cm:
            g.resolve_path("orders", "cities")
        self.assertErrorCode(cm, "ambiguous_join", "orders 到 cities 有直连与绕行两条路")
        self.assertEqual(cm.exception.details["reason"], "multiple_paths")

    def test_missing_key_column_rejected(self) -> None:
        reg = _reg()
        g = RelationGraph(reg)
        with self.assertRaises(Exception) as cm:
            g.add(Relation("bad", "orders", ("nope",), "customers", ("cid",),
                           Cardinality.MANY_TO_ONE))
        self.assertErrorCode(cm, "invalid_caliber", "orders.nope 列不存在")


if __name__ == "__main__":
    unittest.main()
