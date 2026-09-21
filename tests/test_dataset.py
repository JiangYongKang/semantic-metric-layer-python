"""数据集注册/推断的异常归类测试。"""

from __future__ import annotations

from decimal import Decimal

from tests._support import LoggingTestCase
from sml.dataset import DatasetRegistry, infer_schema
from sml.errors import (
    DuplicatePrimaryKeyError,
    FieldMissingError,
    IncompleteStructureError,
    SourceUpdateError,
    TypeConflictError,
)
from sml.types import LogicalType


class InferSchemaTest(LoggingTestCase):
    def test_empty_records_rejected(self) -> None:
        with self.assertRaises(IncompleteStructureError) as cm:
            infer_schema([], name="empty")
        self.assertErrorCode(cm, "incomplete_structure", "records=[]")

    def test_all_null_column_rejected(self) -> None:
        recs = [{"id": 1, "note": None}, {"id": 2, "note": None}]
        with self.assertRaises(FieldMissingError) as cm:
            infer_schema(recs, name="t")
        self.assertErrorCode(cm, "field_missing", "note 列全空")

    def test_ragged_rows_rejected(self) -> None:
        recs = [{"id": 1, "v": 2}, {"id": 2}]
        with self.assertRaises(FieldMissingError) as cm:
            infer_schema(recs, name="t")
        self.assertErrorCode(cm, "field_missing", "第二行缺少 v 列")
        self.assertEqual(cm.exception.details["missing_columns"], ["v"])

    def test_type_conflict_int_vs_text(self) -> None:
        recs = [{"id": 1, "v": 10}, {"id": 2, "v": "x"}]
        with self.assertRaises(TypeConflictError) as cm:
            infer_schema(recs, name="t")
        self.assertErrorCode(cm, "type_conflict", "v 列首行为 int、次行为 str")

    def test_int_promotes_to_decimal(self) -> None:
        recs = [{"id": 1, "v": 10}, {"id": 2, "v": Decimal("1.5")}]
        schema = infer_schema(recs, name="t")
        self.log_judgement("v 列含 int 与 Decimal", "提升为 decimal", schema.field_map["v"].logical_type.value)
        self.assertIs(schema.field_map["v"].logical_type, LogicalType.DECIMAL)

    def test_float_rejected(self) -> None:
        recs = [{"id": 1, "v": 1.0}]
        with self.assertRaises(TypeConflictError) as cm:
            infer_schema(recs, name="t")
        self.assertErrorCode(cm, "type_conflict", "v=1.0 (Python float)")

    def test_pk_missing_column(self) -> None:
        with self.assertRaises(FieldMissingError) as cm:
            infer_schema([{"id": 1}], primary_key=("k",), name="t")
        self.assertErrorCode(cm, "field_missing", "主键列 k 不存在")

    def test_pk_null_and_duplicate(self) -> None:
        with self.assertRaises(IncompleteStructureError) as cm:
            infer_schema([{"id": 1}, {"id": None}], primary_key=("id",), name="t")
        self.assertErrorCode(cm, "incomplete_structure", "第二行主键为空")
        with self.assertRaises(DuplicatePrimaryKeyError) as cm2:
            infer_schema([{"id": 1}, {"id": 1}], primary_key=("id",), name="t")
        self.assertErrorCode(cm2, "duplicate_primary_key", "主键重复 1")


class UpdateTest(LoggingTestCase):
    def _reg(self) -> DatasetRegistry:
        reg = DatasetRegistry()
        reg.register("orders", [{"id": 1, "amt": Decimal("10.50")}], primary_key=("id",))
        return reg

    def test_content_update_ok_bumps_version(self) -> None:
        reg = self._reg()
        ds = reg.replace_data("orders", [{"id": 1, "amt": Decimal("11.00")}, {"id": 2, "amt": Decimal("12.00")}])
        self.log_judgement("替换为两行同构数据", "version=2", str(ds.version))
        self.assertEqual(ds.version, 2)
        self.assertEqual(len(ds.records), 2)

    def test_add_column_allowed_with_null_default(self) -> None:
        reg = self._reg()
        ds = reg.replace_data("orders", [{"id": 1, "amt": Decimal("10.50"), "tag": "a"}])
        self.log_judgement("新增 tag 列", "tag nullable=True 且旧结构保留", str(ds.schema.field_map["tag"].nullable))
        self.assertTrue(ds.schema.field_map["tag"].nullable)
        self.assertIn("id", ds.schema.field_map)

    def test_drop_column_rejected(self) -> None:
        reg = self._reg()
        with self.assertRaises(SourceUpdateError) as cm:
            reg.replace_data("orders", [{"id": 1}])
        self.assertErrorCode(cm, "source_update_rejected", "更新删除 amt 列")

    def test_rename_like_change_rejected(self) -> None:
        reg = self._reg()
        with self.assertRaises(SourceUpdateError) as cm:
            reg.replace_data("orders", [{"id": 1, "amount": Decimal("10.50")}])
        self.assertErrorCode(cm, "source_update_rejected", "amt 改名为 amount")
        self.assertEqual(cm.exception.details["removed_or_renamed_columns"], ["amt"])

    def test_type_drift_rejected(self) -> None:
        reg = self._reg()
        with self.assertRaises(TypeConflictError) as cm:
            reg.replace_data("orders", [{"id": 1, "amt": "x"}])
        self.assertErrorCode(cm, "type_conflict", "amt 由 decimal 漂移为文本")

    def test_failed_update_keeps_old_state(self) -> None:
        reg = self._reg()
        try:
            reg.replace_data("orders", [{"id": 1}])
        except SourceUpdateError:
            pass
        old = reg.get("orders")
        self.log_judgement("失败更新后读取", "仍是 version=1 的旧数据", f"v{old.version}, {old.records[0]}")
        self.assertEqual(old.version, 1)
        self.assertEqual(old.records[0]["amt"], Decimal("10.50"))


if __name__ == "__main__":
    unittest.main()
