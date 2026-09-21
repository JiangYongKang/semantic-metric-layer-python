"""测试公共设施：日志打印输入与判定依据。"""
from __future__ import annotations

import logging
import sys

import pytest

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
    force=True,
)
log = logging.getLogger("sml.test")


def log_case(title: str, inputs, basis: str, outcome=None) -> None:
    """打印：用例标题、输入、判定依据、结果。"""
    log.info("CASE %s | 输入=%s | 判定依据=%s | 结果=%s",
             title, inputs, basis, outcome)


@pytest.fixture()
def registry():
    from sml.dataset import DatasetRegistry
    return DatasetRegistry()


@pytest.fixture()
def sales_registry(registry):
    registry.register("orders", [
        {"oid": 1, "cid": 10, "amt": 0.1, "qty": 3},
        {"oid": 2, "cid": 10, "amt": 0.2, "qty": 1},
        {"oid": 3, "cid": 20, "amt": 0.3, "qty": None},
        {"oid": 4, "cid": 99, "amt": 1.0, "qty": 2},
    ])
    registry.register("customers", [
        {"cid": 10, "city": "hz"},
        {"cid": 20, "city": "sh"},
    ])
    return registry


@pytest.fixture()
def model(sales_registry):
    from sml.model import Agg, Join, Metric, RatioMetric, SemanticModel
    m = SemanticModel(sales_registry)
    m.add_join(Join("o2c", "orders", "cid", "customers", "cid"))
    m.add_metric(Metric("total_amt", "orders", "amt", Agg.SUM))
    m.add_metric(Metric("avg_qty", "orders", "qty", Agg.AVG))
    m.add_metric(Metric("n_orders", "orders", None, Agg.COUNT))
    m.add_ratio_metric(RatioMetric("amt_per_order", "total_amt", "n_orders"))
    return m
