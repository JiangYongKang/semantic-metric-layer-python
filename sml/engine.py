"""查询执行引擎。

确定性保证：
- 执行前固定数据集版本与模型版本快照，查询期间不受并发更新影响；
- 关联仅多对一，执行时基于快照复核一侧键唯一性，杜绝隐性行数膨胀；
- 聚合全部基于原始行（AVG 以 sum/count 累积，绝不由预聚合结果再聚合）；
- 数值用 Decimal 精确累积，AVG/RATIO 仅在最终一步以固定精度、
  ROUND_HALF_EVEN 舍入，结果可复现；
- 输出按维度键排序，顺序稳定。

空值语义（固定且可解释）：
- 维度为 NULL 的行归入 NULL 组（全空时输出唯一 NULL 组）；
- 过滤谓词中任一侧为 NULL 时不匹配（行被排除）；
- SUM/COUNT_DISTINCT/MIN/MAX 忽略 NULL；全为 NULL 时结果为 None；
- COUNT 计行数（含 NULL 行）；AVG 的分母为非 NULL 值个数，全 NULL 时结果为 None；
- RATIO 分母为 0 或 None 时结果为 None。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from .dataset import DatasetRegistry, DatasetVersion
from .errors import AmbiguousJoinError, QueryError, UnknownFieldError
from .lineage import ColumnLineage
from .model import Agg, Metric, RatioMetric, SemanticModel
from .resources import ResourceGuard, ResourceLimits
from .schema import DataType, normalize_value

# AVG / RATIO 的最终输出精度：10 位小数，ROUND_HALF_EVEN，全局固定。
_OUTPUT_SCALE = Decimal("0.0000000001")
_DIV_PRECISION = 50

_OPS = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "gt": lambda a, b: a > b,
    "ge": lambda a, b: a >= b,
    "lt": lambda a, b: a < b,
    "le": lambda a, b: a <= b,
    "in": lambda a, b: a in b,
}


@dataclass(frozen=True)
class Filter:
    field: str  # "dataset.field"
    op: str     # eq / ne / gt / ge / lt / le / in
    value: object


@dataclass(frozen=True)
class Query:
    metrics: tuple[str, ...]
    dimensions: tuple[str, ...] = ()   # "dataset.field"
    filters: tuple[Filter, ...] = ()
    # 固定数据集版本（可复现历史查询）；空 = 使用当前最新版本
    pin_versions: tuple[tuple[str, int], ...] = ()

    def canonical(self) -> tuple:
        return (self.metrics, self.dimensions,
                tuple((f.field, f.op, repr(f.value)) for f in self.filters),
                self.pin_versions)


@dataclass
class QueryResult:
    columns: tuple[str, ...]
    rows: list[dict]
    lineage: tuple[ColumnLineage, ...]
    dataset_versions: dict[str, int]
    model_version: int

    def to_dict(self) -> dict:
        return {
            "columns": list(self.columns),
            "rows": [
                {k: (str(v) if isinstance(v, Decimal) else v) for k, v in r.items()}
                for r in self.rows
            ],
            "lineage": [l.to_dict() for l in self.lineage],
            "dataset_versions": dict(self.dataset_versions),
            "model_version": self.model_version,
        }


class QueryEngine:
    def __init__(self, registry: DatasetRegistry, model: SemanticModel,
                 limits: ResourceLimits | None = None) -> None:
        self._registry = registry
        self._model = model
        self._limits = limits or ResourceLimits()
        self._cache: dict[tuple, QueryResult] = {}
        self._cache_lock = threading.Lock()

    # ---- 公共入口 ----
    def run(self, query: Query) -> QueryResult:
        metrics = [self._model.metric(n) for n in query.metrics]
        # 比率指标隐式依赖其分子/分母基础指标，即使未出现在查询列中
        needed = {m.name: m for m in metrics if isinstance(m, Metric)}
        for m in metrics:
            if isinstance(m, RatioMetric):
                for dep in (m.num, m.den):
                    base = self._model.metric(dep)
                    needed.setdefault(dep, base)
        base_metrics = list(needed.values())
        fact_name = self._resolve_fact(base_metrics)
        involved = self._involved_datasets(fact_name, query)
        # 固定快照：版本号 + 不可变数据；pin_versions 可复现历史口径
        pin = dict(query.pin_versions)
        versions = {name: self._registry.get(name, pin.get(name))
                    for name in sorted(involved)}
        model_version = self._model.version
        key = (query.canonical(),
               tuple(sorted((n, v.version) for n, v in versions.items())),
               model_version)
        with self._cache_lock:
            hit = self._cache.get(key)
        if hit is not None:
            return hit
        # 计算失败不会写入缓存，也不会残留部分结果
        result = self._execute(query, metrics, base_metrics, fact_name,
                               versions, model_version)
        with self._cache_lock:
            self._cache.setdefault(key, result)
        return result

    # ---- 解析与校验 ----
    @staticmethod
    def _resolve_fact(metrics) -> str:
        facts = set()
        for m in metrics:
            if isinstance(m, RatioMetric):
                continue
            facts.add(m.dataset)
        if not facts:
            raise QueryError("查询至少需要一个基础指标", detail={})
        if len(facts) > 1:
            raise QueryError(
                f"指标来自多个事实数据集 {sorted(facts)}，无法确定统一口径",
                detail={"facts": sorted(facts)})
        return next(iter(facts))

    def _involved_datasets(self, fact: str, query: Query) -> set[str]:
        names = {fact}
        for ref in list(query.dimensions) + [f.field for f in query.filters]:
            names.add(self._split_ref(ref)[0])
        for name in names - {fact}:
            joins = [j for j in self._model.joins_for(fact) if j.right_dataset == name]
            if not joins:
                raise QueryError(
                    f"数据集 {name!r} 与事实表 {fact!r} 之间没有关联",
                    detail={"dataset": name, "fact": fact})
            if len(joins) > 1:
                raise AmbiguousJoinError(
                    f"事实表 {fact!r} 到 {name!r} 存在多条关联，无法选择",
                    detail={"fact": fact, "dataset": name})
        return names

    @staticmethod
    def _split_ref(ref: str) -> tuple[str, str]:
        parts = ref.split(".")
        if len(parts) != 2 or not all(parts):
            raise QueryError(
                f"字段引用 {ref!r} 必须是 'dataset.field' 形式",
                detail={"ref": ref})
        return parts[0], parts[1]

    @staticmethod
    def _resolve_field(ref: str, versions: dict[str, DatasetVersion]):
        ds_name, field_name = QueryEngine._split_ref(ref)
        ds = versions[ds_name]
        if not ds.schema.has(field_name):
            raise UnknownFieldError(
                f"数据集 {ds_name!r} 的版本 {ds.version} 中不存在字段 {field_name!r}"
                "（可能已被删除或改名）",
                detail={"dataset": ds_name, "version": ds.version,
                        "field": field_name})
        return ds, ds.schema.field(field_name)

    # ---- 执行 ----
    def _execute(self, query: Query, metrics, base_metrics, fact_name: str,
                 versions: dict[str, DatasetVersion], model_version: int) -> QueryResult:
        guard = ResourceGuard(self._limits)
        fact = versions[fact_name]

        # 指标 / 维度 / 过滤字段解析（基于固定快照，删列改名在此显式报错）
        fact_schema = versions[fact_name].schema
        for m in base_metrics:
            if m.field is not None and not fact_schema.has(m.field):
                raise UnknownFieldError(
                    f"指标 {m.name!r} 引用的字段 {fact_name}.{m.field} 在版本 "
                    f"{versions[fact_name].version} 中不存在（可能已被删除或改名）",
                    detail={"metric": m.name, "dataset": fact_name,
                            "field": m.field,
                            "version": versions[fact_name].version})
        dim_fields = [self._resolve_field(r, versions) for r in query.dimensions]
        filter_fields = [(self._resolve_field(f.field, versions), f)
                         for f in query.filters]

        # 关联：构建一侧索引并复核唯一性（数据可能在定义关联后被更新）
        join_indexes = {}
        for join in self._model.joins_for(fact_name):
            if join.right_dataset not in versions:
                continue  # 查询未引用该关联，跳过
            right = versions[join.right_dataset]
            guard.charge_rows(len(right.rows), stage=f"index:{join.name}")
            index: dict = {}
            for row in right.rows:
                k = row[join.right_key]
                if k in index:
                    from .errors import NonUniqueJoinKeyError
                    raise NonUniqueJoinKeyError(
                        f"关联 {join.name!r} 的一侧键 {k!r} 在当前数据版本中不唯一，"
                        "继续执行会产生行数膨胀，拒绝",
                        detail={"join": join.name, "key": repr(k),
                                "dataset_version": right.version})
                index[k] = row
            join_indexes[join.right_dataset] = (join, index)

        # 扫描 + 关联 + 过滤
        guard.charge_rows(len(fact.rows), stage="scan:fact")
        matched: list[dict] = []
        for row in fact.rows:
            joined = {fact_name: row}
            ok = True
            for ds_name, (join, index) in join_indexes.items():
                right_row = index.get(row[join.left_key])  # 左连接：无匹配则该数据集字段为 None
                joined[ds_name] = right_row
                if len(matched) % 1024 == 0:
                    guard.check_time(stage="join")
            for (ds, fld), flt in filter_fields:
                src = joined[ds.name]
                value = src[fld.name] if src is not None else None
                if not self._apply_filter(value, flt, fld.dtype):
                    ok = False
                    break
            if ok:
                matched.append(joined)
        guard.charge_rows(len(matched), stage="post-filter")

        # 分组（NULL 维度归入 NULL 组；排序键固定保证输出顺序确定）
        groups: dict[tuple, list[dict]] = {}
        for joined in matched:
            key = tuple(
                (joined[ds.name][fld.name] if joined[ds.name] is not None else None)
                for ds, fld in dim_fields)
            groups.setdefault(key, []).append(joined)
        guard.check_output(len(groups))

        # 聚合：全部基于原始行
        ratio_metrics = [m for m in metrics if isinstance(m, RatioMetric)]
        out_rows: list[dict] = []
        for key in sorted(groups, key=lambda k: tuple((v is not None, v) for v in k)):
            rows = groups[key]
            out: dict = {}
            for (ds, fld), v in zip(dim_fields, key):
                out[f"{ds.name}.{fld.name}"] = v
            values = {m.name: self._aggregate(m, rows, fact_name)
                      for m in base_metrics}
            for m in metrics:  # 按查询声明顺序输出
                out[m.name] = (self._divide(values[m.num], values[m.den])
                               if isinstance(m, RatioMetric) else values[m.name])
            out_rows.append(out)

        columns = tuple([f"{ds.name}.{fld.name}" for ds, fld in dim_fields]
                        + [m.name for m in metrics])
        lineage = self._lineage(metrics, base_metrics, dim_fields)
        return QueryResult(
            columns=columns, rows=out_rows, lineage=lineage,
            dataset_versions={n: v.version for n, v in versions.items()},
            model_version=model_version)

    @staticmethod
    def _apply_filter(value, flt: Filter, dtype: DataType) -> bool:
        if flt.op not in _OPS:
            raise QueryError(f"不支持的过滤操作 {flt.op!r}", detail={"op": flt.op})
        if value is None or flt.value is None:
            return False  # NULL 不匹配任何谓词
        if flt.op == "in":
            target = [normalize_value(v, dtype) for v in flt.value]
        else:
            target = normalize_value(flt.value, dtype)
        return _OPS[flt.op](value, target)

    @staticmethod
    def _aggregate(m: Metric, rows: list[dict], fact_name: str):
        if m.agg is Agg.COUNT:
            return len(rows)
        vals = [r[fact_name][m.field] for r in rows
                if r[fact_name][m.field] is not None]
        if m.agg is Agg.SUM:
            if not vals:
                return None
            total = Decimal(0)
            for v in vals:
                total += Decimal(v)
            return total
        if m.agg is Agg.AVG:
            if not vals:
                return None
            total = Decimal(0)
            for v in vals:
                total += Decimal(v)
            return QueryEngine._divide(total, Decimal(len(vals)))
        if m.agg is Agg.MIN:
            return min(vals) if vals else None
        if m.agg is Agg.MAX:
            return max(vals) if vals else None
        if m.agg is Agg.COUNT_DISTINCT:
            return len(set(vals))
        raise QueryError(f"不支持的聚合 {m.agg}", detail={})

    @staticmethod
    def _divide(num, den):
        """最终一步除法：固定精度与舍入；除零/空值 -> None。"""
        if num is None or den is None:
            return None
        num, den = Decimal(num), Decimal(den)
        if den == 0:
            return None
        with localcontext() as ctx:
            ctx.prec = _DIV_PRECISION
            return (num / den).quantize(_OUTPUT_SCALE, rounding=ROUND_HALF_EVEN)

    @staticmethod
    def _lineage(metrics, base_metrics, dim_fields) -> tuple[ColumnLineage, ...]:
        base_by_name = {m.name: m for m in base_metrics}

        def sources_of(m) -> tuple[str, ...]:
            if isinstance(m, RatioMetric):
                out: list[str] = []
                for dep in (m.num, m.den):
                    for s in sources_of(base_by_name[dep]):
                        if s not in out:
                            out.append(s)
                return tuple(out)
            return () if m.field is None else (f"{m.dataset}.{m.field}",)

        out = []
        for ds, fld in dim_fields:
            out.append(ColumnLineage(
                output_column=f"{ds.name}.{fld.name}", kind="dimension",
                source_fields=(f"{ds.name}.{fld.name}",),
                expression="identity"))
        for m in metrics:
            out.append(ColumnLineage(
                output_column=m.name, kind="metric",
                source_fields=sources_of(m), expression=m.expression()))
        return tuple(out)
