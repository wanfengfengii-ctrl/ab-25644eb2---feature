# 纸本修复室 · 雾化管路配平系统

回湿脆化古画前，审核雾化管路能否把规定水量稳定送到每个分区：
**一处水源、2~4 个分区、0~4 个分流节点、4~10 条有向管路**，求整数流量方案。

- 水源流出**恰等于**水源总量；
- 每个分流节点流入 = 流出（不会凭空增减流量）；
- 每个分区流入**恰等于**精确需求（不会因某条支路有余量就让下游缺水）；
- 每条管路流量为整数且在 `[最小量, 最大量]` 内；
- 可行方案先最小化**相对优选量的绝对偏差和**，再按**管路录入顺序的流量序列字典序**稳定决胜；
- 不可行时给出逐点收支诊断（缺水点 / 积压点、缺口水量、可读原因）。

## 时序复核（连续雾化时隙）

为避免管路行程和节点暂存被静态配平掩盖，可在同一草稿旁建立 **2~4 个连续雾化时隙**，
填写各时隙水源放水量、分区逐时隙精确需求、每条管路的整数行程时隙与每个分流节点的暂存上限。
服务端从完整草稿重建**时空网络**，联合求出每条管路每个时隙的整数发出量与节点库存：

- 水量只可在**行程时隙结束后**到达下游（时隙 t 发出、时隙 t+行程 到达）；
- 任一时隙发出量均在管路 `[最小量, 最大量]` 内，节点时隙末库存不超过暂存上限；
- 每时隙水源放出**恰等于**申报量、分区到水**恰等于**该时隙需求；
- 期末不得遗留库存或在途水量（行程超期末的时隙禁止发出）；
- 可行方案先最小化逐时隙相对优选量的总绝对偏差，再按**（时隙, 管路录入顺序）**
  展开的发出序列字典序稳定决胜；
- 无解时指出**首个无法满足的时隙**及对应分区、节点或管路原因
  （时序不可达 / 库存越界 / 收支不闭合）。

## 架构

| 组件 | 技术 | 说明 |
|---|---|---|
| `api` | Python 3.11 标准库（零第三方依赖） | 整数最小费用流求解 + HTTP API，端口 8000，`GET /healthz` 健康检查 |
| `web` | nginx + 原生静态页 | 录入草稿、发起配平/时序复核、展示逐管流量、节点收支与逐时隙发出/到达/库存台账；反代 `/api/` 到 api，`GET /healthz` 健康检查 |
| `verify` | 一次性容器 | 单元测试 → 字节码构建核查 → API 业务冒烟（含时序复核可行/时序不可达/库存越界场景）→ Web/API 联调冒烟，跑完即退出，**退出码即验收结论** |

## 快速开始

```bash
# 宿主机端口可用环境变量配置（默认 web 8080 / api 8000）
WEB_PORT=8081 API_PORT=9000 docker compose up --build

# 浏览器打开 http://localhost:8081
```

只跑一次性验收（测试 + 构建 + 冒烟），并用 verify 的退出码报告结果：

```bash
docker compose build
docker compose up \
  --abort-on-container-exit \
  --exit-code-from verify verify
echo "验收退出码：$?"   # 0 通过，非 0 失败
```

> `--exit-code-from verify` 使整条 compose 命令返回 verify 容器的退出码；
> verify 依赖 api、web 健康检查通过后才开始冒烟，结束后自行退出，
> api/web 仍可按需要常驻（`docker compose up`）或由调用方停止。

## 端口配置

`docker-compose.yml` 读取宿主机环境变量，均有默认值：

| 变量 | 默认 | 含义 |
|---|---|---|
| `WEB_PORT` | `8080` | Web 页面宿主机端口 |
| `API_PORT` | `8000` | API 宿主机端口（一般只需经 Web 反代访问） |

可复制 `.env.example` 为 `.env` 后调整（`docker compose` 自动读取）。

## HTTP API

### `POST /api/balance`

请求体：

```json
{
  "source": {"id": "S"},
  "source_total": 10,
  "zones": [{"id": "A", "demand": 6}, {"id": "B", "demand": 4}],
  "nodes": [{"id": "N"}],
  "pipes": [
    {"id": "p1", "from": "S", "to": "N", "min": 0, "max": 10, "preferred": 5},
    {"id": "p2", "from": "N", "to": "A", "min": 0, "max": 10, "preferred": 3},
    {"id": "p3", "from": "N", "to": "B", "min": 0, "max": 10, "preferred": 7},
    {"id": "p4", "from": "S", "to": "A", "min": 0, "max": 0,  "preferred": 0}
  ]
}
```

- 数量/连接方向/范围（`min ≤ preferred ≤ max`、非负整数）等校验失败返回 `400 {"error": ...}`。
- 校验通过返回 `200`，业务可行性由 `feasible` 表达：
  - 可行：`objective`（绝对偏差和）、`tie_sequence`（决胜流量序列）、
    `flows[]`（逐管流量与偏差）、`balances`（水源/节点/分区收支，守恒差额均为 0）；
  - 不可行：`infeasibility` 含总量与需求合计、已成立/缺口流量、
    `deficits`（进水不足点）、`surpluses`（来水积压点）与中文 `reasons`。

