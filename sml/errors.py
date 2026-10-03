"""语义指标层的错误类型体系。

所有拒绝都必须使用可区分的异常类型，错误码 ``code`` 稳定可程序化判别，
禁止对结构/口径问题做静默接受或静默取其一。
"""

from __future__ import annotations


class SMLError(Exception):
    """所有语义指标层错误的基类。"""

    code: str = "sml_error"

    def __init__(self, message: str = "", *, details: dict | None = None) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        self.details: dict = details or {}

    def __str__(self) -> str:  # pragma: no cover - 简单格式
        d = f" details={self.details}" if self.details else ""
        return f"[{self.code}] {self.message}{d}"


class DatasetError(SMLError):
    """数据集注册/推断相关错误的基类。"""

    code = "dataset_error"


class FieldMissingError(DatasetError):
    """结构声明了字段，但数据源中缺失（列不存在或全空无法推断）。"""

    code = "field_missing"


class TypeConflictError(DatasetError):
    """同一字段的值无法归入唯一逻辑类型。"""

    code = "type_conflict"


class IncompleteStructureError(DatasetError):
    """结构不完整：空数据集、空字段名、缺少主键等。"""

    code = "incomplete_structure"


class SourceUpdateError(DatasetError):
    """数据源更新破坏了既有结构（删列/改名/类型漂移）。"""

    code = "source_update_rejected"


class DuplicatePrimaryKeyError(DatasetError):
    """声明的主键在数据中存在重复值。"""

    code = "duplicate_primary_key"


class DatasetAlreadyExistsError(DatasetError):
    """同名数据集已注册。"""

    code = "dataset_already_exists"


class DatasetNotFoundError(DatasetError):
    """引用了不存在的数据集。"""

    code = "dataset_not_found"


class RelationError(SMLError):
    """关联关系定义/校验相关错误的基类。"""

    code = "relation_error"


class AmbiguousJoinError(RelationError):
    """关联方向歧义或路径不唯一。"""

    code = "ambiguous_join"


class NonUniqueKeyError(RelationError):
    """关联键在被引用侧不唯一，可能造成隐性行数膨胀。"""

    code = "non_unique_key"


class CaliberError(SMLError):
    """聚合口径定义/使用相关错误的基类。"""

    code = "caliber_error"


class CaliberConflictError(CaliberError):
    """同一指标存在互相冲突的口径定义。"""

    code = "caliber_conflict"


class CaliberNotFoundError(CaliberError):
    """引用了不存在的指标/维度口径。

    历史版本回查时，目标版本之后才定义的口径同样抛本错误，
    ``details.reason = "not_defined_at_version"`` 并携带
    ``requested_version`` / ``current_version`` / ``defined_in_current``，
    明确表达「该版本上这个口径还不存在」，禁止用当前口径顶替。
    """

    code = "caliber_not_found"


class InvalidCaliberError(CaliberError):
    """口径表达式非法（如比率口径引用平均口径、字段不存在等）。"""

    code = "invalid_caliber"


class QueryError(SMLError):
    """查询本身非法（字段未知、过滤值类型不对等）。"""

    code = "query_error"


class ResourceLimitError(SMLError):
    """查询超出资源预算（行数/耗时/内存/高基数）。

    ``rejected=True`` 表示查询被直接拒绝；否则表示结果被截断。
    """

    code = "resource_limit"

    def __init__(
        self,
        message: str,
        *,
        limit_kind: str,
        budget: float,
        observed: float,
        rejected: bool = True,
        details: dict | None = None,
    ) -> None:
        d = {
            "limit_kind": limit_kind,
            "budget": budget,
            "observed": observed,
            "rejected": rejected,
        }
        if details:
            d.update(details)
        super().__init__(message, details=d)
        self.limit_kind = limit_kind
        self.budget = budget
        self.observed = observed
        self.rejected = rejected


class ConcurrencyError(SMLError):
    """并发冲突（如版本已过期）。"""

    code = "concurrency_conflict"


class VersionError(SMLError):
    """历史版本回查相关错误的基类。"""

    code = "version_error"


class VersionNotFoundError(VersionError):
    """指定的版本号从未存在（如大于当前版本、非正数）。

    与「曾经存在但已被淘汰」严格区分：调用方绝不能用当前版本回退冒充。
    """

    code = "version_not_found"


class VersionEvictedError(VersionError):
    """版本曾经存在，但因超过历史保留上限被淘汰。

    details 携带 ``requested`` / ``oldest_available`` / ``retention``，
    便于调用方区分「从未存在」与「曾经存在但已不可回查」。
    """

    code = "version_evicted"


class VersionOutOfRangeError(VersionError):
    """版本号早于可回查范围（从未被保留进历史，而非保留后被淘汰）。"""

    code = "version_out_of_range"


class RetentionConfigError(SMLError):
    """历史版本保留配置非法（如负数）。"""

    code = "invalid_retention"
