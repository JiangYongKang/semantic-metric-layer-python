# Semantic Metric Layer (Python)

本地数据集的语义建模与指标查询层：多个本地数据源在统一口径下得到一致、
可解释、可追溯的聚合结果，并在结构变更、异常数据与并发访问下保持确定性。

## 支持的数据源形态

- 本地内存数据集：`list[dict]` 形式的行数据，通过 `DatasetRegistry.register/update`
  或 HTTP `POST/PUT /datasets/{name}` 注册。
- 字段类型：`int` / `decimal`（float 经 `str` 转 `Decimal`，无二进制误差）/
  `string` / `bool`；`None` 为合法空值。
- 注册时自动推断结构，以下情况**分类拒绝**（错误码可区分）：
  - `INCOMPLETE_STRUCTURE`：空数据集、行不是映射、出现未声明字段、全空列；
  - `MISSING_FIELD`：某些行缺少字段；
  - `TYPE_CONFLICT`：同名字段类型冲突（含更新时的类型变更）。
- 每次 `update` 产生新的不可变版本；历史版本保留，可按版本复现历史查询。

## 口径定义（语义模型）

- **关联（Join）**：仅支持多对一 `left(多) -> right(一)`。
  - 一侧键必须唯一：两侧均不唯一 -> `NON_UNIQUE_JOIN_KEY`（多对多，会膨胀）；
    声明方向与基数矛盾 -> `AMBIGUOUS_JOIN`；一侧键含空 -> `AMBIGUOUS_JOIN`。
  - 键类型必须一致，否则 `TYPE_CONFLICT`。
  - 执行时基于固定快照**复核**唯一性：定义后被更新破坏的关联在查询时显式拒绝。
- **基础指标（Metric）**：`sum / count / avg / min / max / count_distinct`。
  - `sum/avg` 仅允许数值字段；同名指标以不同口径重复注册 -> `METRIC_CONFLICT`。
  - `avg` 以 `(sum, count)` 从原始行累积，**绝不由预聚合结果再聚合**。
- **比率指标（RatioMetric）**：`ratio(num / den)`，逐组对分子分母各自求值后相除。

## 结果语义（固定且可解释）

- **空值**：维度为 NULL 的行归入 NULL 组；过滤谓词任一侧为 NULL 则不匹配；
  `sum/min/max/count_distinct` 忽略 NULL（全 NULL 结果为 `None`）；
  `count` 计行数；`avg` 分母为非 NULL 个数（全 NULL -> `None`）；
  比率分母为 0 或 NULL -> `None`（除零不报错、不静默给 0）。
- **精度**：全部聚合以 `Decimal` 精确累积；`avg/ratio` 仅在最终一步以
  10 位小数、`ROUND_HALF_EVEN` 舍入，多次关联与聚合后无漂移、可复现。
- **关联**：左连接语义，未匹配行进 NULL 组；任何关联不产生隐性行数膨胀
  （各组计数之和恒等于事实表行数）。
- **顺序**：输出按维度键排序，NULL 组在前，顺序稳定。
- **血缘**：每个输出列附带列级血缘（来源字段 + 口径表达式），比率指标
  血缘展开到原始字段；结果同时携带 `dataset_versions` 与 `model_version`。
- **结构变更**：新增字段缺省为 NULL 且不影响既有口径；删列/改名后引用旧列
  的查询显式报 `UNKNOWN_FIELD`，绝不静默错列；`pin_versions` 可复现历史结果。
- **资源上限**：`ResourceLimits(max_scan_rows, max_output_rows, max_elapsed_ms)`，
  超限抛出 `RESOURCE_LIMIT`（携带 `limit_kind/budget/observed`），失败查询
  不残留部分结果、不写缓存、不影响后续查询。
- **并发**：查询在固定版本快照上执行，不会观察到半更新状态；缓存键包含
  查询、全部数据集版本与模型版本，缓存永不与当前数据或口径不一致。

## 本地验证方法

```bash
# 安装依赖
uv sync

# 运行全部测试（单测日志会打印输入与判定依据）
uv run pytest -q -s

# 启动 HTTP 服务
uv run uvicorn main:app --reload

# 示例：注册 -> 建模 -> 查询
curl -X POST localhost:8000/datasets/orders -H 'content-type: application/json' \
  -d '{"rows": [{"oid": 1, "cid": 10, "amt": 0.1}, {"oid": 2, "cid": 10, "amt": 0.2}]}'
curl -X POST localhost:8000/datasets/customers -H 'content-type: application/json' \
  -d '{"rows": [{"cid": 10, "city": "hz"}]}'
curl -X POST localhost:8000/model/joins -H 'content-type: application/json' -d '{
  "name": "o2c", "left_dataset": "orders", "left_key": "cid",
  "right_dataset": "customers", "right_key": "cid"}'
curl -X POST localhost:8000/model/metrics -H 'content-type: application/json' \
  -d '{"name": "total", "dataset": "orders", "field": "amt", "agg": "sum"}'
curl -X POST localhost:8000/query -H 'content-type: application/json' \
  -d '{"metrics": ["total"], "dimensions": ["customers.city"]}'
```

## 代码结构

| 模块 | 职责 |
| --- | --- |
| `sml/errors.py` | 错误分类体系（可区分的拒绝原因） |
| `sml/schema.py` | 类型系统、结构推断、值规范化 |
| `sml/dataset.py` | 数据集注册与版本化不可变快照 |
| `sml/model.py` | 关联与指标口径、定义时校验 |
| `sml/engine.py` | 快照固定、关联执行、分组聚合、缓存 |
| `sml/lineage.py` | 列级血缘 |
| `sml/resources.py` | 资源预算与超限拒绝 |
| `main.py` | FastAPI 服务层 |
| `tests/` | 异常数据、关联歧义、精度边界、结构变更、超限、并发 |
