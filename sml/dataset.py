"""数据集注册：结构推断、可区分拒绝原因、版本化内容。

拒绝规则（不静默接受）：

- 0 条记录 / 0 个列 -> ``incomplete_structure``
- 记录间列集合不一致（缺列或多列）-> ``field_missing``
- 某列在所有记录中都是空值（无法推断类型）-> ``field_missing``
- 同列值无法归一为唯一逻辑类型 -> ``type_conflict``
- 主键字段不在列集合中 -> ``field_missing``
- 主键值为空或重复 -> ``incomplete_structure`` / ``duplicate_primary_key``

更新规则：

- 内容更新只允许「列集合不变」或「仅新增列」；删列/改名一律拒绝，
  避免查询静默错列。新增列对旧记录以 NULL 补（缺省语义稳定且显式）。
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import (
    DatasetAlreadyExistsError,
    DatasetNotFoundError,
    DuplicatePrimaryKeyError,
    FieldMissingError,
    IncompleteStructureError,
    SourceUpdateError,
)
from .types import (
    FieldSchema,
    LogicalType,
    TableSchema,
    infer_value,
    merge_types,
)


@dataclass(frozen=True)
class Dataset:
    """一个已注册数据集的不可变版本快照。"""

    name: str
    schema: TableSchema
    records: tuple[dict, ...]
    version: int


def _column_names(records: list[dict], *, name: str) -> list[str]:
    """以第一条记录的列序为基准，校验所有记录列集合完全一致。"""
    if not records:
        return []
    base = list(records[0].keys())
    base_set = set(base)
    if len(base_set) != len(base):
        raise IncompleteStructureError(
            f"数据集 '{name}' 的首条记录存在重复列名",
            details={"dataset": name, "columns": base},
        )
    for idx, rec in enumerate(records[1:], start=2):
        keys = set(rec.keys())
        missing = [c for c in base if c not in keys]
        extra = [c for c in rec.keys() if c not in base_set]
        if missing or extra:
            raise FieldMissingError(
                f"数据集 '{name}' 第 {idx} 行与首行列集合不一致"
                f"（缺列 {missing or '无'}，多列 {extra or '无'}），拒绝静默对齐",
                details={
                    "dataset": name,
                    "row_index": idx,
                    "missing_columns": missing,
                    "unexpected_columns": extra,
                },
            )
    return base


def infer_schema(
    records: list[dict],
    *,
    primary_key: tuple[str, ...] = (),
    name: str = "",
) -> TableSchema:
    """从记录推断结构；问题数据抛可区分错误。"""
    if not records:
        raise IncompleteStructureError(
            f"数据集 '{name or '<未命名>'}' 没有任何记录，无法推断结构",
            details={"dataset": name},
        )
    columns = _column_names(records, name=name)
    if not columns:
        raise IncompleteStructureError(
            f"数据集 '{name}' 没有任何列",
            details={"dataset": name},
        )
    for pk in primary_key:
        if pk not in columns:
            raise FieldMissingError(
                f"数据集 '{name}' 声明的主键列 '{pk}' 不存在",
                details={"dataset": name, "primary_key": list(primary_key), "missing": pk},
            )

    fields: list[FieldSchema] = []
    for col in columns:
        inferred: LogicalType | None = None
        nullable = False
        for rec in records:
            v = rec[col]
            if v is None:
                nullable = True
                continue
            t = infer_value(v, field=col)
            inferred = t if inferred is None else merge_types(inferred, t, field=col)
        if inferred is None:
            raise FieldMissingError(
                f"数据集 '{name}' 的列 '{col}' 在全部 {len(records)} 行中均为空，无法推断类型",
                details={"dataset": name, "field": col, "reason": "all_null"},
            )
        fields.append(FieldSchema(name=col, logical_type=inferred, nullable=nullable))

    if primary_key:
        seen: set[tuple] = set()
        for idx, rec in enumerate(records, start=1):
            vals = tuple(rec[pk] for pk in primary_key)
            if any(v is None for v in vals):
                raise IncompleteStructureError(
                    f"数据集 '{name}' 第 {idx} 行主键 {list(primary_key)} 含空值",
                    details={"dataset": name, "row_index": idx, "primary_key": list(primary_key)},
                )
            if vals in seen:
                raise DuplicatePrimaryKeyError(
                    f"数据集 '{name}' 的主键 {list(primary_key)} 出现重复值 {list(vals)!r}",
                    details={
                        "dataset": name,
                        "primary_key": list(primary_key),
                        "duplicate_value": [_jsonable(v) for v in vals],
                        "row_index": idx,
                    },
                )
            seen.add(vals)

    return TableSchema(fields=tuple(fields), primary_key=tuple(primary_key))


def _jsonable(v: object) -> object:
    from decimal import Decimal

    if isinstance(v, Decimal):
        return str(v)
    return v


def _check_types_against_schema(
    records: list[dict], schema: TableSchema, *, name: str
) -> None:
    """按既有 schema 复验数据（值类型必须仍可归入列类型）。"""
    for idx, rec in enumerate(records, start=1):
        for fs in schema.fields:
            v = rec.get(fs.name)
            if v is None:
                continue
            t = infer_value(v, field=fs.name)
            merge_types(fs.logical_type, t, field=fs.name)


class DatasetRegistry:
    """数据集注册中心：注册、内容更新、按名取当前快照。"""

    def __init__(self) -> None:
        self._datasets: dict[str, Dataset] = {}

    def register(
        self,
        name: str,
        records: list[dict],
        *,
        primary_key: tuple[str, ...] = (),
    ) -> Dataset:
        if name in self._datasets:
            raise DatasetAlreadyExistsError(
                f"数据集 '{name}' 已注册（version={self._datasets[name].version}），"
                "如需更新请使用 replace_data",
                details={"dataset": name, "existing_version": self._datasets[name].version},
            )
        schema = infer_schema(records, primary_key=primary_key, name=name)
        ds = Dataset(name=name, schema=schema, records=tuple(records), version=1)
        self._datasets[name] = ds
        return ds

    def replace_data(self, name: str, records: list[dict]) -> Dataset:
        """整体替换数据内容。

        - 列集合相同：类型必须与既有 schema 一致；主键仍需唯一非空。
        - 仅新增列：允许，旧行新列补 NULL，schema 升版，nullable=True。
        - 删列/改名（列减少或类型漂移）：明确拒绝，杜绝静默错列。
        """
        old = self.get(name)
        old_cols = [f.name for f in old.schema.fields]
        if not records:
            raise IncompleteStructureError(
                f"数据集 '{name}' 的更新数据为空，拒绝用空内容覆盖既有结构",
                details={"dataset": name},
            )
        new_cols = _column_names(records, name=name)
        removed = [c for c in old_cols if c not in new_cols]
        if removed:
            raise SourceUpdateError(
                f"数据集 '{name}' 的更新删除/改名了列 {removed}，拒绝更新以防查询静默错列",
                details={
                    "dataset": name,
                    "removed_or_renamed_columns": removed,
                    "old_version": old.version,
                },
            )
        added = [c for c in new_cols if c not in old_cols]
        # 旧列部分：先按旧 schema 复验
        old_records_view = [{c: r[c] for c in old_cols} for r in records]
        _check_types_against_schema(old_records_view, old.schema, name=name)
        # 新列部分：对新列单独推断；并给全部行补齐（新数据源行本身已有值）
        added_fields: list[FieldSchema] = []
        for col in added:
            inferred: LogicalType | None = None
            nullable = False
            for r in records:
                v = r[col]
                if v is None:
                    nullable = True
                    continue
                t = infer_value(v, field=col)
                inferred = t if inferred is None else merge_types(inferred, t, field=col)
            if inferred is None:
                raise FieldMissingError(
                    f"数据集 '{name}' 新增列 '{col}' 全部为空，无法推断类型",
                    details={"dataset": name, "field": col, "reason": "all_null"},
                )
            added_fields.append(
                FieldSchema(name=col, logical_type=inferred, nullable=True)
            )

        # 主键复验
        if old.schema.primary_key:
            pks = old.schema.primary_key
            seen: set[tuple] = set()
            for idx, r in enumerate(records, start=1):
                vals = tuple(r[p] for p in pks)
                if any(v is None for v in vals):
                    raise IncompleteStructureError(
                        f"数据集 '{name}' 更新后第 {idx} 行主键含空值",
                        details={"dataset": name, "row_index": idx},
                    )
                if vals in seen:
                    raise DuplicatePrimaryKeyError(
                        f"数据集 '{name}' 更新后主键 {list(pks)} 出现重复 {list(vals)!r}",
                        details={"dataset": name, "duplicate_value": [_jsonable(v) for v in vals]},
                    )
                seen.add(vals)

        merged_fields = tuple(list(old.schema.fields) + added_fields)
        new_schema = TableSchema(fields=merged_fields, primary_key=old.schema.primary_key)
        ds = Dataset(
            name=name,
            schema=new_schema,
            records=tuple(dict(r) for r in records),
            version=old.version + 1,
        )
        self._datasets[name] = ds
        return ds

    def get(self, name: str) -> Dataset:
        if name not in self._datasets:
            raise DatasetNotFoundError(
                f"数据集 '{name}' 不存在", details={"dataset": name}
            )
        return self._datasets[name]

    def names(self) -> list[str]:
        return sorted(self._datasets)
