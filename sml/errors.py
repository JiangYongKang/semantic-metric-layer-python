"""错误分类体系：所有拒绝都必须可归因、可区分。"""
from __future__ import annotations


class SMLError(Exception):
    """语义层所有错误的基类。"""

    code: str = "SML_ERROR"

    def __init__(self, message: str, *, detail: dict | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "detail": self.detail}


# ---- 数据集注册 / 结构推断 ----
class SchemaError(SMLError):
    code = "SCHEMA_ERROR"


class MissingFieldError(SchemaError):
    code = "MISSING_FIELD"


class TypeConflictError(SchemaError):
    code = "TYPE_CONFLICT"


class IncompleteStructureError(SchemaError):
    code = "INCOMPLETE_STRUCTURE"


# ---- 语义模型（关联 / 口径）----
class ModelError(SMLError):
    code = "MODEL_ERROR"


class NonUniqueJoinKeyError(ModelError):
    code = "NON_UNIQUE_JOIN_KEY"


class AmbiguousJoinError(ModelError):
    code = "AMBIGUOUS_JOIN"


class MetricConflictError(ModelError):
    code = "METRIC_CONFLICT"


class UnknownFieldError(ModelError):
    code = "UNKNOWN_FIELD"


# ---- 查询执行 ----
class QueryError(SMLError):
    code = "QUERY_ERROR"


class ResourceLimitError(QueryError):
    code = "RESOURCE_LIMIT"

    def __init__(self, message: str, *, limit_kind: str, budget, observed, detail=None):
        super().__init__(message, detail=detail)
        self.limit_kind = limit_kind
        self.budget = budget
        self.observed = observed
        self.detail.update({"limit_kind": limit_kind, "budget": budget, "observed": observed})
