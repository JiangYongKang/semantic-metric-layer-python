"""线程安全的语义指标层门面：原子发布 + 历史版本回查。

并发确定性保证：

- 一把可重入锁保护所有注册/更新/查询操作；模型以不可变快照发布，
  查询全程只持快照，因此更新提交前后的查询看到的都是完整模型，
  任何查询不可能观察到半更新状态。
- 每次变更都走「克隆候选态 -> 应用变更 -> 在候选态上整体重建模型并
  全量校验 -> 全部通过才原子替换当前指针」。任何一步抛错，当前版本号、
  当前模型、查询缓存以及后续登记能力都与更新前完全一致，杜绝
  「版本前进/数据/缓存」三者错档的混合状态。
- 模型版本单调递增；只有成功发布才升版，并把**被取代的旧模型**作为
  不可变快照压入历史栈、同时整体失效当前查询缓存；失败不升版、
  不入历史、不清缓存。
- 查询可显式指定 ``at_version`` 回查历史：在当时的完整快照（数据/口径/
  关联/列级血缘）上以同一套查询与预算语义执行，绝不以当前版本顶替；
  历史回查不读写当前缓存，因此不影响当前版本的缓存命中与查询结果。
"""

from __future__ import annotations

import threading
from dataclasses import replace
from typing import Callable, TypeVar

from .caliber import CaliberBook, DimensionSpec, MeasureSpec, RatioSpec
from .dataset import Dataset, DatasetRegistry
from .engine import Query, QueryEngine, QueryResult
from .errors import (
    VersionEvictedError,
    VersionNeverExistedError,
    VersionOutOfRangeError,
)
from .history import VersionHistory
from .limits import ResourceBudget
from .model import SemanticModel, build_model
from .relations import Relation, RelationGraph

T = TypeVar("T")


