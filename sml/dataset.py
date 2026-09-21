"""数据集注册与版本化快照。

- register/update 都会重新推断结构，拒绝原因来自 schema 层，可区分。
- update 允许新增字段（缺省语义：历史版本中该字段不存在，查询按 UNKNOWN_FIELD 拒绝，
  绝不静默错列）；允许删列/改名（查询旧列名时显式报错）；
  保留字段的类型变更一律拒绝（TypeConflictError）。
- 每次 update 产生新的不可变版本；历史版本保留，历史查询可复现。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

from .errors import SchemaError, TypeConflictError
from .schema import TableSchema, infer_schema, normalize_rows


@dataclass(frozen=True)
class DatasetVersion:
    name: str
    version: int
    schema: TableSchema
    rows: tuple[dict, ...]


class DatasetRegistry:
    """线程安全的数据集注册表，保存全部历史版本。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._datasets: dict[str, list[DatasetVersion]] = {}

    def register(self, name: str, rows: list[dict]) -> DatasetVersion:
        with self._lock:
            if name in self._datasets:
                raise SchemaError(f"数据集 {name!r} 已存在，请使用 update",
                                  detail={"dataset": name})
            return self._append(name, rows)

    def update(self, name: str, rows: list[dict]) -> DatasetVersion:
        with self._lock:
            if name not in self._datasets:
                raise SchemaError(f"数据集 {name!r} 不存在，请使用 register",
                                  detail={"dataset": name})
            return self._append(name, rows)

    def _append(self, name: str, rows: list[dict]) -> DatasetVersion:
        schema = infer_schema(rows)
        versions = self._datasets.setdefault(name, [])
        if versions:
            prev = versions[-1].schema
            for f in schema.fields:
                if prev.has(f.name) and prev.field(f.name).dtype is not f.dtype:
                    raise TypeConflictError(
                        f"数据集 {name!r} 字段 {f.name!r} 类型由 "
                        f"{prev.field(f.name).dtype.value} 变为 {f.dtype.value}，拒绝更新",
                        detail={"dataset": name, "field": f.name,
                                "before": prev.field(f.name).dtype.value,
                                "after": f.dtype.value},
                    )
        dv = DatasetVersion(name, len(versions) + 1, schema, normalize_rows(rows, schema))
        versions.append(dv)
        return dv

    def get(self, name: str, version: int | None = None) -> DatasetVersion:
        """取指定版本（默认最新）的不可变快照；快照不受后续 update 影响。"""
        with self._lock:
            versions = self._datasets.get(name)
            if not versions:
                raise SchemaError(f"数据集 {name!r} 未注册", detail={"dataset": name})
            if version is None:
                return versions[-1]
            if not 1 <= version <= len(versions):
                raise SchemaError(
                    f"数据集 {name!r} 不存在版本 {version}",
                    detail={"dataset": name, "version": version,
                            "available": len(versions)},
                )
            return versions[version - 1]

    def current_version(self, name: str) -> int:
        return self.get(name).version
