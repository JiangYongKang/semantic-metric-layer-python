"""结构变更与内容更新：新增字段缺省语义稳定、删列/改名显式报错、
口径或数据更新后历史查询可区分且可复现。"""
import pytest

from sml.engine import Query, QueryEngine
from sml.errors import TypeConflictError, UnknownFieldError
from sml.model import Agg, Metric, SemanticModel
from tests.conftest import log_case


@pytest.fixture()
def env(registry):
    registry.register("t", [{"id": 1, "v": 10}, {"id": 2, "v": 20}])
    m = SemanticModel(registry)
    m.add_metric(Metric("s", "t", "v", Agg.SUM))
    m.add_metric(Metric("c", "t", None, Agg.COUNT))
    return registry, m, QueryEngine(registry, m)


class TestSchemaEvolution:
    def test_add_field_default_semantics_stable(self, env):
        registry, m, eng = env
        before = eng.run(Query(metrics=("s",)))
        registry.update("t", [{"id": 1, "v": 10, "note": "x"},
                              {"id": 2, "v": 20, "note": None}])
        after = eng.run(Query(metrics=("s",)))
        log_case("新增字段", "update 增加 note 列",
                 "既有指标结果不变；note 缺省为 None", (before.rows, after.rows))
        assert before.rows == after.rows
        # 新字段可用于查询；历史版本快照中不存在该字段
        m.add_metric(Metric("cn", "t", "note", Agg.COUNT_DISTINCT))
        r = eng.run(Query(metrics=("cn",)))
        assert r.rows[0]["cn"] == 1  # None 不计入 distinct

    def test_drop_column_fails_loudly(self, env):
        registry, m, eng = env
        registry.update("t", [{"id": 1}, {"id": 2}])  # 删掉 v 列
        with pytest.raises(UnknownFieldError) as ei:
            eng.run(Query(metrics=("s",)))
        log_case("删列", "update 删除 v 列后查询 sum(t.v)",
                 "必须显式报错而非静默错列", ei.value.code)
        assert "v" in ei.value.message

    def test_rename_column_fails_loudly(self, env):
        registry, m, eng = env
        registry.update("t", [{"id": 1, "v2": 10}, {"id": 2, "v2": 20}])
        with pytest.raises(UnknownFieldError) as ei:
            eng.run(Query(metrics=("s",)))
        log_case("改名", "v 改名为 v2", "旧列名查询显式失败", ei.value.code)

    def test_type_change_rejected(self, env):
        registry, m, eng = env
        with pytest.raises(TypeConflictError) as ei:
            registry.update("t", [{"id": 1, "v": "x"}])
        log_case("类型变更", "v: int -> string", "拒绝更新，旧版本不受影响",
                 ei.value.code)
        # 旧数据仍可查
        assert eng.run(Query(metrics=("s",))).rows[0]["s"] == 30

    def test_failed_update_does_not_pollute(self, env):
        registry, m, eng = env
        try:
            registry.update("t", [{"id": 1, "v": 10, "bad": None}])  # 全空列
        except Exception as e:
            log_case("失败更新", "新增全空列", "推断失败，版本不前进", e)
        assert registry.current_version("t") == 1


class TestVersioningAndReproducibility:
    def test_results_distinguishable_after_update(self, env):
        registry, m, eng = env
        r1 = eng.run(Query(metrics=("s", "c")))
        registry.update("t", [{"id": 1, "v": 10}, {"id": 2, "v": 20},
                              {"id": 3, "v": 30}])
        r2 = eng.run(Query(metrics=("s", "c")))
        log_case("数据更新可区分", "追加一行后同一查询",
                 "dataset_versions 与结果均不同",
                 (r1.dataset_versions, r2.dataset_versions))
        assert r1.dataset_versions != r2.dataset_versions
        assert r1.rows != r2.rows

    def test_historical_query_reproducible_via_pin(self, env):
        registry, m, eng = env
        r1 = eng.run(Query(metrics=("s",)))
        registry.update("t", [{"id": 1, "v": 999}])
        r2 = eng.run(Query(metrics=("s",), pin_versions=(("t", 1),)))
        log_case("历史复现", "pin_versions={'t': 1}",
                 "结果与更新前完全一致", (r1.rows, r2.rows))
        assert r2.rows == r1.rows
        assert r2.dataset_versions == {"t": 1}

    def test_model_change_distinguishable(self, env):
        registry, m, eng = env
        r1 = eng.run(Query(metrics=("s",)))
        m.add_metric(Metric("s2", "t", "v", Agg.SUM))
        r2 = eng.run(Query(metrics=("s",)))
        log_case("口径变更可区分", "新增指标后 model_version 变化",
                 "结果携带的 model_version 不同",
                 (r1.model_version, r2.model_version))
        assert r2.model_version > r1.model_version
