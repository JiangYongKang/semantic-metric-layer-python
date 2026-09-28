# 本地语义指标层（Semantic Metric Layer, Python）

在多个**本地数据源**之上建立统一语义模型：注册并推断数据集结构、定义关联与聚合口径、
执行分组/过滤/聚合查询，并为每个结果列附带**列级血缘**。全部计算基于整数与
`decimal.Decimal`，在结构变更、异常数据与并发访问下结果**确定、可解释、可复现、可追溯**。

## 1. 支持的数据源形态

| 形态 | 入口 | 说明 |
| --- | --- | --- |
| 内存记录 | `MetricLayer.register_dataset(name, records, primary_key=...)` | `list[dict]`，值为 `int`/`Decimal`/`str`/`bool`/`None` |
| CSV 文件 | `sml.sources.LocalSource.from_csv(name, path)` | 必须带表头；单元格经确定性标量解析 |

**入站值规则（CSV 与 JSON API 共用）**

- 空串/纯空白 → `NULL`；`true/false/yes/no`（大小写不敏感）→ 布尔
- 纯整数 → `int`（任意精度）；`10.50`、`1E+3` 等 → `Decimal`
- `nan/inf/infinity` 一律拒绝；Python/JSON `float` 一律拒绝
  （金额请以字符串 `"10.50"` 或整数传递，二进制浮点不允许进入精确数值体系）
- 其余文本保持原样；内存里的字符串**不**隐式转数值（只有 CSV/JSON 文本载体走解析）

**结构注册拒绝原因（可区分错误码，绝不静默接受）**

| 错误码 `error.code` | 触发条件 |
| --- | --- |
| `incomplete_structure` | 0 行/0 列、空列名、表头重名、CSV 行列数不一致、主键为空 |
| `field_missing` | 行列集合不一致（缺列/多列）、列全空无法推断、主键列不存在 |
| `type_conflict` | 同列值无法归一为唯一逻辑类型、出现 float/日期时间等不支持类型 |
| `duplicate_primary_key` | 主键值重复 |
| `dataset_already_exists` | 同名数据集重复注册 |

## 2. 口径定义（Caliber）

- **维度口径** `DimensionSpec(name, dataset, field)`：分组键来自某数据集某字段。
- **基础指标** `MeasureSpec(name, dataset, agg, field?)`，`agg ∈`
  `sum / count / avg / min / max / count_distinct`；`count` 可不带字段（行数）。
  `sum/avg/min/max` 的字段必须是数值列，否则 `invalid_caliber`。
- **比率口径** `RatioSpec(name, dataset, numerator, denominator)`：
  分子、分母必须是**同一数据集**上的**基础口径**，禁止基于 `avg` 或其他比率构造。
- 口径名全局唯一（维度/指标/比率共享命名空间），重名 → `caliber_conflict`。

### 关联关系（Relation）

`Relation(name, left_dataset, left_keys, right_dataset, right_keys, cardinality)`，
基数以 left 相对 right 描述：`many_to_one` / `one_to_many` / `one_to_one`。

- 仅支持星型安全的基数，`many_to_many` 不允许构造；
- `many_to_one` / `one_to_one` 要求 one 侧关联键在**数据中唯一且非空**，
  否则 `non_unique_key`（从注册期就杜绝隐性行数膨胀，而非运行时去重兜底）；
- 同一对数据集之间只允许一条关联，重复 → `ambiguous_join`；
- 查询路径必须**唯一**：存在多条候选路径 → `ambiguous_join`（不静默选其一）；
  需要从 one 侧向 many 侧扇出（如按维度表反查事实）同样 → `ambiguous_join`。

一次查询中的所有指标必须来自**同一个事实数据集**；维度可来自沿唯一
many→one/one_to_one 路径可达的数据集，违反 → `query_error`。

## 3. 查询结果语义（固定且可解释）

| 情形 | 固定语义 |
| --- | --- |
| 连接匹配不到（左连接） | 维度值为 `NULL`，事实行不丢失、不重复 |
| `in` 过滤 | 行值 ∈ 集合；空值行仅在集合显式含 `null` 时选中 |
| `not in` 过滤 | `in` 的确定补集：`not in (具体值)` 保留空值桶；`not in (..., null)` 显式排除空值桶 |
| 分组键为 `NULL` | 进入固定空值桶，输出 `null`；排序恒在最前 |
| `sum` 无参与值（全空） | `0` |
| `count(*)` | 统计通过过滤的行数（含所有列均为空的行） |
| `count_distinct` | 忽略 `NULL`，无值时为 `0` |
| `avg` / `min` / `max` 无参与值 | `NULL` |
| `avg` | 恒等于明细上的 `sum(非空值)/count(非空值)`，**不由预聚合结果再聚合** |
| 比率 | 在明细行上分别聚合分子、分母后相除；**不由预聚合结果再聚合** |
| 除零 / 分母为 `NULL` | 结果恒为 `NULL`（不抛异常、不产生 inf/NaN） |
| 比率 vs 平均在空组的区别 | `sum/count(*)` 型比率在「有行但金额全空」组为 `0/行数=0`（金额合计确为 0）；`avg(金额)` 同组为 `NULL`（没有可平均的值）。二者语义不同，均按上表固定规则产出 |
| 分组排序 | 维度值确定性升序，空值桶最前；同数据多次执行逐字节一致 |

