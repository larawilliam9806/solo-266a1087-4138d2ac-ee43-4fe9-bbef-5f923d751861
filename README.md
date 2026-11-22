# 风机轴承早期失效在运统计服务

按轴承序列号维护出厂批次、首装日、首次失效日和一段段在运记录，按「批次 × 日龄」给出**在险台日**与**失效件数**，供质检比较不同批次的早期失效。

- FastAPI 供数服务，SQLite 存台账
- `docker compose up --build` 即可启动
- 日龄以首装日为第 0 天，查询区间为闭区间
- 在运区间含首尾；同一件同一天只算一次；首次失效日之后停计；失效当天必须有在运记录才计入失效件数
- 台账与批次统计在同一事务内生效，失败时二者保持原样
- 查询只读已保存的批次统计；订正只读取该轴承的明细；核算与存储都不逐日展开单件区间

## 启动

```bash
docker compose up --build
# http://localhost:8000/docs
```

本地不用 Docker：

```bash
pip install -r requirements.txt
DB_PATH=data/bearings.db uvicorn app.main:app --port 8000
```

测试：

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

## 数据模型

| 表 | 含义 |
| --- | --- |
| `bearings` | 序列号 → 出厂批次、首装日、首次失效日（可空）。批次与首装日登记后不可改，避免历史日龄整体漂移。 |
| `operation_intervals` | `(序列号, 记录号)` → 在运起止日。按记录号登记或订正。 |
| `batch_exposure_segments` | 批次统计：`[age_start, age_end]` 段内每个日龄的在险台数（游程编码，不逐日落行）。 |
| `batch_failure_counts` | 批次统计：`(批次, 日龄)` → 失效件数。 |

### 口径

- **在险台日**：某日龄上处于在运的轴承台数之和。一件轴承在同一日龄即使有多条重叠在运记录也只算 1 台日。
- **停计**：首装日到首次失效日（含当天）为止；失效日之后不再产生在险台日。
- **失效件数**：首次失效日的日龄落在某条在运记录内才计 1 件；否则不计。首次失效日可补报、订正或清除，统计随之增量调整。
- **日龄**：`first_failure_date - first_install_date` 的天数，首装日为 0。早于首装日的在运记录从第 0 天起截断。
- **重叠与空档**：多条记录重叠取并集，中间的日期空档不计在险。

### 不逐日展开

在运区间先按轴承合并为若干日龄闭区间，再以区间加/减的方式折叠进批次的游程段（`app/stats.py::_add_range`）。单件区间和批次统计的行数都与**天数无关**，只与区间端点数量有关；查询直接对段做区间交集求和，订正只读取该序列号自己的记录。

### 订正的增量链路

在运记录或首次失效日订正时，先对新旧两套单件覆盖做区间集合差（`stats.coverage_delta` / `subtract_ranges`），得到按日龄的**净增减**：

- 净增减为空（覆盖没变）时完全不碰在险段，台账照常保存；失效件数按新旧失效日龄单独增减，与在险段互不影响。
- 净增减非空时只取变化日龄窗口内的统计段以及窗口两侧各一个可能合并的邻段（`stats.reconcile_segments`，固定三次索引查询），在内存里套区间增量后按主键对账：内容不变的行原样保留（含主键），仅删除/重写真正变化的行；相邻同台数的段就地合并，其余无关段不读、不删、不写。
- 取数与写入量只取决于变化区间端点和局部段数量，与批次已有的无关段数量无关，核算全程不逐日展开。
- 查询接口、数据库结构均保持原样，不需要全量重算；`service.rebuild_statistics()` 仍可从台账整体重建用于校验。

## 接口

所有路径同时挂载在 `/`、`/api`、`/api/v1` 三个前缀下。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查 |
| `POST` | `/bearings` | 登记轴承：`{"serial_number","batch","first_install_date"}`。重复登记相同内容返回 200 且 `created=false`；批次或首装日不同返回 409。 |
| `GET` | `/bearings` | 列出全部轴承及明细 |
| `GET` | `/bearings/{serial_number}` | 该轴承台账明细 |
| `PUT`/`POST` | `/bearings/{serial_number}/failure` | 补报/订正/清除首次失效日：`{"first_failure_date": "YYYY-MM-DD"}`，传 `null` 清除 |
| `PUT` | `/bearings/{serial_number}/intervals/{record_id}` | 按记录号登记或订正在运区间：`{"start_date","end_date"}` |
| `POST` | `/bearings/{serial_number}/intervals` | 同上，记录号放在请求体（新增返回 201） |
| `DELETE` | `/bearings/{serial_number}/intervals/{record_id}` | 删除一条在运记录 |
| `GET` | `/exposure?batch=&age_start=&age_end=` | **唯一查询口径**，读已保存的批次统计 |
| `GET` | `/batches/{batch}/exposure?age_start=&age_end=` | 同上，批次放在路径 |
| `GET` | `/batches/{batch}/exposure/points` | 可选：按日龄逐点列出，仅由已保存的段在响应中展开，不落库、不重算单件区间（不在 OpenAPI 文档中展示） |

`/exposure` 返回：

```json
{
  "batch": "B9",
  "age_start": 0,
  "age_end": 100,
  "at_risk_bearing_days": 15,
  "failure_count": 1
}
```

`age_end` 省略时等于 `age_start`；`age_end < age_start` 返回 400，负数日龄返回 422。

请求体字段同时接受若干常见别名（如 `sn`/`serial`、`batch_no`、`installed_on`、`record_no`、`from_date`/`to_date`），日期接受 `YYYY-MM-DD` 或 ISO 时间戳。

## 示例

```bash
curl -X POST localhost:8000/bearings -H 'content-type: application/json' \
  -d '{"serial_number":"S1","batch":"B1","first_install_date":"2024-01-01"}'
curl -X PUT localhost:8000/bearings/S1/intervals/R1 -H 'content-type: application/json' \
  -d '{"start_date":"2024-01-01","end_date":"2024-01-31"}'
curl -X PUT localhost:8000/bearings/S1/failure -H 'content-type: application/json' \
  -d '{"first_failure_date":"2024-01-11"}'
curl 'localhost:8000/exposure?batch=B1&age_start=0&age_end=30'
# {"batch":"B1","age_start":0,"age_end":30,"at_risk_bearing_days":11,"failure_count":1}
```

## 一致性

- 每次写入用 `BEGIN IMMEDIATE` 串行化，读旧状态、算增量、写台账与统计在同一事务提交；任一步异常整体回滚。
- 批次统计只存增量结果，不缓存单件状态；订正只重写变化窗口涉及的局部段，无关段按主键原样保留；`service.rebuild_statistics()` 可从台账全量重建用于校验（测试 `test_rebuild_matches_incremental_statistics`）。
- SQLite 以 WAL 模式运行，卷挂载在 `/data`。
