"""聚合口径定义：维度口径、基础指标口径、比率口径。

口径冲突的拒绝规则：

- 口径名全局唯一（维度/指标/比率共享命名空间），重复登记即 ``caliber_conflict``
- 指标字段必须属于所声明数据集且为数值类型，否则 ``invalid_caliber``
- ``count`` / ``count_distinct`` 可不带字段（行数/去重行数）
- 比率的分子分母必须是**同一数据集**上的基础口径（sum/count/count_distinct/
  min/max），不得引用 avg 或另一个比率——平均类口径只能由引擎在明细上
  以 sum/count 重新计算，杜绝「对预聚合结果再聚合」
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .errors import (
    CaliberConflictError,
    InvalidCaliberError,
)
from .types import LogicalType


class AggKind(str, Enum):
    SUM = "sum"
    COUNT = "count"
    AVG = "avg"
    MIN = "min"
    MAX = "max"
    COUNT_DISTINCT = "count_distinct"

    @property
    def needs_numeric_field(self) -> bool:
        return self in (AggKind.SUM, AggKind.AVG, AggKind.MIN, AggKind.MAX)


# 允许作为比率分子/分母的基础聚合（avg 被显式排除）
_RATIO_BASE_ALLOWED = frozenset(
    {AggKind.SUM, AggKind.COUNT, AggKind.COUNT_DISTINCT, AggKind.MIN, AggKind.MAX}
)


@dataclass(frozen=True)
class DimensionSpec:
    """维度口径：来自某数据集某字段。"""

    name: str
    dataset: str
    field: str

    def source(self) -> str:
        return f"{self.dataset}.{self.field}"


@dataclass(frozen=True)
class MeasureSpec:
    """基础指标口径：聚合方式 + 来源字段（count 类可无字段）。"""

    name: str
    dataset: str
    agg: AggKind
    field: str | None = None

    def sources(self) -> tuple[str, ...]:
        return (f"{self.dataset}.{self.field}",) if self.field else ()


@dataclass(frozen=True)
class RatioSpec:
    """比率口径：同数据集上两个基础口径之比，在明细语义下计算。"""

    name: str
    dataset: str
    numerator: str
    denominator: str


class CaliberBook:
    """口径册：登记并校验维度/指标/比率。"""

    def __init__(self, datasets: "object") -> None:  # DatasetRegistry
        self._datasets = datasets
        self._dimensions: dict[str, DimensionSpec] = {}
        self._measures: dict[str, MeasureSpec] = {}
        self._ratios: dict[str, RatioSpec] = {}

    # ---- 查询辅助 ----
    def dimension(self, name: str) -> DimensionSpec:
        return self._dimensions[name]

    def measure(self, name: str) -> MeasureSpec:
        return self._measures[name]

    def ratio(self, name: str) -> RatioSpec:
        return self._ratios[name]

    def resolve_metric(self, name: str):
        """返回 (kind, spec)，kind ∈ {'measure','ratio'}。"""
        if name in self._measures:
            return "measure", self._measures[name]
        if name in self._ratios:
            return "ratio", self._ratios[name]
        from .errors import CaliberNotFoundError

        raise CaliberNotFoundError(
            f"指标口径 '{name}' 不存在", details={"caliber": name}
        )

    def is_dimension(self, name: str) -> bool:
        return name in self._dimensions

    # ---- 登记 ----
    def _claim_name(self, name: str, kind: str) -> None:
        for bucket, label in (
            (self._dimensions, "dimension"),
            (self._measures, "measure"),
            (self._ratios, "ratio"),
        ):
            if name in bucket:
                raise CaliberConflictError(
                    f"口径名 '{name}' 已被 {label} 占用，不能再定义为 {kind}",
                    details={"name": name, "existing_kind": label, "new_kind": kind},
                )

    def _field(self, dataset: str, field: str | None):
        ds = self._datasets.get(dataset)
        if field is None:
            return ds, None
        fs = ds.schema.field_map.get(field)
        if fs is None:
            raise InvalidCaliberError(
                f"口径引用的字段 '{dataset}.{field}' 不存在",
                details={"dataset": dataset, "field": field},
            )
        return ds, fs

    def add_dimension(self, spec: DimensionSpec) -> None:
        self._claim_name(spec.name, "dimension")
        self._field(spec.dataset, spec.field)
        self._dimensions[spec.name] = spec

    def add_measure(self, spec: MeasureSpec) -> None:
        self._claim_name(spec.name, "measure")
        if spec.field is None:
            if spec.agg not in (AggKind.COUNT, AggKind.COUNT_DISTINCT):
                raise InvalidCaliberError(
                    f"指标 '{spec.name}' 的聚合 {spec.agg.value} 必须指定字段",
                    details={"measure": spec.name, "agg": spec.agg.value},
                )
            self._field(spec.dataset, None)
        else:
            _, fs = self._field(spec.dataset, spec.field)
            if spec.agg.needs_numeric_field and fs.logical_type not in (
                LogicalType.INTEGER,
                LogicalType.DECIMAL,
            ):
                raise InvalidCaliberError(
                    f"指标 '{spec.name}' 对非数值字段 '{spec.dataset}.{spec.field}'"
                    f"（{fs.logical_type.value}）做 {spec.agg.value}，口径非法",
                    details={
                        "measure": spec.name,
                        "field": f"{spec.dataset}.{spec.field}",
                        "field_type": fs.logical_type.value,
                        "agg": spec.agg.value,
                    },
                )
        self._measures[spec.name] = spec

    def add_ratio(self, spec: RatioSpec) -> None:
        self._claim_name(spec.name, "ratio")
        num = self._measures.get(spec.numerator)
        den = self._measures.get(spec.denominator)
        if num is None or den is None:
            raise InvalidCaliberError(
                f"比率 '{spec.name}' 的分子/分母必须是已定义的基础指标: "
                f"numerator={spec.numerator!r}, denominator={spec.denominator!r}",
                details={
                    "ratio": spec.name,
                    "numerator_defined": num is not None,
                    "denominator_defined": den is not None,
                },
            )
        if num.dataset != spec.dataset or den.dataset != spec.dataset:
            raise InvalidCaliberError(
                f"比率 '{spec.name}' 的分子分母必须属于同一数据集 '{spec.dataset}'",
                details={
                    "ratio": spec.name,
                    "ratio_dataset": spec.dataset,
                    "numerator_dataset": num.dataset,
                    "denominator_dataset": den.dataset,
                },
            )
        if num.agg not in _RATIO_BASE_ALLOWED or den.agg not in _RATIO_BASE_ALLOWED:
            raise InvalidCaliberError(
                f"比率 '{spec.name}' 不得基于 avg 或其他比率构造，"
                "平均类指标必须由明细的 sum/count 计算",
                details={
                    "ratio": spec.name,
                    "numerator_agg": num.agg.value,
                    "denominator_agg": den.agg.value,
                },
            )
        self._ratios[spec.name] = spec

    # ---- 快照（供模型版本化）----
    def snapshot(self) -> "CaliberBook":
        book = CaliberBook(self._datasets)
        book._dimensions = dict(self._dimensions)
        book._measures = dict(self._measures)
        book._ratios = dict(self._ratios)
        return book

    def metric_names(self) -> list[str]:
        return sorted(set(self._measures) | set(self._ratios))
