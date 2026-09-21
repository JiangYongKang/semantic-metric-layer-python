"""查询引擎：在语义模型快照上执行过滤 / 连接 / 分组 / 聚合。

固定结果语义（任何数据下均确定、可解释、可复现）：

- 连接：以指标所在数据集为事实表（查询内所有指标必须同一数据集），
  维度沿唯一的 many->one / one_to_one 路径做字典查找式左连接，
  one 侧键唯一已在注册期校验，故连接不可能产生行数膨胀；
  匹配不到时维度值为 NULL。
- 空值：分组键 NULL 归入固定空值桶（输出为 None，排序恒在最前）；
  sum / count / count_distinct 忽略空值，无参与值时分别为 0 / 0 / 0；
  avg / min / max 无参与值时为 NULL。
- 除零：avg 与比率分母为 0 或 NULL 时结果恒为 NULL（不抛异常、不产生 inf）。
- 平均类指标（avg、比率）一律在明细行上以 sum/count 等基础聚合现场计算，
  绝不使用预先聚合结果再聚合。
- 精度：全程 int / Decimal；Decimal 上下文 prec=65、ROUND_HALF_UP，
  除法结果统一量化到 10 位小数（ROUND_HALF_UP），跨平台可复现。
- 顺序：分组结果按维度值确定性升序（空值桶最前）。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP, localcontext, getcontext

from .caliber import AggKind, MeasureSpec
from .errors import QueryError, ResourceLimitError
from .limits import BudgetGuard, ResourceBudget, approx_row_bytes
from .lineage import ColumnLineage, Lineage
from .types import LogicalType, coerce_value

getcontext().prec = 65
_QUANTUM = Decimal("1E-10")

def _safe_div(numerator: Decimal | int | None, denominator: Decimal | int | None):
    """固定除法语义：分母为 None 或 0 -> None；否则量化到 10 位小数。

    数值层只产出确定的 Decimal；对外文本形态由序列化层统一
    （HTTP 用 ``format(value, 'f')``，零值固定显示为 0.0000000000）。
    """
    if denominator is None or denominator == 0:
        return None
    with localcontext() as ctx:
        ctx.prec = 65
        ctx.rounding = ROUND_HALF_UP
        raw = Decimal(numerator) / Decimal(denominator)
    return raw.quantize(_QUANTUM, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Filter:
    """维度过滤：``values`` 成员判定。

    - ``in``：行值 ∈ values；空值行仅在 values 显式含 None 时选中
    - ``not in``：``in`` 的确定补集（in 不选空桶时 not-in 选空桶）
    """

    dimension: str
    values: tuple[object, ...]
    negate: bool = False


@dataclass(frozen=True)
class Query:
    dimensions: tuple[str, ...] = ()
    measures: tuple[str, ...] = ()
    filters: tuple[Filter, ...] = ()


@dataclass(frozen=True)
class QueryResult:
    columns: tuple[str, ...]
    rows: tuple[tuple[object, ...], ...]
    lineage: Lineage
    model_version: int
    truncated: bool = False
    resource_stats: dict | None = None


class _Accumulator:
    """单分组内各输出指标的累加状态。"""

    __slots__ = ("sum_v", "cnt", "min_v", "max_v", "distinct")

    def __init__(self) -> None:
        # 按输出列名存放
        self.sum_v: dict[str, Decimal | int] = {}
        self.cnt: dict[str, int] = {}
        self.min_v: dict[str, object] = {}
        self.max_v: dict[str, object] = {}
        self.distinct: dict[str, set] = {}

    def add_measure_value(self, col: str, agg: AggKind, v: object) -> None:
        if agg is AggKind.COUNT:
            self.cnt[col] = self.cnt.get(col, 0) + 1
            return
        if v is None:
            # 除 count(*) 外所有聚合忽略空值；确保槽位存在以输出 0/NULL
            self.cnt.setdefault(col, 0)
            self.sum_v.setdefault(col, 0)
            return
        self.cnt[col] = self.cnt.get(col, 0) + 1
        if agg is AggKind.SUM or agg is AggKind.AVG:
            cur = self.sum_v.get(col, 0)
            self.sum_v[col] = cur + v
        elif agg is AggKind.MIN:
            if col not in self.min_v or v < self.min_v[col]:
                self.min_v[col] = v
        elif agg is AggKind.MAX:
            if col not in self.max_v or v > self.max_v[col]:
                self.max_v[col] = v
        elif agg is AggKind.COUNT_DISTINCT:
            self.distinct.setdefault(col, set()).add(v)


class QueryEngine:
    def __init__(self, model: object, budget: ResourceBudget | None = None) -> None:
        self._model = model
        self._budget = budget or ResourceBudget()

    # ------------------------------------------------------------------
    def run(self, query: Query) -> QueryResult:
        guard = BudgetGuard(self._budget)
        model = self._model
        book = model.calibers

        if not query.measures:
            raise QueryError("查询必须至少包含一个指标", details={"reason": "no_measures"})
        if len(set(query.measures)) != len(query.measures):
            raise QueryError("查询中的指标重复", details={"measures": list(query.measures)})
        if len(set(query.dimensions)) != len(query.dimensions):
            raise QueryError("查询中的维度重复", details={"dimensions": list(query.dimensions)})
        overlap = set(query.measures) & set(query.dimensions)
        if overlap:
            raise QueryError(f"列名同时作为维度和指标: {sorted(overlap)}",
                             details={"columns": sorted(overlap)})

        # 解析指标（measure 或 ratio），并确定唯一事实数据集
        resolved: list[tuple[str, str, object]] = []  # (col, kind, spec)
        fact: str | None = None
        for name in query.measures:
            kind, spec = book.resolve_metric(name)
            ds = spec.dataset
            if fact is None:
                fact = ds
            elif fact != ds:
                raise QueryError(
                    f"一次查询的所有指标必须来自同一数据集；"
                    f"已遇到 '{fact}' 与 '{ds}'（指标 '{name}'），跨事实表聚合被拒绝",
                    details={"fact": fact, "other_dataset": ds, "measure": name},
                )
            resolved.append((name, kind, spec))
        assert fact is not None

        # 解析维度：必须存在且可从事实表唯一到达
        dim_specs = []
        dim_paths: dict[str, list] = {}
        for d in query.dimensions:
            if not book.is_dimension(d):
                from .errors import CaliberNotFoundError

                raise CaliberNotFoundError(
                    f"维度口径 '{d}' 不存在", details={"caliber": d, "kind": "dimension"}
                )
            dspec = book.dimension(d)
            path = model.relations.resolve_path(fact, dspec.dataset)
            dim_specs.append(dspec)
            dim_paths[d] = path

        # 过滤器解析与值类型校验
        filters: list[tuple[object, tuple[object, ...], bool, list]] = []
        for f in query.filters:
            if not book.is_dimension(f.dimension):
                raise QueryError(
                    f"过滤维度 '{f.dimension}' 不存在",
                    details={"filter": f.dimension},
                )
            dspec = book.dimension(f.dimension)
            path = model.relations.resolve_path(fact, dspec.dataset)
            target_field = model.datasets[dspec.dataset].schema.field_map[dspec.field]
            coerced = []
            for raw in f.values:
                if raw is None:
                    coerced.append(None)
                    continue
                try:
                    coerced.append(coerce_value(raw, target_field.logical_type, field=dspec.field))
                except Exception as exc:  # TypeConflictError -> QueryError
                    raise QueryError(
                        f"过滤维度 '{f.dimension}' 的值 {raw!r} 与其类型 "
                        f"{target_field.logical_type.value} 不匹配: {exc}",
                        details={"filter": f.dimension, "value": repr(raw)},
                    ) from exc
            filters.append((dspec, tuple(coerced), f.negate, path))

        # 预建被查找侧（one 侧）唯一键索引
        indexes: dict[tuple[str, tuple[str, ...]], dict[tuple, dict]] = {}

        def index_for(ds_name: str, keys: tuple[str, ...]) -> dict[tuple, dict]:
            tok = (ds_name, keys)
            if tok not in indexes:
                ds = model.datasets[ds_name]
                idx: dict[tuple, dict] = {}
                for rec in ds.records:
                    idx[tuple(rec[k] for k in keys)] = rec
                indexes[tok] = idx
            return indexes[tok]

        fact_ds = model.datasets[fact]
        guard.add_bytes(len(fact_ds.records) * approx_row_bytes(len(fact_ds.schema.fields)))
        guard.check_time()

        # 预先收集本次查询要经过的所有路径（维度 + 过滤）
        all_paths: list[list] = list(dim_paths.values()) + [p for _, _, _, p in filters]
        unique_paths: list[list] = []
        seen_sig: set[tuple[str, ...]] = set()
        for p in all_paths:
            sig = tuple(r.name for r in p)
            if sig not in seen_sig:
                seen_sig.add(sig)
                unique_paths.append(p)

        def traverse(rec: dict) -> dict[str, object]:
            wide = {f"{fact}.{k}": v for k, v in rec.items()}
            for path in unique_paths:
                cur_ds = fact
                cur_row: dict | None = rec
                for rel in path:
                    nxt = rel.other(cur_ds)
                    if cur_row is None:
                        break
                    from_keys = rel.keys_of(cur_ds)
                    to_keys = rel.keys_of(nxt)
                    vals = tuple(cur_row.get(k) for k in from_keys)
                    if any(v is None for v in vals):
                        cur_row = None
                        break
                    idx = index_for(nxt, to_keys)
                    cur_row = idx.get(vals)
                    cur_ds = nxt
                if cur_row is not None:
                    for k, v in cur_row.items():
                        wide.setdefault(f"{cur_ds}.{k}", v)
            return wide

        intermediate = 0
        groups: dict[tuple[object, ...], _Accumulator] = {}

        def dim_value(wide: dict, dspec, path: list) -> object:
            return wide.get(f"{dspec.dataset}.{dspec.field}")

        for rec in fact_ds.records:
            wide = traverse(rec)
            # 过滤：固定的 in / not-in 语义（两者互为确定补集）
            # - membership 判定：actual in vals（Python 成员判定，
            #   因此空值行只有在 vals 显式包含 None 时才算成员）
            # - negate=True 取确定补集：in 不选空桶时，not in 选空桶；
            #   in 显式选空桶 (...,None) 时，not in 排除空桶
            passed = True
            for dspec, vals, negate, path in filters:
                actual = dim_value(wide, dspec, path)
                hit = actual in vals
                if negate:
                    hit = not hit
                if not hit:
                    passed = False
                    break
            if not passed:
                continue
            intermediate += 1
            if intermediate % 1024 == 0:
                guard.check_time()
            guard.add_bytes(approx_row_bytes(len(wide)))

            key = tuple(
                dim_value(wide, d, dim_paths[d.name]) for d in dim_specs
            )
            acc = groups.get(key)
            if acc is None:
                # 高基数预检：每次新增分组时校验（大批量时 guard 内部记录基数）
                guard.register_groups(len(groups) + 1)
                acc = _Accumulator()
                groups[key] = acc

            for col, kind, spec in resolved:
                if kind == "measure":
                    if spec.agg is AggKind.COUNT:
                        # count(*) 对每个通过过滤的行计数
                        acc.add_measure_value(col, AggKind.COUNT, None)
                    else:
                        acc.add_measure_value(col, spec.agg, rec.get(spec.field))
                else:
                    # ratio：对分子/分母两个基础 measure 在明细行上独立累加，
                    # 最终在分组聚合完成后才相除（不使用预聚合结果）
                    num_spec = book.measure(spec.numerator)
                    den_spec = book.measure(spec.denominator)
                    if num_spec.agg is AggKind.COUNT:
                        acc.add_measure_value(f"__num__{col}", AggKind.COUNT, None)
                    else:
                        acc.add_measure_value(
                            f"__num__{col}", num_spec.agg, rec.get(num_spec.field)
                        )
                    if den_spec.agg is AggKind.COUNT:
                        acc.add_measure_value(f"__den__{col}", AggKind.COUNT, None)
                    else:
                        acc.add_measure_value(
                            f"__den__{col}", den_spec.agg, rec.get(den_spec.field)
                        )

        guard.add_intermediate_rows(intermediate)
        guard.check_time()

        # 确定性排序：None 桶恒在最前
        def sort_key(k: tuple[object, ...]):
            return tuple((v is not None, _sort_scalar(v)) for v in k)

        ordered_keys = sorted(groups.keys(), key=sort_key)
        allowed = guard.allow_output_rows(len(ordered_keys))
        if allowed < len(ordered_keys):
            ordered_keys = ordered_keys[:allowed]

        columns = tuple([d.name for d in dim_specs] + [c for c, _, _ in resolved])
        rows: list[tuple[object, ...]] = []
        for key in ordered_keys:
            acc = groups[key]
            out: list[object] = list(key)
            for col, kind, spec in resolved:
                out.append(self._finalize(kind, spec, col, acc, book))
            rows.append(tuple(out))

        lineage = self._build_lineage(dim_specs, resolved, dim_paths, book)
        return QueryResult(
            columns=columns,
            rows=tuple(rows),
            lineage=lineage,
            model_version=model.version,
            truncated=guard.truncated,
            resource_stats=guard.stats(),
        )

    # ------------------------------------------------------------------
    def _finalize(self, kind: str, spec: object, col: str, acc: _Accumulator, book) -> object:
        if kind == "measure":
            agg: AggKind = spec.agg
            if agg is AggKind.COUNT:
                return acc.cnt.get(col, 0)
            if agg is AggKind.COUNT_DISTINCT:
                return len(acc.distinct.get(col, set()))
            if agg is AggKind.SUM:
                return acc.sum_v.get(col, 0)
            if agg is AggKind.AVG:
                n = acc.cnt.get(col, 0)
                if n == 0:
                    return None
                return _safe_div(acc.sum_v.get(col, 0), n)
            if agg is AggKind.MIN:
                return acc.min_v.get(col)
            if agg is AggKind.MAX:
                return acc.max_v.get(col)
            raise QueryError(f"不支持的聚合 {agg}", details={"agg": agg.value})
        # ratio
        num_spec = book.measure(spec.numerator)
        den_spec = book.measure(spec.denominator)
        nval = self._base_value(num_spec, f"__num__{col}", acc)
        dval = self._base_value(den_spec, f"__den__{col}", acc)
        if nval is None or dval is None:
            return None
        return _safe_div(nval, dval)

    @staticmethod
    def _base_value(mspec: MeasureSpec, slot: str, acc: _Accumulator):
        agg = mspec.agg
        if agg in (AggKind.COUNT, AggKind.COUNT_DISTINCT):
            return (acc.cnt.get(slot, 0) if agg is AggKind.COUNT
                    else len(acc.distinct.get(slot, set())))
        if agg is AggKind.SUM:
            return acc.sum_v.get(slot, 0)
        if agg is AggKind.MIN:
            return acc.min_v.get(slot)
        if agg is AggKind.MAX:
            return acc.max_v.get(slot)
        raise QueryError(
            f"比率的基础口径不允许使用 {agg.value}", details={"agg": agg.value}
        )

    # ------------------------------------------------------------------
    def _build_lineage(self, dim_specs, resolved, dim_paths, book) -> Lineage:
        cols: list[ColumnLineage] = []
        for d in dim_specs:
            cols.append(
                ColumnLineage(
                    output_column=d.name,
                    kind="dimension",
                    caliber_name=d.name,
                    sources=(f"{d.dataset}.{d.field}",),
                    transforms=("group_by",),
                    via_relations=tuple(r.name for r in dim_paths[d.name]),
                )
            )
        for col, kind, spec in resolved:
            if kind == "measure":
                kind_label = "avg" if spec.agg is AggKind.AVG else "measure"
                transforms = (
                    ("avg = sum(detail)/count(detail)",)
                    if spec.agg is AggKind.AVG
                    else (spec.agg.value,)
                )
                cols.append(
                    ColumnLineage(
                        output_column=col,
                        kind=kind_label,
                        caliber_name=col,
                        sources=spec.sources(),
                        transforms=transforms
                        + (("count_rows" if spec.field is None else "field_value"),),
                    )
                )
            else:
                num = book.measure(spec.numerator)
                den = book.measure(spec.denominator)
                cols.append(
                    ColumnLineage(
                        output_column=col,
                        kind="ratio",
                        caliber_name=col,
                        sources=tuple(
                            s for m in (num, den) for s in m.sources()
                        ),
                        transforms=(
                            f"ratio({spec.numerator}/{spec.denominator}) computed on detail rows",
                        ),
                    )
                )
        return Lineage(columns=tuple(cols))


def _sort_scalar(v: object) -> object:
    """排序辅助：数值统一到 Decimal 比较；bool/str 原样。"""
    if isinstance(v, bool):
        return (0, int(v))
    if isinstance(v, int):
        return (1, Decimal(v))
    if isinstance(v, Decimal):
        return (1, v)
    return (2, str(v))
