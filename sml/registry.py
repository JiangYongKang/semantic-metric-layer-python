"""线程安全的语义指标层门面。

并发确定性保证：

- 一把可重入锁保护所有注册/更新/查询操作；模型以不可变快照发布，
  查询全程只持快照，因此更新提交前后的查询看到的都是完整模型，
  任何查询不可能观察到半更新状态。
- 模型版本单调递增；任何注册/更新成功都会升版并**整体失效查询缓存**，
  保证缓存永远对应当前数据与口径；失败的注册/更新不升版、不清缓存。
- 查询失败（资源超限/查询非法）不写入缓存、不修改任何已发布状态，
  不影响后续查询。

历史版本与安全发布：

- 每次成功变更后，被取代的旧模型快照进入 :class:`VersionArchive`；
  ``query(..., at_version=v)`` 在**当时的**快照上重放，支持与当前完全
  相同的维度/指标/过滤/资源预算语义，结果与当时当次一致；历史查询
  不读写当前版本缓存，因而不会改变当前版本的缓存命中与查询结果。
- 数据更新（``replace_dataset_data``）采用「候选副本全量预检 → 原子提交」：
  在与当前同构的候选注册中心上执行替换并整体重建模型，任何关联键不再
  唯一、结构不兼容或口径/查询路径失效都会在提交前暴露；预检失败时
  当前版本号、当前模型、缓存以及后续登记行为与更新前完全一致。
"""

from __future__ import annotations

import threading
from dataclasses import replace

from .caliber import CaliberBook, DimensionSpec, MeasureSpec, RatioSpec
from .dataset import DatasetRegistry
from .engine import Query, QueryEngine, QueryResult
from .errors import (
    VersionEvictedError,
    VersionNotFoundError,
    VersionOutOfRangeError,
)
from .history import VersionArchive
from .limits import ResourceBudget
from .model import SemanticModel, build_model
from .relations import Relation, RelationGraph


