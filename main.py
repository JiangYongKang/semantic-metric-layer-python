"""语义指标层 HTTP 服务：数据集注册、语义模型管理与指标查询。"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from sml.dataset import DatasetRegistry
from sml.engine import Filter, Query, QueryEngine
from sml.errors import SMLError
from sml.model import Agg, Join, Metric, RatioMetric, SemanticModel
from sml.resources import ResourceLimits

app = FastAPI(title="semantic-metric-layer")

registry = DatasetRegistry()
model = SemanticModel(registry)
engine = QueryEngine(registry, model, ResourceLimits())


@app.exception_handler(SMLError)
async def sml_error_handler(_: Request, exc: SMLError) -> JSONResponse:
    # 所有拒绝都带可区分的错误码与细节
    return JSONResponse(status_code=422, content=exc.to_dict())


class DatasetBody(BaseModel):
    rows: list[dict]


class JoinBody(BaseModel):
    name: str
    left_dataset: str
    left_key: str
    right_dataset: str
    right_key: str


class MetricBody(BaseModel):
    name: str
    dataset: str
    field: str | None = None
    agg: Agg


class RatioMetricBody(BaseModel):
    name: str
    num: str
    den: str


class FilterBody(BaseModel):
    field: str
    op: str
    value: object


class QueryBody(BaseModel):
    metrics: list[str]
    dimensions: list[str] = []
    filters: list[FilterBody] = []


@app.post("/datasets/{name}")
def register_dataset(name: str, body: DatasetBody):
    v = registry.register(name, body.rows)
    return {"dataset": v.name, "version": v.version,
            "schema": {f.name: f.dtype.value for f in v.schema.fields}}


@app.put("/datasets/{name}")
def update_dataset(name: str, body: DatasetBody):
    v = registry.update(name, body.rows)
    return {"dataset": v.name, "version": v.version,
            "schema": {f.name: f.dtype.value for f in v.schema.fields}}


@app.get("/datasets/{name}")
def get_dataset(name: str, version: int | None = None):
    v = registry.get(name, version)
    return {"dataset": v.name, "version": v.version,
            "schema": {f.name: f.dtype.value for f in v.schema.fields},
            "rows": len(v.rows)}


@app.post("/model/joins")
def add_join(body: JoinBody):
    model.add_join(Join(**body.model_dump()))
    return {"model_version": model.version}


@app.post("/model/metrics")
def add_metric(body: MetricBody):
    model.add_metric(Metric(**body.model_dump()))
    return {"model_version": model.version}


@app.post("/model/ratio-metrics")
def add_ratio_metric(body: RatioMetricBody):
    model.add_ratio_metric(RatioMetric(**body.model_dump()))
    return {"model_version": model.version}


@app.post("/query")
def run_query(body: QueryBody):
    q = Query(metrics=tuple(body.metrics), dimensions=tuple(body.dimensions),
              filters=tuple(Filter(**f.model_dump()) for f in body.filters))
    return engine.run(q).to_dict()
