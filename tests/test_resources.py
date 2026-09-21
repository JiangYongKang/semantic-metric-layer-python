"""资源上限与超限处置：超限即拒绝、不残留部分结果、不影响后续查询。"""
import pytest

from sml.engine import Query, QueryEngine
from sml.errors import ResourceLimitError
from sml.model import Agg, Metric, SemanticModel
from sml.resources import ResourceLimits
from tests.conftest import log_case


@pytest.fixture()
def env(registry):
    registry.register("t", [{"g": i % 7, "v": i} for i in range(100)])
    m = SemanticModel(registry)
    m.add_metric(Metric("s", "t", "v", Agg.SUM))
    m.add_metric(Metric("c", "t", None, Agg.COUNT))
    return registry, m


class TestLimits:
    def test_scan_rows_limit(self, env):
        registry, m = env
        eng = QueryEngine(registry, m, ResourceLimits(max_scan_rows=50))
        with pytest.raises(ResourceLimitError) as ei:
            eng.run(Query(metrics=("s",)))
        log_case("扫描行数超限", "100 行，预算 50", "charge 时即拒绝",
                 (ei.value.limit_kind, ei.value.budget, ei.value.observed))
        assert ei.value.limit_kind == "scan_rows"

    def test_elapsed_limit(self, env):
        registry, m = env
        eng = QueryEngine(registry, m, ResourceLimits(max_elapsed_ms=0))
        with pytest.raises(ResourceLimitError) as ei:
            eng.run(Query(metrics=("s",)))
        log_case("耗时超限", "预算 0ms", "任何执行都超时", ei.value.limit_kind)
        assert ei.value.limit_kind == "elapsed_ms"

    def test_output_limit(self, env):
        registry, m = env
        eng = QueryEngine(registry, m, ResourceLimits(max_output_rows=3))
        with pytest.raises(ResourceLimitError) as ei:
            eng.run(Query(metrics=("c",), dimensions=("t.g",)))
        log_case("输出行数超限", "7 个分组，预算 3", "拒绝", ei.value.limit_kind)
        assert ei.value.limit_kind == "output_rows"

    def test_failure_leaves_no_partial_state(self, env):
        """超限失败后：缓存无残留、后续正常查询不受影响、结果仍正确。"""
        registry, m = env
        eng = QueryEngine(registry, m, ResourceLimits(max_scan_rows=50))
        with pytest.raises(ResourceLimitError):
            eng.run(Query(metrics=("s",)))
        assert len(eng._cache) == 0
        log_case("失败无残留", "超限查询后检查缓存", "缓存为空", len(eng._cache))
        # 放宽预算后同一引擎可正常服务
        eng._limits = ResourceLimits()
        r = eng.run(Query(metrics=("s",)))
        log_case("后续查询", "放宽预算重试", "结果正确", r.rows)
        assert r.rows[0]["s"] == sum(range(100))

    def test_failed_query_not_cached(self, env):
        registry, m = env
        eng = QueryEngine(registry, m, ResourceLimits(max_scan_rows=50))
        for _ in range(2):
            with pytest.raises(ResourceLimitError):
                eng.run(Query(metrics=("s",)))
        eng._limits = ResourceLimits()
        r = eng.run(Query(metrics=("s",)))
        log_case("失败不缓存", "两次超限后重试", "得到正确结果而非缓存的失败",
                 r.rows[0]["s"])
        assert r.rows[0]["s"] == sum(range(100))
