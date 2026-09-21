"""数据集结构：类型系统、结构推断与值规范化。

拒绝语义（可区分）：
- 空数据集 / 行不是映射 / 字段无法推断类型 -> IncompleteStructureError
- 某些行缺少字段              -> MissingFieldError
- 同一字段出现不兼容类型       -> TypeConflictError
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from decimal import Decimal

from .errors import IncompleteStructureError, MissingFieldError, TypeConflictError


class DataType(enum.Enum):
    INT = "int"
    DECIMAL = "decimal"
    STRING = "string"
    BOOL = "bool"


@dataclass(frozen=True)
class Field:
    name: str
    dtype: DataType


@dataclass(frozen=True)
class TableSchema:
    fields: tuple[Field, ...] = ()

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields)

    def field(self, name: str) -> Field:
        for f in self.fields:
            if f.name == name:
                return f
        raise KeyError(name)

    def has(self, name: str) -> bool:
        return any(f.name == name for f in self.fields)


def _infer_value_type(value) -> DataType | None:
    """单个值的类型；None 不参与推断。"""
    if value is None:
        return None
    if isinstance(value, bool):  # 必须先于 int 判断
        return DataType.BOOL
    if isinstance(value, int):
        return DataType.INT
    if isinstance(value, (float, Decimal)):
        return DataType.DECIMAL
    if isinstance(value, str):
        return DataType.STRING
    raise TypeConflictError(
        f"不支持的值类型: {type(value).__name__}",
        detail={"value_repr": repr(value)},
    )


def normalize_value(value, dtype: DataType):
    """把值规范化为确定性的内部表示；float 经 str 转 Decimal，避免二进制误差。"""
    if value is None:
        return None
    if dtype is DataType.INT:
        return int(value)
    if dtype is DataType.DECIMAL:
        if isinstance(value, Decimal):
            return value
        if isinstance(value, float):
            return Decimal(str(value))
        return Decimal(value)
    if dtype is DataType.BOOL:
        return bool(value)
    return str(value)


def infer_schema(rows: list[dict]) -> TableSchema:
    """从行数据推断结构；任何不完整/冲突都明确拒绝。"""
    if not rows:
        raise IncompleteStructureError("数据集为空，无法推断结构", detail={"rows": 0})
    for i, row in enumerate(rows):
        if not isinstance(row, dict) or not row:
            raise IncompleteStructureError(
                f"第 {i} 行不是非空映射", detail={"row_index": i, "row_repr": repr(row)}
            )
    expected = tuple(rows[0].keys())
    for i, row in enumerate(rows[1:], start=1):
        missing = [k for k in expected if k not in row]
        if missing:
            raise MissingFieldError(
                f"第 {i} 行缺少字段 {missing}",
                detail={"row_index": i, "missing": missing},
            )
        extra = [k for k in row if k not in expected]
        if extra:
            raise IncompleteStructureError(
                f"第 {i} 行出现未声明字段 {extra}",
                detail={"row_index": i, "extra": extra},
            )

    fields: list[Field] = []
    for name in expected:
        dtype: DataType | None = None
        for i, row in enumerate(rows):
            vt = _infer_value_type(row[name])
            if vt is None:
                continue
            if dtype is None:
                dtype = vt
            elif vt is not dtype:
                raise TypeConflictError(
                    f"字段 {name!r} 类型冲突: {dtype.value} vs {vt.value}",
                    detail={"field": name, "row_index": i,
                            "expected": dtype.value, "observed": vt.value},
                )
        if dtype is None:
            raise IncompleteStructureError(
                f"字段 {name!r} 全部为空，无法推断类型", detail={"field": name}
            )
        fields.append(Field(name, dtype))
    return TableSchema(tuple(fields))


def normalize_rows(rows: list[dict], schema: TableSchema) -> tuple[dict, ...]:
    """按已推断结构规范化所有行，输出不可变元组。"""
    out = []
    for row in rows:
        out.append({f.name: normalize_value(row[f.name], f.dtype) for f in schema.fields})
    return tuple(out)