### 精度规则

- 全程 `int` / `Decimal`，无 float 参与；Decimal 上下文 `prec=65`、`ROUND_HALF_UP`；
- 所有除法结果统一量化到 **10 位小数**（`ROUND_HALF_UP`），跨平台、跨重复关联可复现；
- 求和保留全部精确位数（大数场景如 `99999999999999999999.99 × 3` 精确成立）；
- HTTP/JSON 中 Decimal 以**字符串**输出，防止客户端反序列化造成精度漂移。

## 4. 列级血缘（Lineage）

每次查询响应都带 `lineage.columns`，与结果列**一一对应、同序、同来源计算**：

```json
{
  "output_column": "city",
  "kind": "dimension",
  "caliber": "city",
  "sources": ["customers.city"],
  "transforms": ["group_by"],
  "via_relations": ["o_c"]
}
```

`kind ∈ dimension / measure / avg / ratio`；比率列的 `transforms` 显式标注
“在明细行上计算（computed on detail rows）”。血缘与结果在引擎内同一次计算产出，
不存在事后补造或错位的可能。

## 5. 结构变更、历史版本回查与安全发布

### 版本号

- 数据集每次成功注册/更新、口径或关联每次成功登记都会让**模型版本**单调 +1；
  版本号从 1 开始、连续、无空洞；每个数据集自身也有从 1 开始的数据版本。
- 内容更新只允许「列集合不变」或「仅新增列」；
  **删列/改名 → `source_update_rejected`（HTTP 409）**，杜绝查询静默错列；
  新增列对旧数据以 `NULL` 补且 `nullable=true`；旧列类型漂移 → `type_conflict`。
- 查询结果携带 `model_version`；未指定版本时始终查询**当前最新版本**。

### 按历史版本回查

每次成功变更后，被取代的旧模型以**不可变快照**进入版本档案；查询时显式
指定版本即在当时的快照上重放，返回**当时的数据、关联、口径与列级血缘**，
绝不拿当前数据/口径顶替：

```python
old = layer.query(query).model_version          # 记录当时版本
layer.replace_dataset_data("orders", new_rows)  # 之后数据已变
layer.query(query, at_version=old)              # 结果与当时当次完全一致
```

- 历史查询支持与当前完全相同的维度、指标、过滤（含三值 NOT IN）与资源预算
  语义（reject / truncate / 超时 / 高基数）；
- 历史查询在独立快照上执行，**不读、不写当前版本缓存**，因此不会改变当前
  版本的缓存命中与查询结果，也不改变任何已发布状态；
- 返回结果的 `resource_stats.historical=true`、`cache_hit=false`；
  HTTP 响应另外携带 `current_version` 以便对账方核对。

### 历史保留与淘汰边界

构造层时设置除当前版本外最多保留的历史版本数（`MetricLayer(history_retention=n)`），
也可随时用 `layer.set_history_retention(n)` 调整并**立即**按规则淘汰：

- `None`（默认）：不限；`0`：不保留任何历史版本；正整数：保留最近 n 个。
- 淘汰规则固定为 **FIFO：超出上限时淘汰最旧版本**，稳定可预测；调小上限会
  立即淘汰多余版本，调大上限**不会**让已淘汰版本复活。
- 观察接口：`retained_versions()`、`oldest_retained_version()`、
  `history_retention()`；HTTP：`GET /versions`、`POST /versions/retention`。

指定了版本但查不到时，三种情形**严格区分、绝不用当前版本回退冒充**：

| 情形 | 错误码（HTTP） | details.reason |
| --- | --- | --- |
| 版本号从未存在（>当前版本、0、负数、非整数） | `version_not_found`（404） | `future_version` / `invalid_version` |
| 版本曾归档、后因保留上限被淘汰（含缩容淘汰） | `version_evicted`（410） | `evicted_by_retention` |
| 从未进入可回查范围（retention=0 时发布、早于最早归档点） | `version_out_of_range`（410） | `out_of_retention_range` |

错误 details 统一携带 `requested` / `current_version` / `oldest_available` /
`retention`，便于程序化区分「从未存在」「曾经存在已淘汰」「超范围」。

### 失败发布不污染、历史不串档

数据更新（`replace_dataset_data`）采用**「候选副本全量预检 → 原子提交」**：
先在与当前已发布模型同构的隔离副本上替换数据并整体重建模型——关联键唯一性、
结构兼容（删列/改名/类型漂移）、主键唯一非空、口径有效性、查询路径全部在
新数据上重新校验；任一项不过，整次更新在提交前拒绝。

- 只要会让**关联键不再唯一/出现空键、结构不兼容、主键重复或口径失效**，
  整次更新拒绝（`non_unique_key` / `source_update_rejected` /
  `type_conflict` / `duplicate_primary_key` 等）；
