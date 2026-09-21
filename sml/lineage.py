"""列级血缘：每个输出列可追溯到来源字段与所用口径。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ColumnLineage:
    output_column: str
    kind: str  # "dimension" | "metric"
    source_fields: tuple[str, ...] = ()  # 形如 "dataset.field"
    expression: str = ""                 # 口径表达式，如 sum(orders.amt)

    def to_dict(self) -> dict:
        return {
            "output_column": self.output_column,
            "kind": self.kind,
            "source_fields": list(self.source_fields),
            "expression": self.expression,
        }
