"""FastAPI 服务层：把 :class:`~sml.registry.MetricLayer` 暴露为 HTTP 接口。

端点：

- ``GET  /health``                    健康检查
- ``GET  /meta``                      当前模型版本、数据集/关联/指标清单
- ``POST /datasets/{name}``           注册数据集（records + 可选 primary_key）
- ``PUT  /datasets/{name}/data``      更新数据内容（删列/改名会被拒绝）
- ``POST /dimensions`` / ``/measures`` / ``/ratios`` / ``/relations`` 定义口径
- ``POST /query``                     指标查询（返回行 + 列级血缘 + 版本）
- ``GET  /cache/size`` ``POST /cache/clear``

所有 :class:`~sml.errors.SMLError` 统一映射为 HTTP 4xx，响应体携带稳定
错误码 ``error.code`` 与结构化 ``details``；Decimal 序列化为字符串，
从协议层杜绝浮点精度漂移。
"""

from __future__ import annotations

import json
from decimal import Decimal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .caliber import AggKind, DimensionSpec, MeasureSpec, RatioSpec
from .engine import Filter, Query
from .errors import (
    SMLError,
    CaliberConflictError,
    CaliberNotFoundError,
    InvalidCaliberError,
    AmbiguousJoinError,
    NonUniqueKeyError,
    QueryError,
    ResourceLimitError,
)
from .limits import LimitAction, ResourceBudget
from .registry import MetricLayer
from .relations import Cardinality, Relation
from .sources import normalize_text_records

# 错误码 -> HTTP 状态码
_STATUS_MAP = {
    "field_missing": 422,
    "type_conflict": 422,
    "incomplete_structure": 422,
    "source_update_rejected": 409,
    "duplicate_primary_key": 409,
    "dataset_already_exists": 409,
    "dataset_not_found": 404,
    "ambiguous_join": 409,
    "non_unique_key": 409,
    "relation_error": 400,
    "caliber_conflict": 409,
    "caliber_not_found": 404,
    "invalid_caliber": 422,
    "query_error": 400,
    "resource_limit": 429,
    "concurrency_conflict": 409,
}


class _JSONEncoder(json.JSONEncoder):
    def default(self, o: object) -> object:
        if isinstance(o, Decimal):
            # 'f' 格式：固定小数写法（0.0000000000 而非 0E-10），跨端可复现
            return format(o, "f")
        return super().default(o)


def _jsonable(value: object) -> object:
    return json.loads(json.dumps(value, cls=_JSONEncoder))


def create_app(layer: MetricLayer | None = None) -> FastAPI:
    layer = layer or MetricLayer()
    app = FastAPI(title="Local Semantic Metric Layer", version="0.1.0")
    app.state.layer = layer

    @app.exception_handler(SMLError)
    async def _sml_error_handler(_: Request, exc: SMLError) -> JSONResponse:
        status = _STATUS_MAP.get(exc.code, 400)
        return JSONResponse(
            status_code=status,
            content={"error": {"code": exc.code, "message": exc.message,
                               "details": _jsonable(exc.details)}},
        )

    @app.get("/")
    def root() -> dict:
        return {"service": "semantic-metric-layer", "version": "0.1.0"}

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/meta")
    def meta() -> dict:
        return {
            "model_version": layer.model_version() or 0,
            "datasets": layer.list_datasets(),
            "relations": layer.list_relations(),
            "metrics": layer.list_metrics(),
            "cache_size": layer.cache_size(),
        }

    @app.post("/datasets/{name}")
    async def register_dataset(name: str, request: Request) -> dict:
        body = await request.json()
        records = normalize_text_records(body.get("records", []))
        pk = tuple(body.get("primary_key", ()))
        ds = layer.register_dataset(name, records, primary_key=pk)
        return {
            "dataset": name,
            "version": ds.version,
            "model_version": layer.model_version(),
            "schema": ds.schema.to_dict(),
        }

    @app.put("/datasets/{name}/data")
    async def replace_data(name: str, request: Request) -> dict:
        body = await request.json()
        records = normalize_text_records(body.get("records", []))
        ds = layer.replace_dataset_data(name, records)
        return {
            "dataset": name,
            "version": ds.version,
            "model_version": layer.model_version(),
            "schema": ds.schema.to_dict(),
        }

    @app.post("/dimensions")
    async def add_dimension(request: Request) -> dict:
        b = await request.json()
        layer.add_dimension(DimensionSpec(b["name"], b["dataset"], b["field"]))
        return {"ok": True, "model_version": layer.model_version()}

    @app.post("/measures")
    async def add_measure(request: Request) -> dict:
        b = await request.json()
        layer.add_measure(
            MeasureSpec(
                name=b["name"],
                dataset=b["dataset"],
                agg=AggKind(b["agg"]),
                field=b.get("field"),
            )
        )
        return {"ok": True, "model_version": layer.model_version()}

    @app.post("/ratios")
    async def add_ratio(request: Request) -> dict:
        b = await request.json()
        layer.add_ratio(
            RatioSpec(b["name"], b["dataset"], b["numerator"], b["denominator"])
        )
        return {"ok": True, "model_version": layer.model_version()}

    @app.post("/relations")
    async def add_relation(request: Request) -> dict:
        b = await request.json()
        layer.add_relation(
            Relation(
                name=b["name"],
                left_dataset=b["left_dataset"],
                left_keys=tuple(b["left_keys"]),
                right_dataset=b["right_dataset"],
                right_keys=tuple(b["right_keys"]),
                cardinality=Cardinality(b["cardinality"]),
            )
        )
        return {"ok": True, "model_version": layer.model_version()}

    @app.post("/query")
    async def query(request: Request) -> dict:
        b = await request.json()
        q = Query(
            dimensions=tuple(b.get("dimensions", ())),
            measures=tuple(b.get("measures", ())),
            filters=tuple(
                Filter(
                    dimension=f["dimension"],
                    values=tuple(f.get("values", [])),
                    negate=bool(f.get("negate", False)),
                )
                for f in b.get("filters", [])
            ),
        )
        bb = b.get("budget", {})
        budget = ResourceBudget(
            max_output_rows=int(bb.get("max_output_rows", 10_000)),
            max_intermediate_rows=int(bb.get("max_intermediate_rows", 1_000_000)),
            max_groups=int(bb.get("max_groups", 100_000)),
            timeout_seconds=float(bb.get("timeout_seconds", 5.0)),
            max_bytes=int(bb.get("max_bytes", 256 * 1024 * 1024)),
            on_overflow=LimitAction(bb.get("on_overflow", "reject")),
        )
        result = layer.query(q, budget=budget, use_cache=bool(b.get("use_cache", True)))
        return {
            "columns": list(result.columns),
            "rows": [list(_jsonable_cell(v) for v in row) for row in result.rows],
            "lineage": result.lineage.to_dict(),
            "model_version": result.model_version,
            "truncated": result.truncated,
            "resource_stats": result.resource_stats,
        }

    @app.get("/cache/size")
    def cache_size() -> dict:
        return {"cache_size": layer.cache_size()}

    @app.post("/cache/clear")
    def cache_clear() -> dict:
        layer.clear_cache()
        return {"ok": True}

    return app


def _jsonable_cell(v: object) -> object:
    if isinstance(v, Decimal):
        return format(v, "f")
    return v