class MetricLayer:
    def __init__(self, *, max_history: int = 10) -> None:
        self._lock = threading.RLock()
        self._datasets = DatasetRegistry()
        self._calibers = CaliberBook(self._datasets)
        self._relations = RelationGraph(self._datasets)
        self._model_version = 0
        self._model: SemanticModel | None = None
        self._history = VersionHistory(max_history)
        self._cache: dict[tuple, QueryResult] = {}
        self._stats_lock = threading.Lock()
        self._query_seq = 0

    # ------------------------------------------------------------------
    # 数据集
    # ------------------------------------------------------------------
    def register_dataset(
        self, name: str, records: list[dict], *, primary_key: tuple[str, ...] = ()
    ) -> Dataset:
        with self._lock:
            def apply(reg: DatasetRegistry, _b: CaliberBook, _g: RelationGraph) -> Dataset:
                return reg.register(name, records, primary_key=primary_key)

            return self._commit(apply)

    def replace_dataset_data(self, name: str, records: list[dict]) -> Dataset:
        with self._lock:
            def apply(reg: DatasetRegistry, _b: CaliberBook, _g: RelationGraph) -> Dataset:
                return reg.replace_data(name, records)

            return self._commit(apply)

    def dataset_version(self, name: str) -> int:
        with self._lock:
            return self._datasets.get(name).version

    # ------------------------------------------------------------------
    # 口径 / 关联（每次成功登记都发布一个新版本）
    # ------------------------------------------------------------------
    def add_dimension(self, spec: DimensionSpec) -> None:
        with self._lock:
            self._commit(lambda _r, b, _g: b.add_dimension(spec))

    def add_measure(self, spec: MeasureSpec) -> None:
        with self._lock:
            self._commit(lambda _r, b, _g: b.add_measure(spec))

    def add_ratio(self, spec: RatioSpec) -> None:
        with self._lock:
            self._commit(lambda _r, b, _g: b.add_ratio(spec))

    def add_relation(self, relation: Relation) -> None:
        with self._lock:
            self._commit(lambda _r, _b, g: g.add(relation))

    def model_version(self) -> int:
        with self._lock:
            return self._model_version

    def list_datasets(self) -> list[str]:
        with self._lock:
            return self._datasets.names()

    def list_relations(self) -> list[str]:
        with self._lock:
            return self._relations.names()

    def list_metrics(self) -> list[str]:
        with self._lock:
            return self._calibers.metric_names()

    # ------------------------------------------------------------------
    # 历史版本保留与容量边界
    # ------------------------------------------------------------------
    def history_limit(self) -> int:
        with self._lock:
            return self._history.limit

    def set_history_limit(self, max_history: int) -> tuple[int, ...]:
        """调整最多保留的历史版本数；立即按 FIFO 淘汰并返回被淘汰版本号。"""
        with self._lock:
            return self._history.set_limit(max_history)

    def history_versions(self) -> tuple[int, ...]:
        """仍可回查的历史版本号（升序，不含当前版本）。"""
        with self._lock:
            return self._history.retained_versions

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def query(
        self,
        query: Query,
        *,
        budget: ResourceBudget | None = None,
        use_cache: bool = True,
        at_version: int | None = None,
    ) -> QueryResult:
        budget = budget or ResourceBudget()
        cache_key = self._cache_key(query, budget)
        with self._lock:
            model = self._resolve_model_locked(at_version)
            # 显式指定「当前版本」与不指定等价：都是当前查询，走当前缓存；
            # 只有回查早于当前的历史版本才算 historical，完全不碰当前缓存
            historical = at_version is not None and model.version != self._model_version
            if use_cache and not historical and cache_key in self._cache:
                cached = self._cache[cache_key]
                # 缓存条目在写入时已与当时模型版本绑定；缓存随发布失效，
                # 这里再防御性校验一次版本一致
                if cached.model_version == self._model_version:
                    return replace(cached, resource_stats=dict(cached.resource_stats or {},
                                                               cache_hit=True))
            # 历史回查在历史快照上以完全相同的引擎/预算语义执行
            result = QueryEngine(model, budget).run(query)
            # 仅当前版本的成功查询写当前缓存；历史回查绝不触碰当前缓存
            if use_cache and not historical:
                self._cache[cache_key] = result
            with self._stats_lock:
                self._query_seq += 1
            return result

    def cache_size(self) -> int:
        with self._lock:
            return len(self._cache)

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()

    # ------------------------------------------------------------------
    def snapshot_model(self, *, at_version: int | None = None) -> SemanticModel:
        """取一份模型快照；缺省为当前版本，``at_version`` 回查历史版本。"""
        with self._lock:
            return self._resolve_model_locked(at_version)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _current_model_locked(self) -> SemanticModel:
        if self._model is None:
            self._model_version += 1
            self._model = build_model(
                self._model_version, self._datasets, self._calibers, self._relations
            )
        return self._model

    def _clone_state_locked(self) -> tuple[DatasetRegistry, CaliberBook, RelationGraph]:
        """克隆一份与在线态不共享任何可变容器的候选态（Dataset 本身不可变）。"""
        reg = DatasetRegistry()
        reg._datasets = dict(self._datasets._datasets)
        book = CaliberBook(reg)
        book._dimensions = dict(self._calibers._dimensions)
        book._measures = dict(self._calibers._measures)
        book._ratios = dict(self._calibers._ratios)
        graph = RelationGraph(reg)
        graph._relations = dict(self._relations._relations)
        return reg, book, graph

    def _commit(self, apply: Callable[[DatasetRegistry, CaliberBook, RelationGraph], T]) -> T:
        """原子发布一次变更（调用方已持锁）。

        顺序固定：克隆 -> 在候选态上应用变更（可能抛错）-> 以候选态整体
        重建模型，重建会对所有数据集/口径/关联重新校验（含 one 侧键唯一、
        结构与口径引用）-> 全部成功后才替换在线指针、入历史、清缓存。
        任一步抛错时在线态原封不动，失败的候选态直接丢弃。
        """
        reg, book, graph = self._clone_state_locked()
        payload = apply(reg, book, graph)
        new_version = self._model_version + 1
        # build_model 在候选态上整体重建并全量校验；不通过即整体拒绝
        candidate_model = build_model(new_version, reg, book, graph)
        old_model = self._model
        # 提交点：仅替换不可变指针与字典引用（持锁，原子可见）
        self._datasets = reg
        self._calibers = book
        self._relations = graph
        self._model_version = new_version
        self._model = candidate_model
        if old_model is not None:
            self._history.record(old_model)
        self._cache.clear()
        return payload

    def _resolve_model_locked(self, at_version: int | None) -> SemanticModel:
        current = self._current_model_locked()
        if at_version is None:
            return current
        if not isinstance(at_version, int) or isinstance(at_version, bool) or at_version < 1:
            raise VersionOutOfRangeError(
                f"版本号必须是 >=1 的整数，收到 {at_version!r}",
                details={"requested": at_version, "current_version": current.version},
            )
        if at_version == current.version:
            return current
        if at_version > current.version:
            raise VersionNeverExistedError(
                f"版本 {at_version} 从未发布过（当前最新版本为 {current.version}），"
                "不会用当前版本顶替",
                details={"requested": at_version, "current_version": current.version},
            )
        model = self._history.get(at_version)
        if model is None:
            raise VersionEvictedError(
                f"版本 {at_version} 曾发布但已按保留上限淘汰"
                f"（当前保留 {self._history.limit} 个历史版本），不会用当前版本顶替",
                details={
                    "requested": at_version,
                    "current_version": current.version,
                    "retained_versions": list(self._history.retained_versions),
                    "max_history": self._history.limit,
                },
            )
        return model

    @staticmethod
    def _cache_key(query: Query, budget: ResourceBudget) -> tuple:
        return (
            query.dimensions,
            query.measures,
            tuple((f.dimension, f.values, f.negate) for f in query.filters),
            (
                budget.max_output_rows,
                budget.max_intermediate_rows,
                budget.max_groups,
                budget.timeout_seconds,
                budget.max_bytes,
                budget.on_overflow.value,
            ),
        )
