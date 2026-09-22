# 灰度发布闸门 API（Canary Release Gate）

用 **Python + FastAPI + SQLite** 实现的灰度发布闸门服务：

- 登记服务版本与灰度百分比阶段（如 10% → 50% → 100%）
- 按用户标识做**稳定哈希分桶**（`sha256(service|version|user_id) % 100`），同一用户永远落在同一 cohort，扩量时单调包含、不抖动
- 接收成功/失败遥测，在**滑动窗口错误率**超过阈值时**自动暂停**灰度（可配置**自动回滚**到上一稳定版本）
- 提供发布、百分比提升、暂停/恢复/回滚、遥测上报、灰度路由、状态与审计查询接口
- 数据落 SQLite（WAL 模式），Docker 卷持久化；写操作在 `BEGIN IMMEDIATE` 事务内完成，遥测评估与状态翻转原子化，并发上报不会重复触发闸门

## 启动

### Docker Compose 一键启动（推荐）

```bash
docker compose up -d --build
```

- 端口：`http://localhost:8000`
- 交互式 API 文档（Swagger UI）：<http://localhost:8000/docs>
- 健康检查：`GET /health`
- 数据库：容器内 `/data/gate.db`，挂载在命名卷 `gate-data`，容器重建不丢数据

停止：

```bash
docker compose down          # 停止并删除容器（保留数据卷）
docker compose down -v       # 连同数据卷一起删除
```

### 本地直接运行（可选）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export GATE_DB_PATH=./data/gate.db
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

| 配置项 | 环境变量 | 默认值 |
| --- | --- | --- |
| SQLite 文件路径 | `GATE_DB_PATH` | `/data/gate.db`（容器内） |

服务级策略在登记服务时指定：`error_threshold_pct`（错误率阈值，默认 50%）、`window_seconds`（滑动窗口，默认 60 秒）、`auto_rollback`（超阈值自动回滚，默认 `false` 即仅暂停）。

> 闸门保护机制：窗口内样本不足 **5** 条时不触发自动动作，避免冷启动误伤。

## 发布状态机

```
            发布(percentage<100)         promote=100
 stable ───────────────────────► canary ───────────────► stable（旧 stable → superseded）
                                   │  ▲  │
                            暂停/闸门│  │resume
                                   ▼  │  │
                                 paused ┘  │ 闸门超阈值 / 手动
                                          ▼
                                      rolled_back（流量回到上一 stable）
```

- 服务的**第一个版本**或 `percentage=100` 直接登记为 `stable`
- 同时只允许一个进行中的灰度（`canary`/`paused`）
- `paused` / `rolled_back` 后，路由查询自动把流量切回当前 `stable`

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/services` | 登记服务（含阈值/窗口/自动回滚策略） |
| GET | `/api/services` | 服务列表 |
| GET | `/api/services/{name}` | 发布状态总览（含各版本窗口统计） |
| GET | `/api/services/{name}/releases` | 该服务所有版本 |
| GET | `/api/services/{name}/route?user_id=...` | 查询用户被路由到哪个版本（灰度判定） |
| POST | `/api/services/{name}/releases` | 登记新版本并设定灰度百分比 |
| POST | `/api/services/{name}/releases/{version}/promote` | 调整灰度百分比（100 即全量转正） |
| POST | `/api/services/{name}/releases/{version}/pause` | 手动暂停 |
| POST | `/api/services/{name}/releases/{version}/resume` | 恢复灰度 |
| POST | `/api/services/{name}/releases/{version}/rollback` | 手动回滚到上一稳定版 |
| POST | `/api/services/{name}/telemetry` | 上报单次请求成功/失败（触发闸门评估） |
| GET | `/api/audit` | 审计日志（可按 `service`、`action` 过滤、分页） |

## 示例 curl

### 1. 登记服务

```bash
curl -s -X POST http://localhost:8000/api/services \
  -H 'Content-Type: application/json' \
  -d '{"name":"payments","error_threshold_pct":50,"window_seconds":60,"auto_rollback":false}'
