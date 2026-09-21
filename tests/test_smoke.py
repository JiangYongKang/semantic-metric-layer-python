"""骨架冒烟测试：仅验证包可导入、错误体系可用。"""

from __future__ import annotations

import unittest

from sml import errors
from sml.types import LogicalType, infer_value, merge_types


class SmokeTest(unittest.TestCase):
    def test_import_and_codes(self) -> None:
        self.assertEqual(errors.FieldMissingError("x").code, "field_missing")
        self.assertEqual(errors.NonUniqueKeyError("x").code, "non_unique_key")

    def test_infer(self) -> None:
        self.assertIs(infer_value(1), LogicalType.INTEGER)
        self.assertIs(infer_value("a"), LogicalType.TEXT)
        self.assertIs(
            merge_types(LogicalType.INTEGER, LogicalType.DECIMAL, field="f"),
            LogicalType.DECIMAL,
        )


if __name__ == "__main__":
    unittest.main()
