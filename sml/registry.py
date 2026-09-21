"""线程安全的语义指标层门面。

并发确定性保证：

- 一把可重入锁保护所有注册/更新/查询操作；模型以不可变快照发布，
  查询全程只持快照，因此更新提交前后的查询看到的都是完整模型，
  任何查询不可能观察到半更新状态。
- 模型版本单调递增；任何注册/更新成功都会升版并**整体失效查询缓存**，
  保证缓存永远对应当前数据与口径；失败的注册/更新不升版、不清缓存。
- 查询失败（资源超限/查询非法）不写入缓存、不修改任何已发布状态，
  不影响后续查询。
"""

from __future__ import annotations

import threading
from dataclasses import replace

from .caliber import CaliberBook, DimensionSpec, MeasureSpec, RatioSpec
from .dataset import DatasetRegistry
from .engine import Query, QueryEngine, QueryResult
from .limits import ResourceBudget
from .model import SemanticModel, build_model
from .relations import Relation, RelationGraph


class MetricLayer:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._datasets = DatasetRegistry()
        self._calibers = CaliberBook(self._datasets)
        self._relations = RelationGraph(self._datasets)
        self._model_version = 0
        self._model: SemanticModel | None = None
        self._cache: dict[tuple, QueryResult] = {}
        self._stats_lock = threading.Lock()
        self._query_seq = 0

    # ------------------------------------------------------------------
    # 数据集
    # ------------------------------------------------------------------
    def register_dataset(
        self, name: str, records: list[dict], *, primary_key: tuple[str, ...] = ()
    ):
        with self._lock:
            ds = self._datasets.register(name, records, primary_key=primary_key)
            self._bump()
            return ds

    def replace_dataset_data(self, name: str, records: list[dict]):
        with self._lock:
            # 先执行（可能抛错）；成功才升版，失败保持旧状态
            ds = self._datasets.replace_data(name, records)
            self._bump()
            return ds

    def dataset_version(self, name: str) -> int:
        with self._lock:
            return self._datasets.get(name).version

    # ------------------------------------------------------------------
    # 口径 / 关联（每次成功登记都使模型升版）
    # ------------------------------------------------------------------
    def add_dimension(self, spec: DimensionSpec) -> None:
        with self._lock:
            self._calibers.add_dimension(spec)
            self._bump()

    def add_measure(self, spec: MeasureSpec) -> None:
        with self._lock:
            self._calibers.add_measure(spec)
            self._bump()

    def add_ratio(self, spec: RatioSpec) -> None:
        with self._lock:
            self._calibers.add_ratio(spec)
            self._bump()

    def add_relation(self, relation: Relation) -> None:
        with self._lock:
            self._relations.add(relation)
            self._bump()

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
    # 查询
    # ------------------------------------------------------------------
    def query(
        self,
        query: Query,
        *,
        budget: ResourceBudget | None = None,
        use_cache: bool = True,
    ) -> QueryResult:
        budget = budget or ResourceBudget()
        cache_key = self._cache_key(query, budget)
        with self._lock:
            if use_cache and cache_key in self._cache:
                cached = self._cache[cache_key]
                # 缓存条目在写入时已与当时模型版本绑定；缓存随模型失效，
                # 这里再防御性校验一次版本一致
                if cached.model_version == self._model_version:
                    return replace(cached, resource_stats=dict(cached.resource_stats or {},
                                                               cache_hit=True))
            model = self._current_model_locked()
            result = QueryEngine(model, budget).run(query)
            # 仅成功查询写缓存
            if use_cache:
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
    def snapshot_model(self) -> SemanticModel:
        """显式取一份当前模型快照（供外部读取/离线使用）。"""
        with self._lock:
            return self._current_model_locked()

    def _current_model_locked(self) -> SemanticModel:
        if self._model is None:
            self._model_version += 1
            self._model = build_model(
                self._model_version, self._datasets, self._calibers, self._relations
            )
        return self._model

    def _bump(self) -> None:
        self._model_version += 1
        self._model = build_model(
            self._model_version, self._datasets, self._calibers, self._relations
        )
        self._cache.clear()

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