### `POST /api/schedule`

时序复核。请求体：

```json
{
  "source": {"id": "S"},
  "slots": 2,
  "source_dispatch": [6, 0],
  "zones": [{"id": "A", "demands": [0, 3]}, {"id": "B", "demands": [0, 3]}],
  "nodes": [{"id": "N", "capacity": 6}],
  "pipes": [
    {"id": "p1", "from": "S", "to": "N", "min": 0, "max": 10, "preferred": 6, "travel": 0},
    {"id": "p2", "from": "N", "to": "A", "min": 0, "max": 10, "preferred": 0, "travel": 0},
    {"id": "p3", "from": "N", "to": "B", "min": 0, "max": 10, "preferred": 0, "travel": 0},
    {"id": "p4", "from": "S", "to": "A", "min": 0, "max": 0,  "preferred": 0, "travel": 0}
  ]
}
```

- `slots`：时隙数（2~4）；`source_dispatch`：逐时隙水源放水量（恰等于）；
- 分区 `demands`：逐时隙精确需求；节点 `capacity`：暂存上限；
- 管路 `travel`：整数行程时隙（0 ~ slots），时隙 t 发出于 t+travel 到达，
  t+travel ≥ slots 的时隙禁止发出。
- 校验失败返回 `400 {"error": ...}`；校验通过返回 `200`，业务可行性由 `feasible` 表达：
  - 可行：`objective`（逐时隙绝对偏差和）、`tie_sequence`（按时隙优先展开的决胜发出序列）、
    `pipes[]`（逐管逐时隙发出 `dispatch` / 到达 `arrival` / 禁发时隙 `blocked_slots`）、
    `nodes[]`（逐时隙到达/发出/时隙末库存与上限）、
    `source` / `zones`（逐时隙收支对照，差额均为 0）；
  - 不可行：`infeasibility` 含 `first_slot`（首个无法满足的时隙）、
    放出/需求合计、`deficits`（进水不足点，带时隙）、`surpluses`
    （来水积压点，分流节点附 `inventory_used`/`capacity`）与中文 `reasons`
    （区分时序不可达、库存越界、收支不闭合）。

### `GET /healthz`

返回 `200 {"status":"ok","service":"balance-api"}`。

## 算法（app/）

- `mincost.py`：连续最短路增广（SPFA/Bellman-Ford）的整数最小费用流。
  整数容量按整数瓶颈增广，流量必为整数。
- `balance.py`：以**优选量为初始预流**，再用超源/超汇调整：
  - 增大边费用 `P+q`、减小边费用 `P-q`，全部为正，无负费用环；
  - 每偏离优选量 1 单位，费用的主目标分量恰为 `P`；
    净变化携带录入顺序权重 `q`，故最小费用
    ⇔ 先最小化绝对偏差和、再最小化录入顺序字典序；
  - 超源/超汇两侧必须同时饱和才可行（总量 ≠ 需求合计时两侧总量不等，必然不可行）。
- `schedule.py`：同一框架的**时空展开**版本：
  - 顶点为 (水源/分流节点/分区, 时隙)；管路调整边 `(u,t)→(v,t+行程)`；
  - 库存边 `(n,t)→(n,t+1)` 容量为暂存上限、费用 0，流量即该时隙末库存；
    不建出最后时隙的库存边，期末库存自然为 0；行程超期末的时隙不建发出边，
    期末在途自然为 0；
  - 决胜权重按（时隙, 管路录入顺序）分层，逻辑与静态版一致；
  - 不可行时由超源/超汇残余定位首个失败时隙及分区/节点/管路原因。
- 测试在随机小网络（静态与时空）上对**全量整数解暴力枚举**逐例对照
  可行性、最优目标值与字典序决胜序列
  （`tests/test_balance.py` 400 例、`tests/test_schedule.py` 120 例）。

## 本地开发（无需 Docker）

```bash
python3 -m app.server                 # 起 API：http://localhost:8000
python3 -m unittest discover -s tests # 跑测试
BASE_URL=http://127.0.0.1:8000 python3 smoke/smoke.py
```

## 目录

```
app/                 API 与求解器（标准库）：balance.py 静态配平、schedule.py 时序复核
web/index.html       前端单页（录入/配平/时序复核/结果与逐时隙台账展示）
nginx/default.conf   静态托管 + /api/ 反代 + Web 健康检查
tests/               unittest 单元/随机对照测试（静态 + 时空网络）
smoke/               API 冒烟、Web 联调、健康等待、verify 入口
Dockerfile           多阶段镜像：api / web / verify 三个 target（均含 HEALTHCHECK）
docker-compose.yml   api / web / verify 三服务
```
