"""数据集注册与结构推断：异常数据必须被分类拒绝，且失败不污染注册表。"""
import pytest

from sml.errors import (IncompleteStructureError, MissingFieldError,
                        SchemaError, TypeConflictError)
from tests.conftest import log_case


class TestInferenceRejection:
    @pytest.mark.parametrize("rows,exc,why", [
        ([], IncompleteStructureError, "空数据集无法推断结构"),
        ([{"a": 1}, "not-a-dict"], IncompleteStructureError, "行不是映射"),
        ([{"a": 1}, {"b": 2}], MissingFieldError, "第 2 行缺少字段 a"),
        ([{"a": 1}, {"a": 2, "c": 3}], IncompleteStructureError, "第 2 行多出未声明字段"),
        ([{"a": 1}, {"a": "x"}], TypeConflictError, "字段 a 类型 int/string 冲突"),
        ([{"a": 1}, {"a": True}], TypeConflictError, "bool 与 int 是不同类型"),
        ([{"a": None}, {"a": None}], IncompleteStructureError, "全空字段无法推断类型"),
    ])
    def test_reject_with_distinct_reasons(self, registry, rows, exc, why):
        with pytest.raises(exc) as ei:
            registry.register("bad", rows)
        log_case("注册拒绝", rows, why, f"{ei.value.code}: {ei.value.message}")
        # 失败不污染状态：数据集不应存在
        with pytest.raises(SchemaError):
            registry.get("bad")

    def test_error_codes_are_distinguishable(self, registry):
        codes = set()
        for rows in ([], [{"a": 1}, {"b": 2}], [{"a": 1}, {"a": "x"}]):
            with pytest.raises(SchemaError) as ei:
                registry.register("x", rows)
            codes.add(ei.value.code)
        log_case("错误码可区分", "三类异常输入", "code 属性互不相同", sorted(codes))
        assert codes == {"INCOMPLETE_STRUCTURE", "MISSING_FIELD", "TYPE_CONFLICT"}


class TestRegistrationSuccess:
    def test_null_values_allowed_when_type_inferable(self, registry):
        v = registry.register("t", [{"a": 1, "b": None}, {"a": 2, "b": "x"}])
        log_case("可推断含空值", "b: [None, 'x']", "None 不参与推断，b=string",
                 [f.name + ":" + f.dtype.value for f in v.schema.fields])
        assert v.schema.field("b").dtype.value == "string"

    def test_duplicate_register_rejected(self, registry):
        registry.register("t", [{"a": 1}])
        with pytest.raises(SchemaError) as ei:
            registry.register("t", [{"a": 2}])
        log_case("重复注册", "register('t') x2", "已存在须走 update", ei.value.message)

    def test_float_normalized_to_decimal(self, registry):
        from decimal import Decimal
        v = registry.register("t", [{"a": 0.1}])
        log_case("float 规范化", 0.1, "经 str 转 Decimal 避免二进制误差",
                 repr(v.rows[0]["a"]))
        assert v.rows[0]["a"] == Decimal("0.1")