```

### 2. 登记基线版本 v1，并发布 10% 灰度版本 v2

```bash
# 首个版本直接成为 stable
curl -s -X POST http://localhost:8000/api/services/payments/releases \
  -H 'Content-Type: application/json' \
  -d '{"version":"v1.0.0","percentage":100}'

# 第二个版本进入 canary，仅 10% 用户命中
curl -s -X POST http://localhost:8000/api/services/payments/releases \
  -H 'Content-Type: application/json' \
  -d '{"version":"v2.0.0","percentage":10,"actor":"release-bot"}'
```

### 3. 查询某用户路由到哪个版本

```bash
curl -s 'http://localhost:8000/api/services/payments/route?user_id=user-42'
# 命中灰度: {"service":"payments","user_id":"user-42","version":"v2.0.0","cohort":"canary:3", ...}
# 未命中:   {... "version":"v1.0.0","cohort":"stable", ...}
```

### 4. 上报遥测（成功 / 失败）

```bash
curl -s -X POST http://localhost:8000/api/services/payments/telemetry \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"user-42","success":true}'

# 显式指定版本也可以；缺省记到当前 canary（无 canary 时记到 stable）
curl -s -X POST http://localhost:8000/api/services/payments/telemetry \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"user-7","success":false,"version":"v2.0.0"}'
```

连续失败使窗口错误率超阈值后，响应直接反映闸门动作：

```json
{"release": {"version": "v2.0.0", "status": "paused", ...},
 "window": {"total": 5, "failures": 5, "error_pct": 100.0},
 "action": "paused"}
```

`action` 为 `paused`（默认）或 `rolled_back`（服务开启 `auto_rollback` 时）。此后路由流量自动回到 v1。

快速制造 5 次失败触发闸门：

```bash
for i in 1 2 3 4 5; do
  curl -s -X POST http://localhost:8000/api/services/payments/telemetry \
    -H 'Content-Type: application/json' \
    -d "{\"user_id\":\"canary-user-$i\",\"success\":false}"
done
```

### 5. 提升灰度 / 暂停 / 恢复 / 回滚

```bash
# 扩量到 50%（已在灰度的用户不会被移出）
curl -s -X POST http://localhost:8000/api/services/payments/releases/v2.0.0/promote \
  -H 'Content-Type: application/json' -d '{"percentage":50}'

# 全量转正：v2 变 stable，v1 变 superseded
curl -s -X POST http://localhost:8000/api/services/payments/releases/v2.0.0/promote \
  -H 'Content-Type: application/json' -d '{"percentage":100}'

# 手动暂停 / 恢复 / 回滚
curl -s -X POST http://localhost:8000/api/services/payments/releases/v2.0.0/pause \
  -H 'Content-Type: application/json' -d '{"actor":"oncall","reason":"人工介入"}'
curl -s -X POST http://localhost:8000/api/services/payments/releases/v2.0.0/resume \
  -H 'Content-Type: application/json' -d '{"actor":"oncall"}'
curl -s -X POST http://localhost:8000/api/services/payments/releases/v2.0.0/rollback \
  -H 'Content-Type: application/json' -d '{"actor":"oncall","reason":"指标异常，回滚到 v1"}'
```

### 6. 查询状态与审计

```bash
# 状态总览：当前 stable、进行中的灰度、每个版本的窗口/累计成功失败
curl -s http://localhost:8000/api/services/payments | python3 -m json.tool

# 审计日志：按服务/动作过滤、分页
curl -s 'http://localhost:8000/api/audit?service=payments&limit=20'
curl -s 'http://localhost:8000/api/audit?action=release.pause'
```

审计动作类型：`service.create`、`release.create`、`release.set_percentage`、
`release.stable`、`release.pause`（手动与闸门自动，`actor=gate`）、
`release.resume`、`release.rollback`（手动与闸门自动）。

## 项目结构

```
.
├── app/
│   ├── main.py        # FastAPI 路由与事务边界
│   ├── gate.py        # 哈希分桶、发布状态机、滑动窗口闸门、审计
│   ├── db.py          # SQLite 连接（WAL）与 schema 初始化
│   └── schemas.py     # 请求模型
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── smoke_test.py      # 核心逻辑冒烟测试（无需外部依赖）
```
