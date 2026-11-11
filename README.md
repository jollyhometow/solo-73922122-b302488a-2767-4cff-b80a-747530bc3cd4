# 新增班次影响分析 API

FastAPI 服务：接收运营导出的**调整前（旧表）、调整后（新表）两份时刻表**、各站最短换乘秒数及最多 30 条出行调查需求，分别求每条需求在两份表上的**最早到达时间**和一套**乘车方案**（列明车次与上、下车停站序号），并标出**旧表按期而新表超期或不可达**的受影响需求，供调整接驳安排。

## 乘车与求解规则

- 时间为同一基准的整数秒；到站不晚于发车、本站发车不晚于下站到站；允许重复停站和快车越行。
- 乘客可在车站等待，可同一车次跨站乘坐（同车停留不算换车），最多换车 2 次（最多 3 个车次段）。
- 首次上车无需缓冲；换车须满足 `接续车次发车 ≥ 到达时刻 + 本站最短换乘秒数`，**等号可接上**。
- 起终站相同时，最早到达即出发时刻，方案为空（0 个车次段）。
- 求解采用按乘车段数分层的最早到达松弛：每层固定对应一个车次段，层首快照保证换车次数约束不被绕过；在 20 车次 × 12 停站、30 条需求的规模下瞬时返回。

## 快速启动（Docker Compose）

```bash
docker compose up --build -d
```

- Swagger UI：<http://localhost:8000/docs>
- ReDoc：<http://localhost:8000/redoc>
- 健康检查：<http://localhost:8000/health>
- 两个内置示例（环线、换乘临界）：<http://localhost:8000/example>

停止：

```bash
docker compose down
```

如需更改宿主机端口：

```bash
HOST_PORT=18080 docker compose up --build -d
```

镜像基于 `python:3.11-slim`，以非 root 用户运行，并带容器健康检查。

## 请求

`POST /impact`

```json
{
  "old_timetable": {
    "label": "调整前",
    "trips": [
      {
        "trip_id": "T1",
        "stops": [
          {"station": "X站", "arrival": 0, "departure": 0},
          {"station": "P站", "arrival": 300, "departure": 320},
          {"station": "Y站", "arrival": 600, "departure": 600}
        ]
      }
    ]
  },
  "new_timetable": {
    "label": "调整后",
    "trips": [
      {
        "trip_id": "T1",
        "stops": [
          {"station": "X站", "arrival": 0, "departure": 0},
          {"station": "P站", "arrival": 300, "departure": 320},
          {"station": "Y站", "arrival": 600, "departure": 600}
        ]
      }
    ]
  },
  "minimum_transfer_seconds": {"X站": 30, "P站": 60, "Y站": 30},
  "demands": [
    {
      "demand_id": "M1",
      "origin": "X站",
      "destination": "Y站",
      "departure_time": 0,
      "arrival_deadline": 600
    }
  ]
}
```

| 字段 | 说明 |
| --- | --- |
| `old_timetable` / `new_timetable` | 时刻表，各含 1..20 个车次；车次号表内不重复 |
| `trips[].stops` | 每车次 2..12 条停站记录，含 `station`、`arrival`、`departure`，允许重复停站 |
| `minimum_transfer_seconds` | 站名 → 最短换乘秒数（非负整数）；时刻表中出现的每个车站都必须提供 |
| `demands` | 1..30 条需求：起终站、出发秒、到达期限；出发秒不晚于期限 |

校验失败（结构、时刻顺序、换乘秒数覆盖、需求站名等）返回 HTTP `422`。

## 响应

