"""语义模型：关联关系与指标口径。

关联规则（定义时基于当前版本校验，执行时基于固定快照复核）：
- 仅支持多对一：left(多) -> right(一)，right 键必须唯一；
- right 键不唯一 -> NonUniqueJoinKeyError / AmbiguousJoinError（方向歧义或多对多）；
- 两侧键类型不一致 -> TypeConflictError；
- 任何关联不得产生隐性行数膨胀。

指标口径：
- 同名指标以不同定义重复注册 -> MetricConflictError（不静默取其一）；
- SUM/AVG/MIN/MAX 只能作用于数值字段；AVG 以 (sum, count) 存储，
  永远由原始行计算，不由预聚合结果再聚合。
"""
from __future__ import annotations

import enum
import threading
from dataclasses import dataclass

from .dataset import DatasetRegistry
from .errors import (AmbiguousJoinError, MetricConflictError, ModelError,
                     NonUniqueJoinKeyError, TypeConflictError, UnknownFieldError)
from .schema import DataType


class Agg(enum.Enum):
    SUM = "sum"
    COUNT = "count"
    AVG = "avg"
    MIN = "min"
    MAX = "max"
    COUNT_DISTINCT = "count_distinct"


@dataclass(frozen=True)
class Join:
    name: str
    left_dataset: str
    left_key: str
    right_dataset: str
    right_key: str


@dataclass(frozen=True)
class Metric:
    name: str
    dataset: str
    field: str | None  # COUNT 时为 None（计数行）
    agg: Agg

    def expression(self) -> str:
        target = self.field if self.field is not None else "*"
        return f"{self.agg.value}({self.dataset}.{target})"


class SemanticModel:
    """关联与口径的注册表；每次变更递增 version，用于缓存键与结果可复现。"""

    def __init__(self, registry: DatasetRegistry) -> None:
        self._registry = registry
        self._lock = threading.RLock()
        self._joins: dict[str, Join] = {}
        self._metrics: dict[str, Metric] = {}
        self._version = 0

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    # ---- 关联 ----
    def add_join(self, join: Join) -> None:
        with self._lock:
            if join.name in self._joins:
                raise MetricConflictError(
                    f"关联 {join.name!r} 已存在", detail={"join": join.name})
            self._validate_join(join)
            self._joins[join.name] = join
            self._version += 1

    def _validate_join(self, join: Join) -> None:
        left = self._registry.get(join.left_dataset)
        right = self._registry.get(join.right_dataset)
        for ds, key in ((left, join.left_key), (right, join.right_key)):
            if not ds.schema.has(key):
                raise UnknownFieldError(
                    f"数据集 {ds.name!r} 不存在关联键 {key!r}",
                    detail={"dataset": ds.name, "field": key})
        lt = left.schema.field(join.left_key).dtype
        rt = right.schema.field(join.right_key).dtype
        if lt is not rt:
            raise TypeConflictError(
                f"关联键类型不一致: {join.left_dataset}.{join.left_key}({lt.value}) "
                f"vs {join.right_dataset}.{join.right_key}({rt.value})",
                detail={"left_type": lt.value, "right_type": rt.value})
        self._check_cardinality(join, left.rows, right.rows)

    @staticmethod
    def _check_cardinality(join: Join, left_rows, right_rows) -> None:
        right_keys = [r[join.right_key] for r in right_rows]
        if None in right_keys:
            raise AmbiguousJoinError(
                f"关联 {join.name!r} 的一侧键含空值，匹配语义不确定",
                detail={"join": join.name})
        right_unique = len(set(right_keys)) == len(right_keys)
        if right_unique:
            return  # 多对一，合法
        left_keys = [r[join.left_key] for r in left_rows]
        left_unique = len(set(left_keys)) == len(left_keys)
        if left_unique:
            raise AmbiguousJoinError(
                f"关联 {join.name!r} 方向歧义：声明的一侧 "
                f"{join.right_dataset!r} 键不唯一，而多侧唯一，方向可能写反",
                detail={"join": join.name,
                        "right_dataset": join.right_dataset})
        raise NonUniqueJoinKeyError(
            f"关联 {join.name!r} 两侧键均不唯一，属于多对多，会产生行数膨胀，拒绝",
            detail={"join": join.name})

    # ---- 指标 ----
    def add_metric(self, metric: Metric) -> None:
        with self._lock:
            existing = self._metrics.get(metric.name)
            if existing is not None:
                if existing == metric:
                    return  # 同定义幂等
                raise MetricConflictError(
                    f"指标 {metric.name!r} 已存在且口径不同: "
                    f"{existing.expression()} vs {metric.expression()}",
                    detail={"metric": metric.name,
                            "existing": existing.expression(),
                            "incoming": metric.expression()})
            ds = self._registry.get(metric.dataset)
            if metric.agg is Agg.COUNT and metric.field is None:
                pass
            else:
                if metric.field is None or not ds.schema.has(metric.field):
                    raise UnknownFieldError(
                        f"指标 {metric.name!r} 引用了不存在的字段 "
                        f"{metric.dataset}.{metric.field}",
                        detail={"metric": metric.name, "dataset": metric.dataset,
                                "field": metric.field})
                dtype = ds.schema.field(metric.field).dtype
                if metric.agg in (Agg.SUM, Agg.AVG) and dtype not in (DataType.INT, DataType.DECIMAL):
                    raise MetricConflictError(
                        f"指标 {metric.name!r} 的 {metric.agg.value} 只能作用于数值字段，"
                        f"实际为 {dtype.value}",
                        detail={"metric": metric.name, "field": metric.field,
                                "dtype": dtype.value})
            self._metrics[metric.name] = metric
            self._version += 1

    def add_ratio_metric(self, ratio: "RatioMetric") -> None:
        with self._lock:
            existing = self._metrics.get(ratio.name)
            if existing is not None:
                if existing == ratio:
                    return
                raise MetricConflictError(
                    f"指标 {ratio.name!r} 已存在且口径不同",
                    detail={"metric": ratio.name})
            for dep in (ratio.num, ratio.den):
                base = self._metrics.get(dep)
                if base is None or isinstance(base, RatioMetric):
                    raise UnknownFieldError(
                        f"比率指标 {ratio.name!r} 依赖未定义或非基础指标 {dep!r}",
                        detail={"metric": ratio.name, "dependency": dep})
            self._metrics[ratio.name] = ratio
            self._version += 1

    def metric(self, name: str) -> "Metric | RatioMetric":
        with self._lock:
            try:
                return self._metrics[name]
            except KeyError:
                raise UnknownFieldError(f"未定义的指标 {name!r}",
                                        detail={"metric": name}) from None

    def joins_for(self, dataset: str) -> tuple[Join, ...]:
        with self._lock:
            return tuple(j for j in self._joins.values() if j.left_dataset == dataset)

    @property
    def joins(self) -> tuple[Join, ...]:
        with self._lock:
            return tuple(self._joins.values())


@dataclass(frozen=True)
class RatioMetric:
    """比率指标：每组分别按 num/den 各自口径求值后再相除。

    除零语义固定：分母为 0 或 None 时结果为 None（不报错、不静默给 0）。
    """
    name: str
    num: str  # 分子指标名
    den: str  # 分母指标名

    def expression(self) -> str:
        return f"ratio({self.num} / {self.den})"
