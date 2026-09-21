"""语义模型：某一时刻数据集 + 口径册 + 关联图的不可变快照。

查询引擎只持有快照，因此注册中心后续发生的任何更新都不会影响
进行中的查询（快照隔离，杜绝观察到半更新状态）。
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

from .caliber import CaliberBook
from .dataset import Dataset, DatasetRegistry
from .relations import RelationGraph


@dataclass(frozen=True)
class SemanticModel:
    """完整语义模型快照。"""

    version: int
    datasets: MappingProxyType[str, Dataset]
    calibers: CaliberBook
    relations: RelationGraph


def build_model(
    version: int,
    datasets: DatasetRegistry,
    calibers: CaliberBook,
    relations: RelationGraph,
) -> SemanticModel:
    """以当前注册中心内容构造不可变快照。

    顺序固定：先冻结数据集，再让口径册/关联图指向同一个快照注册中心，
    保证三者互相一致；所有登记项在快照上重新校验（快照与源同构，
    校验必通过），从而不共享任何可变容器。
    """
    frozen = dict(datasets._datasets)  # Dataset 本身不可变
    snap_registry = DatasetRegistry()
    snap_registry._datasets = frozen

    book = CaliberBook(snap_registry)
    for d in calibers._dimensions.values():
        book.add_dimension(d)
    for m in calibers._measures.values():
        book.add_measure(m)
    for r in calibers._ratios.values():
        book.add_ratio(r)

    graph = RelationGraph(snap_registry)
    for rel in relations._relations.values():
        graph.add(rel)

    return SemanticModel(
        version=version,
        datasets=MappingProxyType(frozen),
        calibers=book,
        relations=graph,
    )