```json
{
  "status": "ok",
  "message": "共分析 2 条出行需求：1 条受影响（超期 0 条、不可达 1 条）……",
  "summary": {
    "total_demands": 2,
    "affected_count": 1,
    "affected_late_count": 0,
    "affected_unreachable_count": 1,
    "old_on_time_count": 2,
    "new_on_time_count": 1,
    "old_unreachable_count": 0,
    "new_unreachable_count": 1
  },
  "results": [
    {
      "demand_id": "M1",
      "origin": "X站",
      "destination": "Z站",
      "departure_time": 0,
      "arrival_deadline": 500,
      "old": {
        "reachable": true,
        "earliest_arrival": 500,
        "on_time": true,
        "journey": {
          "legs": [
            {"trip_id": "T1", "board_stop_index": 1, "alight_stop_index": 2,
             "board_station": "X站", "alight_station": "P站",
             "board_departure": 0, "alight_arrival": 300,
             "transfer_buffer_seconds": 0},
            {"trip_id": "T2", "board_stop_index": 1, "alight_stop_index": 2,
             "board_station": "P站", "alight_station": "Z站",
             "board_departure": 380, "alight_arrival": 500,
             "transfer_buffer_seconds": 60}
          ],
          "arrival_time": 500,
          "leg_count": 2,
          "transfer_count": 1
        }
      },
      "new": {"reachable": false, "earliest_arrival": null, "on_time": false, "journey": null},
      "affected": true,
      "impact_type": "unreachable"
    }
  ]
}
```

| 字段 | 说明 |
| --- | --- |
| `results[].old` / `new` | 需求在旧表、新表上的结果；不可达时 `reachable=false`、`journey=null`，**不可达单列** |
| `earliest_arrival` | 最早到达秒；不可达为 `null` |
| `on_time` | 最早到达是否不晚于 `arrival_deadline` |
| `journey.legs[]` | 车次段：车次号、上/下车停站序号（1-based，环线重复停站靠序号区分）、乘降站名与时刻、本段上车前使用的换乘缓冲秒数 |
| `affected` | **受影响**：旧表按期可达，而新表超期或不可达 |
| `impact_type` | `late`（新表超期到达）或 `unreachable`（新表不可达）；不受影响为 `null` |
| `summary` | 总数、受影响数（按超期/不可达分列）、新旧表按期数与不可达数 |

## 内置示例

### 1. 环线重复停站（`examples/ring_line_*.json`）

环线 L1 依次停靠 A→B→C→D→A→B，A、B 站重复停站，方案以停站序号区分。新表整体后移 120 秒：

- R1（A→B）：旧表 100 秒按期，新表 220 秒超期 → `late`；
- R2（B→A，乘至第二圈 A 站第 5 停站）：旧表 400 秒按期，新表 520 秒超期 → `late`；
- R3（C→D）：新表 420 秒到达，仍在期限 500 内 → 不受影响。

### 2. 换乘临界（`examples/transfer_critical_*.json`）

P 站最短换乘 60 秒。旧表 T1 于 300 秒到达 P 站，360 秒即可接续，T2 在 380 秒发车（等号可接上），M1 经 1 次换乘于 500 秒按期到达 Z 站；新表 T2 提前至 350 秒发车，360 > 350 赶不上且无后续班次，M1 变为**不可达** → `unreachable`。M2 全程乘坐 T1，两表均按期，不受影响。

示例调用：

```bash
curl -s -X POST http://localhost:8000/impact \
  -H 'Content-Type: application/json' \
  --data-binary @examples/transfer_critical_request.json
```

也可以打开 `/docs`，在 `POST /impact` 的 “Try it out” 中直接选择两个示例。

## 本地开发与测试

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt

uvicorn app.main:app --reload
python -m unittest discover -s tests -v
```

测试包含 700 余组随机时刻表，用独立的穷举实现（枚举至多 3 个车次段的全部乘降组合）交叉验证最早到达、可达性与换车次数上限。

## 目录

```text
app/
  main.py       FastAPI、请求/响应模型、内置示例与文档
  solver.py     时刻表校验、分层最早到达求解、新旧表影响分析
tests/
  test_solver.py    规则用例 + 随机穷举交叉验证
  test_api.py       接口、示例与校验测试
examples/
  ring_line_request.json / ring_line_response.json
  transfer_critical_request.json / transfer_critical_response.json
Dockerfile
docker-compose.yml
requirements.txt
requirements-dev.txt
```
