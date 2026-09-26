# 纸本修复室 · 雾化管路配平系统

回湿脆化古画前，审核雾化管路能否把规定水量稳定送到每个分区：
**一处水源、2~4 个分区、0~4 个分流节点、4~10 条有向管路**，求整数流量方案。

- 水源流出**恰等于**水源总量；
- 每个分流节点流入 = 流出（不会凭空增减流量）；
- 每个分区流入**恰等于**精确需求（不会因某条支路有余量就让下游缺水）；
- 每条管路流量为整数且在 `[最小量, 最大量]` 内；
- 可行方案先最小化**相对优选量的绝对偏差和**，再按**管路录入顺序的流量序列字典序**稳定决胜；
- 不可行时给出逐点收支诊断（缺水点 / 积压点、缺口水量、可读原因）。

## 时序复核（/api/schedule）

静态配平之外，还可在同一管路草稿旁建立 **2~4 个连续雾化时隙**，避免管路行程
和节点暂存被静态结论掩盖。修复师额外填写：各时隙水源放水量、分区逐时隙精确
需求、每条管路的整数行程时隙、每个分流节点的暂存上限。服务端从完整草稿重建
**时空网络**，联合求出每条管路每个时隙的整数发出量与节点逐隙库存：

- 时隙 t 发出的水量于时隙 t+行程 到达下游，期末之后才能到达的时隙发出量
  恒为 0（这些时隙若最小量大于 0，则与"期末无在途"冲突，整案不可行）；
- 任一管路各时隙发出量仍在 `[最小量, 最大量]` 内，节点逐隙期末库存 ≤ 暂存上限；
- 每个时隙水源放出**恰等于**申报量、分区到水**恰等于**该隙需求；
- 期末不得遗留库存或在途水量；
- 可行方案先最小化各（时隙×管路）相对优选量的**总绝对偏差**，再按**时隙优先、
  同隙按管路录入顺序**的发出序列字典序稳定决胜；
- 不可行时指出**首个无法满足的时隙**及对应分区 / 节点 / 管路原因。

## 架构

| 组件 | 技术 | 说明 |
|---|---|---|
| `api` | Python 3.11 标准库（零第三方依赖） | 整数最小费用流求解 + HTTP API，端口 8000，`GET /healthz` 健康检查 |
| `web` | nginx + 原生静态页 | 录入草稿、发起配平与时序复核、展示逐管流量、节点收支与逐时隙台账；反代 `/api/` 到 api，`GET /healthz` 健康检查 |
| `verify` | 一次性容器 | 单元测试 → 字节码构建核查 → API 业务冒烟（含时序可行 / 时序不可达 / 库存越界场景）→ Web/API 联调冒烟，跑完即退出，**退出码即验收结论** |

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

在完整管路草稿上叠加时隙维度（数量/范围/连接校验同 `/api/balance`，
校验失败返回 `400 {"error": ...}`）：

```json
{
  "source": {"id": "S"},
  "horizon": 3,
  "source_release": [5, 5, 0],
  "zones": [{"id": "A", "demands": [0, 2, 4]}, {"id": "B", "demands": [0, 2, 2]}],
  "nodes": [{"id": "N", "capacity": 2}],
  "pipes": [
    {"id": "p1", "from": "S", "to": "N", "min": 0, "max": 10, "preferred": 5, "transit": 1},
    {"id": "p2", "from": "N", "to": "A", "min": 0, "max": 10, "preferred": 3, "transit": 0},
    {"id": "p3", "from": "N", "to": "B", "min": 0, "max": 10, "preferred": 7, "transit": 0},
    {"id": "p4", "from": "S", "to": "A", "min": 0, "max": 0,  "preferred": 0, "transit": 0}
  ]
}
```

- `horizon`：时隙数 2~4；`source_release` 长度等于 `horizon`；
- 分区 `demands`：逐时隙精确需求（长度等于 `horizon`）；
- 节点 `capacity`：暂存上限（逐隙期末库存不超过它）；
- 管路 `transit`：整数行程时隙 `0..horizon`（0 = 当期到达）。

校验通过返回 `200`，业务可行性由 `feasible` 表达：

- 可行：`objective`（总绝对偏差）、`tie_sequence`（时隙优先、同隙按管路
  录入顺序的决胜发出序列，含全部 时隙×管路 个位置，期末后才能到达的
  位置恒为 0）、`dispatch`（逐管逐隙发出量/到达时隙/偏差）、
  `inventory`（逐节点逐隙期末库存与上限）、`ledger`（逐时隙台账：
  水源放出、分区到水、节点库存守恒、逐管发出与到达、期末在途）；
- 不可行：`infeasibility` 含 `first_failure_slot`（首个无法满足的时隙）、
  放出/需求合计、已成立/缺口流量、带时隙标注的 `deficits`/`surpluses`
  与中文 `reasons`（指到具体分区、节点或管路原因）。

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
- `schedule.py`：把每个业务点按时隙展开为 `(点, 时隙)` 顶点，得到一张更大的
  静态网络后复用同一预流框架：
  - 管路 (u→v, 行程 τ) 在时隙 t 的发出量 = `(u,t)→(v,t+τ)` 的调整边
    （t+τ ≥ horizon 的时隙不建边，期末无在途）；
  - 节点暂存 = `(n,t)→(n,t+1)` 零费用边，容量即暂存上限；期末顶点无出边，
    守恒自然保证期末库存为 0；
  - 水源逐隙放出、分区逐隙需求为各顶点目标净流入；
  - 决胜权重按"时隙优先、同隙按录入顺序"分层，同 balance.py 的大整数递推。
- 测试在随机小网络上对**全量整数解暴力枚举**逐例对照
  可行性、最优目标值与字典序决胜序列（`tests/test_balance.py` 400 例、
  `tests/test_schedule.py` 300 例）。

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
tests/               unittest 单元/随机对照测试（静态配平 + 时隙时空网）
smoke/               API 冒烟、Web 联调、健康等待、verify 入口
Dockerfile           多阶段镜像：api / web / verify 三个 target（均含 HEALTHCHECK）
docker-compose.yml   api / web / verify 三服务
```
