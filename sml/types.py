"""逻辑类型系统：从原始值推断统一的逻辑类型。

只支持四种逻辑类型，覆盖本地数据场景：

- ``INTEGER``：精确整数（任意精度）
- ``DECIMAL``：精确十进制数（用 :class:`decimal.Decimal` 承载，金额/比率用）
- ``TEXT``：文本
- ``BOOL``：布尔

NULL 本身没有类型；一个字段的类型由其全部非空值推断，
出现无法归一的值即类型冲突。
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum

from .errors import TypeConflictError

_INT_RE = re.compile(r"^[+-]?\d+$")
# Decimal 能解析但不应作为数值接受的特殊串
_NON_FINITE = {"nan", "-nan", "+nan", "inf", "-inf", "+inf", "infinity", "-infinity", "+infinity"}


class LogicalType(str, Enum):
    INTEGER = "integer"
    DECIMAL = "decimal"
    TEXT = "text"
    BOOL = "bool"

    @property
    def is_numeric(self) -> bool:
        return self in (LogicalType.INTEGER, LogicalType.DECIMAL)


# 类型推广序：int + decimal => decimal；任何数值 + text => 冲突（不静默转字符串）
_PROMOTION: dict[tuple[LogicalType, LogicalType], LogicalType] = {
    (LogicalType.INTEGER, LogicalType.DECIMAL): LogicalType.DECIMAL,
    (LogicalType.DECIMAL, LogicalType.INTEGER): LogicalType.DECIMAL,
}


def merge_types(a: LogicalType, b: LogicalType, *, field: str = "") -> LogicalType:
    """合并两个字段类型；不可归一则抛 :class:`TypeConflictError`。"""
    if a == b:
        return a
    promoted = _PROMOTION.get((a, b))
    if promoted is not None:
        return promoted
    raise TypeConflictError(
        f"字段 '{field}' 的值同时呈现 {a.value} 与 {b.value}，无法归一为唯一类型",
        details={"field": field, "types": [a.value, b.value]},
    )


def infer_value(value: object, *, field: str = "") -> LogicalType:
    """推断单个非空值的逻辑类型。"""
    if isinstance(value, bool):
        return LogicalType.BOOL
    if isinstance(value, int):
        return LogicalType.INTEGER
    if isinstance(value, Decimal):
        return LogicalType.DECIMAL
    if isinstance(value, float):
        # float 不允许静默进入精确数值体系，必须显式转 Decimal 的调用方处理
        raise TypeConflictError(
            f"字段 '{field}' 出现 float 值 {value!r}：二进制浮点不允许进入精确数值体系",
            details={"field": field, "value": repr(value)},
        )
    if isinstance(value, str):
        return LogicalType.TEXT
    if isinstance(value, (_dt.date, _dt.datetime, _dt.time)):
        raise TypeConflictError(
            f"字段 '{field}' 出现日期时间值 {value!r}：当前版本不支持该逻辑类型",
            details={"field": field, "value": str(value), "unsupported": "datetime"},
        )
    raise TypeConflictError(
        f"字段 '{field}' 出现不支持的值类型 {type(value).__name__}: {value!r}",
        details={"field": field, "value": repr(value), "python_type": type(value).__name__},
    )


def coerce_value(value: object, logical: LogicalType, *, field: str = "") -> object:
    """按已声明/已推断的逻辑类型校验并规整一个值。

    - None 透传为 None（空值语义统一为 None）
    - 数值字符串按目标数值类型解析（CSV 场景）；解析失败即类型冲突
    - bool 不与数值隐式互换
    """
    if value is None or value == "":
        return None
    if logical is LogicalType.BOOL:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            low = value.strip().lower()
            if low in ("true", "1", "yes"):
                return True
            if low in ("false", "0", "no"):
                return False
        raise TypeConflictError(
            f"字段 '{field}' 的值 {value!r} 无法解释为 bool",
            details={"field": field, "value": repr(value), "expected": "bool"},
        )
    if logical is LogicalType.TEXT:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, Decimal)):
            return str(value)
        if isinstance(value, str):
            return value
        raise TypeConflictError(
            f"字段 '{field}' 的值 {value!r} 无法解释为 text",
            details={"field": field, "value": repr(value), "expected": "text"},
        )
    if logical is LogicalType.INTEGER:
        if isinstance(value, bool):
            raise TypeConflictError(
                f"字段 '{field}' 的布尔值 {value!r} 不得隐式当作整数",
                details={"field": field, "value": repr(value), "expected": "integer"},
            )
        if isinstance(value, int):
            return value
        if isinstance(value, Decimal):
            if value == value.to_integral_value():
                return int(value)
            raise TypeConflictError(
                f"字段 '{field}' 的值 {value} 含小数部分，无法作为整数",
                details={"field": field, "value": str(value), "expected": "integer"},
            )
        if isinstance(value, str):
            try:
                d = Decimal(value.strip())
            except InvalidOperation:
                pass
            else:
                if d == d.to_integral_value() and "." not in value.strip():
                    return int(d)
        raise TypeConflictError(
            f"字段 '{field}' 的值 {value!r} 无法解释为整数",
            details={"field": field, "value": repr(value), "expected": "integer"},
        )
    if logical is LogicalType.DECIMAL:
        if isinstance(value, bool):
            raise TypeConflictError(
                f"字段 '{field}' 的布尔值 {value!r} 不得隐式当作数值",
                details={"field": field, "value": repr(value), "expected": "decimal"},
            )
        if isinstance(value, int):
            return Decimal(value)
        if isinstance(value, Decimal):
            return value
        if isinstance(value, str):
            try:
                return Decimal(value.strip())
            except InvalidOperation:
                pass
        raise TypeConflictError(
            f"字段 '{field}' 的值 {value!r} 无法解释为 decimal",
            details={"field": field, "value": repr(value), "expected": "decimal"},
        )
        # unreachable
    raise TypeConflictError(
        f"字段 '{field}' 的目标逻辑类型 {logical} 不受支持",
        details={"field": field, "expected": logical.value},
    )


def parse_scalar(text: str, *, field: str = "") -> object:
    """把 CSV 单元格文本确定性地解析为原生标量。

    规则固定且可解释：

    - 空串/纯空白 -> None（空值）
    - ``true/false/yes/no``（大小写不敏感）-> bool
    - 纯整数字面量 -> int
    - 十进制数（含指数、正负号）-> :class:`Decimal`
    - ``nan``/``inf`` 等非有限值一律拒绝（不得静默变数值）
    - 其余 -> 原样字符串（已做首尾空白规整的仅数值；文本不 strip）
    """
    if text == "" or text.strip() == "":
        return None
    low = text.strip().lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in _NON_FINITE:
        raise TypeConflictError(
            f"字段 '{field}' 的文本 {text!r} 是非有限数值，不允许进入数值体系",
            details={"field": field, "value": text},
        )
    if _INT_RE.match(text.strip()):
        return int(text.strip())
    try:
        d = Decimal(text.strip())
    except InvalidOperation:
        d = None
    if d is not None and d.is_finite():
        return d
    return text


@dataclass(frozen=True)
class FieldSchema:
    """字段结构：名称 + 逻辑类型 + 是否可为空（推断阶段记录是否出现过空值）。"""

    name: str
    logical_type: LogicalType
    nullable: bool = True

    def to_dict(self) -> dict:
        return {"name": self.name, "type": self.logical_type.value, "nullable": self.nullable}


@dataclass(frozen=True)
class TableSchema:
    """表结构：有序字段列表 + 主键字段名集合（可为空，表示无主键）。"""

    fields: tuple[FieldSchema, ...]
    primary_key: tuple[str, ...] = ()

    @property
    def field_map(self) -> dict[str, FieldSchema]:
        return {f.name: f for f in self.fields}

    def to_dict(self) -> dict:
        return {
            "fields": [f.to_dict() for f in self.fields],
            "primary_key": list(self.primary_key),
        }