- 拒绝后**当前版本号、当前模型、查询结果、当前缓存**与更新前完全一致：
  不会出现版本前进但数据仍旧、或数据已变但版本/缓存停留的混合状态；
  失败更新也不会在历史档案中留下版本；
- 拒绝后继续正常定义口径/关联、执行合法更新均不受影响；
- 并发下任何读取只可能看到某一个**完整版本**（全局锁 + 快照指针原子替换 +
  查询全程只持快照），不可能观察到半更新状态；历史档案与当前缓存互不串档。

## 6. 资源上限与超限处置

`ResourceBudget`（查询请求体可覆盖默认值）：

| 预算 | 默认 | 超限行为 |
| --- | --- | --- |
| `max_output_rows` | 10,000 | reject：拒绝；truncate：截断并置 `truncated=true` |
| `max_intermediate_rows` | 1,000,000 | 拒绝（`max_intermediate_rows`） |
| `max_groups`（高基数） | 100,000 | 分组物化**前**按基数拒绝 |
| `timeout_seconds` | 5.0 | 墙钟超时拒绝；预算 ≤0 立即拒绝 |
| `max_bytes` | 256 MiB | 近似内存占用拒绝 |

被拒绝的查询**不返回任何部分结果**、不写缓存、不影响后续查询（HTTP 429，
错误码 `resource_limit`，details 含 `limit_kind/budget/observed/rejected`）。

## 7. 并发一致性

- 全局可重入锁保护注册/更新/查询；模型以**不可变快照**发布，查询只持快照。
- 更新是「候选副本预检 → 原子替换指针」，查询不可能观察到半更新状态。
- 查询缓存按 `(查询形状, 预算)` 键控，且任何模型升版都**整体清空缓存**，
  保证缓存永不落后于当前数据/口径；失败查询不写缓存。
- 历史版本查询走独立快照，不触碰当前缓存；历史淘汰只影响档案，当前版本与
  当前缓存不受影响。

## 8. HTTP 接口速览

```
GET  /health
GET  /meta                                   # 模型版本、数据集/关联/指标清单
GET  /versions                               # 当前/可回查历史版本、保留上限
POST /versions/retention                     # {"retention": n|null} 立即 FIFO 淘汰
POST /datasets/{name}                        # {"records": [...], "primary_key": [...]}
PUT  /datasets/{name}/data                   # 内容更新（删列改名/键失唯一会拒绝）
POST /dimensions                             # {"name","dataset","field"}
POST /measures                               # {"name","dataset","agg","field"?}
POST /ratios                                 # {"name","dataset","numerator","denominator"}
POST /relations                              # {"name","left_dataset","left_keys",
                                             #  "right_dataset","right_keys","cardinality"}
POST /query                                  # {"dimensions":[],"measures":[],
                                             #  "filters":[...],"budget":{...},
                                             #  "at_version": 3}   # 可选：历史回查
GET  /cache/size ; POST /cache/clear
```

历史回查响应额外含 `"historical": true` 与 `"current_version": <int>`；
版本不存在时按上表返回 404/410 与稳定错误码。

错误响应统一为：

```json
{"error": {"code": "non_unique_key", "message": "...", "details": {...}}}
```

启动：`uv run uvicorn main:app --reload`，交互文档：`http://127.0.0.1:8000/docs`。

## 9. 本地验证方法

```bash
# 环境（Python >= 3.14, uv）
uv sync

# 全部单测（覆盖异常数据/关联歧义/精度边界/结构变更/超限/并发/CSV/HTTP/
#           历史回查/版本淘汰/失败发布）
uv run python -m unittest discover -s tests -v

# 只跑某个专题
uv run python -m unittest tests.test_engine -v       # 结果语义与精度
uv run python -m unittest tests.test_concurrency -v  # 资源上限与并发
uv run python -m unittest tests.test_history -v      # 历史回查/淘汰/失败发布/并发完整版本
uv run python -m unittest tests.test_relations -v    # 关联歧义与膨胀

# 端到端可运行示例（建数、定义口径、查询、打印血缘）
uv run python examples/basic_usage.py
```

`tests.test_history` 覆盖：历史回查结果与列级血缘和当时一致、历史查询支持
同样的过滤/预算语义且不污染当前缓存、retention 的 FIFO 边界（含 0/不限/缩容）、
三种版本不存在错误码区分、失败发布后版本/缓存/结果/后续登记完全不变、
并发更新与读取只见完整版本、历史与当前查询并发隔离。

单测基类 `tests._support.LoggingTestCase` 会为关键判定打印
`[输入] / [判定依据] / [实际]`，便于追溯每个错误归类与边界结果。

### 错误码总表

`field_missing` · `type_conflict` · `incomplete_structure` · `duplicate_primary_key` ·
`dataset_already_exists` · `dataset_not_found` · `source_update_rejected` ·
`non_unique_key` · `ambiguous_join` · `relation_error` ·
`caliber_conflict` · `caliber_not_found` · `invalid_caliber` ·
`query_error` · `resource_limit` · `concurrency_conflict` ·
`version_not_found` · `version_evicted` · `version_out_of_range` ·
`invalid_retention`