class MetricLayer:
    def __init__(self, *, history_retention: int | None = None) -> None:
        self._lock = threading.RLock()
        self._datasets = DatasetRegistry()
        self._calibers = CaliberBook(self._datasets)
        self._relations = RelationGraph(self._datasets)
        self._model_version = 0
        self._model: SemanticModel | None = None
        self._archive = VersionArchive(retention=history_retention)
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
            # 安全发布：先在与当前同构的「候选副本」上执行替换并整体重建
            # 模型（关联键唯一、结构兼容、口径与查询路径全部复验）。
            # 任一步抛错都发生在副本上，当前注册中心/版本/缓存均不触碰。
            candidate_model = self._build_candidate_after_data_replace(name, records)
            # 预检全部通过：对当前注册中心执行同一变更（与副本同输入，
            # 且副本已验证通过，故必然成功），再提交已验证的候选模型。
            self._datasets.replace_data(name, records)
            self._commit(candidate_model)
            return candidate_model.datasets[name]

    def _build_candidate_after_data_replace(
        self, name: str, records: list[dict]
    ) -> SemanticModel:
        """在隔离副本上执行数据替换并整体构建候选模型。

        副本是当前已发布模型的同构再构建，因此已有的关联唯一性、口径
        有效性都会在新数据上重新检查；任何拒绝都在此处、提交之前发生。
        """
        current = self._current_model_locked()
        frozen = dict(current.datasets)  # Dataset 不可变
        candidate_registry = DatasetRegistry()
        candidate_registry._datasets = frozen
        candidate_registry.replace_data(name, records)  # 结构/主键复验
        candidate_book = CaliberBook(candidate_registry)
        for d in self._calibers._dimensions.values():
            candidate_book.add_dimension(d)
        for m in self._calibers._measures.values():
            candidate_book.add_measure(m)
        for r in self._calibers._ratios.values():
            candidate_book.add_ratio(r)
        candidate_graph = RelationGraph(candidate_registry)
        for rel in self._relations._relations.values():
            # 关联键在新数据上的唯一/非空、列存在性在此复验
            candidate_graph.add(rel)
        return build_model(
            self._model_version + 1,
            candidate_registry,
            candidate_book,
            candidate_graph,
        )

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
        at_version: int | None = None,
    ) -> QueryResult:
        budget = budget or ResourceBudget()
        with self._lock:
            if at_version is not None:
                # 历史版本回查：在当时快照上执行完全相同的引擎语义；
                # 不读、不写当前版本缓存，不影响当前版本的缓存命中与结果，
                # 也不改变任何已发布状态。
                model = self._resolve_version_locked(at_version)
                result = QueryEngine(model, budget).run(query)
                stats = dict(result.resource_stats or {})
                stats["cache_hit"] = False
                stats["historical"] = True
                return replace(result, resource_stats=stats)

            cache_key = self._cache_key(query, budget)
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
    # 历史版本回查与保留策略
    # ------------------------------------------------------------------
    def set_history_retention(self, retention: int | None) -> list[int]:
        """设置除当前版本外最多保留的历史版本数；立即按 FIFO 淘汰。

        ``None`` 不限；``0`` 不保留任何历史版本。返回被淘汰的版本号列表。
        只影响历史档案，不改变当前版本号、当前模型与缓存。
        """
        with self._lock:
            return self._archive.set_retention(retention)

    def history_retention(self) -> int | None:
        with self._lock:
            return self._archive.retention

    def retained_versions(self) -> list[int]:
        """当前可回查的历史版本号（升序，不含当前版本）。"""
        with self._lock:
            return self._archive.retained_versions()

    def oldest_retained_version(self) -> int | None:
        """当前可回查的最旧历史版本号；无历史版本时为 None。"""
        with self._lock:
            return self._archive.oldest_version()

    def snapshot_at_version(self, version: int) -> SemanticModel:
        """显式取某一历史版本的模型快照（版本判定同历史查询）。"""
        with self._lock:
            return self._resolve_version_locked(version)

    def _resolve_version_locked(self, version: int) -> SemanticModel:
        """把版本号解析为完整模型快照，四种不存在情形严格区分。

        - 当前版本：当前模型；
        - 从未存在（非正整数或大于当前版本）：``version_not_found``；
        - 曾归档后被淘汰：``version_evicted``；
        - 从未进入保留范围（低于最早归档点 / retention=0）：
          ``version_out_of_range``。
        """
        current = self._current_model_locked()
        if not isinstance(version, int) or isinstance(version, bool) or version <= 0:
            raise VersionNotFoundError(
                f"版本号必须是正整数，收到 {version!r}（该版本从未存在）",
                details={"requested": version, "current_version": current.version,
                         "reason": "invalid_version"},
            )
        if version == current.version:
            return current
        if version > current.version:
            raise VersionNotFoundError(
                f"版本 {version} 从未存在（当前最新版本为 {current.version}），"
                "禁止用当前版本回退冒充",
                details={"requested": version, "current_version": current.version,
                         "reason": "future_version"},
            )
        snapshot = self._archive.get(version)
        if snapshot is not None:
            return snapshot
        oldest = self._archive.oldest_version()
        common = {
            "requested": version,
            "current_version": current.version,
            "oldest_available": oldest,
            "retention": self._archive.retention,
        }
        ever_min = self._archive.ever_archived_min()
        if ever_min is not None and version >= ever_min:
            # 曾进入档案、后因保留上限被淘汰
            raise VersionEvictedError(
                f"版本 {version} 曾经存在，但因超过历史保留上限"
                f"（{self._archive.retention} 个历史版本）已被淘汰；"
                f"当前可回查的最旧版本为 {oldest}",
                details={**common, "reason": "evicted_by_retention"},
            )
        # 从未进入档案（如保留上限为 0，或早于最早归档点）
        raise VersionOutOfRangeError(
            f"版本 {version} 不在可回查范围内（历史保留上限="
            f"{self._archive.retention}，当前最旧可回查版本为 {oldest}）",
            details={**common, "reason": "out_of_retention_range"},
        )

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
        """普通成功变更：构建下一版快照并提交。"""
        new_model = build_model(
            self._model_version + 1, self._datasets, self._calibers, self._relations
        )
        self._commit(new_model)

    def _commit(self, new_model: SemanticModel) -> None:
        """成功变更后的唯一提交点（调用方须持锁，且模型已构建成功）：

        1. 把被取代的旧快照归档进历史档案（可能触发最旧版本淘汰）；
        2. 再替换当前版本号与模型指针、清空当前版本缓存。

        归档/赋值/清缓存之间无任何可抛错的外部操作，故提交原子。
        """
        old_model = self._model
        if old_model is not None:
            self._archive.publish(old_model)
        self._model_version = new_model.version
        self._model = new_model
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
