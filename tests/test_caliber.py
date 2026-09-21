"""口径定义的冲突与合法性测试。"""

from __future__ import annotations

from decimal import Decimal

from tests._support import LoggingTestCase
from sml.caliber import AggKind, CaliberBook, DimensionSpec, MeasureSpec, RatioSpec
from sml.dataset import DatasetRegistry
from sml.errors import CaliberConflictError, InvalidCaliberError


def _book() -> CaliberBook:
    reg = DatasetRegistry()
    reg.register("orders", [
        {"id": 1, "region": "east", "amount": Decimal("10"), "qty": 2},
        {"id": 2, "region": "west", "amount": Decimal("20"), "qty": 0},
    ], primary_key=("id",))
    return CaliberBook(reg)


class CaliberTest(LoggingTestCase):
    def test_basic_definitions_ok(self) -> None:
        b = _book()
        b.add_dimension(DimensionSpec("region", "orders", "region"))
        b.add_measure(MeasureSpec("total_amount", "orders", AggKind.SUM, "amount"))
        b.add_measure(MeasureSpec("orders_cnt", "orders", AggKind.COUNT))
        b.add_ratio(RatioSpec("avg_amount", "orders", "total_amount", "orders_cnt"))
        kind, spec = b.resolve_metric("avg_amount")
        self.log_judgement("定义维度+sum+count+比率", "比率可解析", f"{kind}:{spec.name}")
        self.assertEqual(kind, "ratio")

    def test_duplicate_name_conflict(self) -> None:
        b = _book()
        b.add_dimension(DimensionSpec("region", "orders", "region"))
        with self.assertRaises(CaliberConflictError) as cm:
            b.add_measure(MeasureSpec("region", "orders", AggKind.SUM, "amount"))
        self.assertErrorCode(cm, "caliber_conflict", "维度与指标同名 region")

    def test_unknown_field(self) -> None:
        b = _book()
        with self.assertRaises(InvalidCaliberError) as cm:
            b.add_dimension(DimensionSpec("d", "orders", "nope"))
        self.assertErrorCode(cm, "invalid_caliber", "字段 orders.nope 不存在")

    def test_sum_on_text_rejected(self) -> None:
        b = _book()
        with self.assertRaises(InvalidCaliberError) as cm:
            b.add_measure(MeasureSpec("bad", "orders", AggKind.SUM, "region"))
        self.assertErrorCode(cm, "invalid_caliber", "对文本列 region 做 sum")

    def test_ratio_cross_dataset_rejected(self) -> None:
        reg = DatasetRegistry()
        reg.register("a", [{"id": 1, "x": Decimal("1")}], primary_key=("id",))
        reg.register("b", [{"id": 1, "y": Decimal("1")}], primary_key=("id",))
        b = CaliberBook(reg)
        b.add_measure(MeasureSpec("mx", "a", AggKind.SUM, "x"))
        b.add_measure(MeasureSpec("my", "b", AggKind.SUM, "y"))
        with self.assertRaises(InvalidCaliberError) as cm:
            b.add_ratio(RatioSpec("r", "a", "mx", "my"))
        self.assertErrorCode(cm, "invalid_caliber", "比率分母来自另一数据集")

    def test_ratio_based_on_avg_rejected(self) -> None:
        b = _book()
        b.add_measure(MeasureSpec("avg_amt", "orders", AggKind.AVG, "amount"))
        b.add_measure(MeasureSpec("cnt", "orders", AggKind.COUNT))
        with self.assertRaises(InvalidCaliberError) as cm:
            b.add_ratio(RatioSpec("weird", "orders", "avg_amt", "cnt"))
        self.assertErrorCode(cm, "invalid_caliber", "比率分子使用 avg 口径")

    def test_ratio_missing_base_rejected(self) -> None:
        b = _book()
        with self.assertRaises(InvalidCaliberError) as cm:
            b.add_ratio(RatioSpec("r", "orders", "nope1", "nope2"))
        self.assertErrorCode(cm, "invalid_caliber", "分子分母均未定义")


if __name__ == "__main__":
    unittest.main()
