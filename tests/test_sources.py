"""本地数据源（CSV）测试：解析确定性、表头问题拒绝、端到端聚合。"""

from __future__ import annotations

import os
import tempfile
from decimal import Decimal

from tests._support import LoggingTestCase
from sml.caliber import AggKind, CaliberBook, DimensionSpec, MeasureSpec
from sml.dataset import DatasetRegistry
from sml.engine import Query, QueryEngine
from sml.errors import IncompleteStructureError, TypeConflictError
from sml.model import build_model
from sml.relations import RelationGraph
from sml.sources import LocalSource


def _write_csv(content: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".csv", text=True)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
        fh.write(content)
    return path


class CsvSourceTest(LoggingTestCase):
    def test_scalar_parsing_deterministic(self) -> None:
        csv_text = "oid,region,amount,qty,active\n1,east,100.50,2,true\n2,west,0.1,,false\n"
        path = _write_csv(csv_text)
        try:
            src = LocalSource.from_csv("orders", path)
        finally:
            os.unlink(path)
        self.log_judgement("CSV 两行", "Decimal 精确解析, 空单元格 None, bool",
                           src.records)
        self.assertEqual(src.records[0]["amount"], Decimal("100.50"))
        self.assertEqual(src.records[0]["oid"], 1)
        self.assertIs(src.records[0]["active"], True)
        self.assertIsNone(src.records[1]["qty"])
        self.assertIs(src.records[1]["active"], False)

    def test_csv_aggregation_end_to_end(self) -> None:
        path = _write_csv("oid,region,amount\n1,east,0.1\n2,east,0.2\n3,west,0.3\n")
        try:
            src = LocalSource.from_csv("orders", path)
        finally:
            os.unlink(path)
        reg = DatasetRegistry()
        reg.register("orders", src.records, primary_key=("oid",))
        book = CaliberBook(reg)
        book.add_dimension(DimensionSpec("region", "orders", "region"))
        book.add_measure(MeasureSpec("total", "orders", AggKind.SUM, "amount"))
        model = build_model(1, reg, book, RelationGraph(reg))
        res = QueryEngine(model).run(
            Query(dimensions=("region",), measures=("total",)))
        mapping = {r[0]: r[1] for r in res.rows}
        self.log_judgement("CSV 来源按 region 求和", "east=0.3（非 0.30000000004）",
                           mapping)
        self.assertEqual(mapping["east"], Decimal("0.3"))
        self.assertEqual(mapping["west"], Decimal("0.3"))

    def test_nan_inf_rejected(self) -> None:
        path = _write_csv("id,v\n1,nan\n")
        try:
            with self.assertRaises(TypeConflictError) as cm:
                LocalSource.from_csv("t", path)
            self.assertErrorCode(cm, "type_conflict", "CSV 出现 nan")
        finally:
            os.unlink(path)

    def test_duplicate_header_rejected(self) -> None:
        path = _write_csv("id,id,v\n1,2,3\n")
        try:
            with self.assertRaises(IncompleteStructureError) as cm:
                LocalSource.from_csv("t", path)
            self.assertErrorCode(cm, "incomplete_structure", "表头 id 重名")
        finally:
            os.unlink(path)

    def test_ragged_csv_row_rejected(self) -> None:
        path = _write_csv("id,v\n1,2\n3\n")
        try:
            with self.assertRaises(IncompleteStructureError) as cm:
                LocalSource.from_csv("t", path)
            self.assertErrorCode(cm, "incomplete_structure", "第二行列数不足")
        finally:
            os.unlink(path)

    def test_empty_file_register_rejected(self) -> None:
        path = _write_csv("")
        try:
            src = LocalSource.from_csv("t", path)
            reg = DatasetRegistry()
            from sml.errors import IncompleteStructureError as ICE
            with self.assertRaises(ICE) as cm:
                reg.register("t", src.records)
            self.assertErrorCode(cm, "incomplete_structure", "空 CSV 无列无行")
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
