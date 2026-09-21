"""列级血缘：每个输出列可追溯到来源字段与所用口径。

血缘与结果列一一对应、顺序一致；引擎产出结果时同时产出血缘，
二者来自同一次计算，不允许事后补造，确保不错位、不缺失。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ColumnLineage:
    """单个输出列的血缘。"""

    output_column: str
    kind: str  # dimension / measure / ratio / avg
    caliber_name: str
    sources: tuple[str, ...]
    transforms: tuple[str, ...] = ()
    # 该列值经哪些关联取得（关系名有序列表），无关联时为空
    via_relations: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "output_column": self.output_column,
            "kind": self.kind,
            "caliber": self.caliber_name,
            "sources": list(self.sources),
            "transforms": list(self.transforms),
            "via_relations": list(self.via_relations),
        }


@dataclass(frozen=True)
class Lineage:
    """整次查询的列级血缘。"""

    columns: tuple[ColumnLineage, ...] = ()

    def output_names(self) -> tuple[str, ...]:
        return tuple(c.output_column for c in self.columns)

    def to_dict(self) -> dict:
        return {"columns": [c.to_dict() for c in self.columns]}

    def get(self, output_column: str) -> ColumnLineage:
        for c in self.columns:
            if c.output_column == output_column:
                return c
        raise KeyError(output_column)
