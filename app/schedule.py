"""雾化时隙时序复核：在静态配平草稿上叠加行程时隙与节点暂存，求逐时隙调度。

业务约束
--------
- 时隙数（horizon）2~4 个，连续编号 0..H-1；
- 水源在时隙 t 的放出量**恰等于**申报 release[t]；
- 分区 z 在时隙 t 的到水**恰等于**该隙需求 demands[z][t]；
- 管路 p 在时隙 t 的发出量为整数且落在 [min, max]（优选量 preferred）；
  时隙 t 发出的水量于时隙 t+transit 到达下游；期末之后才能到达的
  （t+transit ≥ H）时隙发出量恒为 0——期末不得遗留在途水量；
  因此这些时隙若 min > 0，则"任一时隙发出量不得超出管路范围"与
  "期末无在途"直接冲突，整案不可行（逐管路逐时隙给出原因）；
- 分流节点 n 在时隙 t 末结转到 t+1 的库存不超过暂存上限 capacity[n]；
  期末（时隙 H-1 末）库存必须为 0；
- 分流节点逐时隙守恒：期初库存 + 当期到达 = 当期发出 + 期末库存。

目标
----
1. 主目标：所有（时隙, 管路）可用对的发出量相对优选量的绝对偏差和最小；
2. 决胜：按**时隙优先、同隙内按管路录入顺序**的发出量序列字典序最小
   （tie_sequence 含全部 horizon×管路数 个位置，期末前无法到达的
   时隙恒为 0，位置固定便于对照）。

建模
----
时空展开网络：每个业务点 v 每个时隙 t 一个顶点 (v,t)，即一张更大的
静态网络，随后完全复用 balance.py 的框架：
- 管路 (u→v, 行程 τ) 时隙 t 的发出量 = (u,t)→(v,t+τ) 的一对调整边
  （增大边费用 P+q、减小边费用 P-q，以优选量预流为基准）；
- 节点暂存 = (n,t)→(n,t+1) 的零费用边，容量即暂存上限（不预流、不计目标）；
- 目标净流入 r(v,t)：水源 -release[t]、分区 +demands[z][t]、分流节点 0；
- 超源/超汇两侧同时饱和才可行（放出合计 ≠ 需求合计时两侧总量不等，
  必然不可行）。

权重（整数大权，Python 原生大整数）：(t,i) 按时隙优先、同隙按录入顺序
排列，q 逆序递推 q_k = 1 + Σ_{j>k} q_j·(max_j-min_j)，P = 1 + Σ q·range，
保证先最小化绝对偏差和、再按该顺序字典序决胜。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .balance import (MAX_NODES, MAX_PIPES, MAX_ZONES, MIN_PIPES, MIN_ZONES,
                      ValidationError, _is_int, _require_int)
from .mincost import MinCostFlow

# 时隙数量约束
MIN_HORIZON, MAX_HORIZON = 2, 4


def validate_schedule(payload: Any) -> Dict[str, Any]:
    """校验时序复核草稿并整理为内部结构。"""
    if not isinstance(payload, dict):
        raise ValidationError("请求体必须是 JSON 对象")

    src = payload.get("source")
    if not isinstance(src, dict) or not str(src.get("id", "")).strip():
        raise ValidationError("必须指定一处水源（source.id）")
    source_id = str(src["id"]).strip()

    horizon = payload.get("horizon")
    if not _is_int(horizon):
        raise ValidationError("时隙数（horizon）必须是整数")
    if not (MIN_HORIZON <= horizon <= MAX_HORIZON):
        raise ValidationError(
            f"时隙数必须在 {MIN_HORIZON}~{MAX_HORIZON} 个之间")

    raw_release = payload.get("source_release")
    if not isinstance(raw_release, list):
        raise ValidationError("source_release 必须是数组")
    if len(raw_release) != horizon:
        raise ValidationError(
            f"水源逐时隙放出量（source_release）的长度必须等于时隙数 {horizon}")
    release: List[int] = []
    for t, x in enumerate(raw_release):
        if not _is_int(x) or x < 0:
            raise ValidationError(f"时隙 {t} 的水源放出量必须是非负整数")
        release.append(x)

    raw_zones = payload.get("zones")
    raw_nodes = payload.get("nodes", [])
    raw_pipes = payload.get("pipes")
    if not isinstance(raw_zones, list):
        raise ValidationError("zones 必须是数组")
    if not isinstance(raw_nodes, list):
        raise ValidationError("nodes 必须是数组")
    if not isinstance(raw_pipes, list):
        raise ValidationError("pipes 必须是数组")

    if not (MIN_ZONES <= len(raw_zones) <= MAX_ZONES):
        raise ValidationError(f"分区数量必须在 {MIN_ZONES}~{MAX_ZONES} 个之间")
    if len(raw_nodes) > MAX_NODES:
        raise ValidationError(f"分流节点数量不能超过 {MAX_NODES} 个")
    if not (MIN_PIPES <= len(raw_pipes) <= MAX_PIPES):
        raise ValidationError(f"管路数量必须在 {MIN_PIPES}~{MAX_PIPES} 条之间")

    seen: Dict[str, str] = {source_id: "水源"}

    def check_id(rid: Any, label: str) -> str:
        if not isinstance(rid, str) or not rid.strip():
            raise ValidationError(f"{label}的 id 不能为空")
        rid = rid.strip()
        if rid in seen:
            raise ValidationError(f"id 重复：{rid}（已被{seen[rid]}占用）")
        seen[rid] = label
        return rid

    zones: List[Dict[str, Any]] = []
    for z in raw_zones:
        if not isinstance(z, dict):
            raise ValidationError("每个分区必须是对象")
        zid = check_id(z.get("id"), "分区")
        demands = z.get("demands")
        if not isinstance(demands, list) or len(demands) != horizon:
            raise ValidationError(
                f"分区 {zid} 的逐时隙需求（demands）必须是长度 {horizon} 的数组")
        dz: List[int] = []
        for t, x in enumerate(demands):
            if not _is_int(x) or x < 0:
                raise ValidationError(
                    f"分区 {zid} 在时隙 {t} 的需求必须是非负整数")
            dz.append(x)
        zones.append({"id": zid, "demands": dz})

    nodes: List[str] = []
    capacity: Dict[str, int] = {}
    for nd in raw_nodes:
        if not isinstance(nd, dict):
            raise ValidationError("每个分流节点必须是对象")
        nid = check_id(nd.get("id"), "分流节点")
        capacity[nid] = _require_int(nd, "capacity", f"分流节点 {nid} 的暂存上限")
        nodes.append(nid)

    pipes: List[Dict[str, Any]] = []
    pipe_ids: Dict[str, str] = {}
    for i, p in enumerate(raw_pipes):
        label = f"第 {i + 1} 条管路"
        if not isinstance(p, dict):
            raise ValidationError(f"{label}必须是对象")
        pid = p.get("id")
        if not isinstance(pid, str) or not pid.strip():
            raise ValidationError(f"{label}缺少 id")
        pid = pid.strip()
        if pid in pipe_ids:
            raise ValidationError(f"管路 id 重复：{pid}")
        pipe_ids[pid] = label

        u = p.get("from")
        v = p.get("to")
        if not isinstance(u, str) or not isinstance(v, str):
            raise ValidationError(f"{label}（{pid}）必须指定起点 from 和终点 to")
        if u not in seen:
            raise ValidationError(f"{label}（{pid}）起点 {u} 不存在")
        if v not in seen:
            raise ValidationError(f"{label}（{pid}）终点 {v} 不存在")
        if seen[u] == "分区":
            raise ValidationError(f"{label}（{pid}）不能从分区 {u} 接出（分区只进水）")
        if seen[v] == "水源":
            raise ValidationError(f"{label}（{pid}）不能接入水源 {v}（水源只出水）")
        if u == v:
            raise ValidationError(f"{label}（{pid}）起点终点不能相同")

        lo = _require_int(p, "min", f"{label}（{pid}）最小量")
        hi = _require_int(p, "max", f"{label}（{pid}）最大量")
        pref = _require_int(p, "preferred", f"{label}（{pid}）优选量")
        if lo > hi:
            raise ValidationError(f"{label}（{pid}）最小量不能大于最大量")
        if not (lo <= pref <= hi):
            raise ValidationError(f"{label}（{pid}）优选量必须位于最小量与最大量之间")

        transit = p.get("transit")
        if not _is_int(transit):
            raise ValidationError(f"{label}（{pid}）行程时隙（transit）必须是整数")
        if not (0 <= transit <= horizon):
            raise ValidationError(
                f"{label}（{pid}）行程时隙必须在 0~{horizon} 之间")

        pipes.append({"id": pid, "from": u, "to": v,
                      "min": lo, "max": hi, "preferred": pref,
                      "transit": transit, "order": i})

    return {
        "source_id": source_id,
        "horizon": horizon,
        "release": release,
        "zones": zones,
        "nodes": nodes,
        "capacity": capacity,
        "pipes": pipes,
    }


def solve_schedule(payload: Any) -> Dict[str, Any]:
    """求时序复核结果。校验失败由调用方转 400。"""
    model = validate_schedule(payload)
    source_id: str = model["source_id"]
    horizon: int = model["horizon"]
    release: List[int] = model["release"]
    zones: List[Dict[str, Any]] = model["zones"]
    nodes: List[str] = model["nodes"]
    capacity: Dict[str, int] = model["capacity"]
    pipes: List[Dict[str, Any]] = model["pipes"]

    zone_ids = [z["id"] for z in zones]
    demands_of = {z["id"]: z["demands"] for z in zones}
    kind_of: Dict[str, str] = {source_id: "水源"}
    kind_of.update({n: "分流节点" for n in nodes})
    kind_of.update({z: "分区" for z in zone_ids})

    # ---- 顶点：(业务点, 时隙) 展开，再加 SS / TT ----
    business = [source_id] + nodes + zone_ids
    idx = {vid: i for i, vid in enumerate(business)}
    h = horizon
    n_biz = len(business)

    def vtx(vid: str, t: int) -> int:
        return idx[vid] * h + t

    ss, tt = n_biz * h, n_biz * h + 1
    mcf = MinCostFlow(tt + 1)

    # ---- 可用（时隙, 管路）对：期末前能到达才允许发出 ----
    m = len(pipes)
    pairs: List[Tuple[int, int]] = [
        (t, i) for t in range(h) for i in range(m)
        if t + pipes[i]["transit"] < h
    ]
    # 期末后才能到达的时隙发出量恒为 0；若这些时隙最小量 > 0，
    # 则"任一时隙发出量不得超出管路范围"与"期末无在途"直接冲突
    blocked: List[Tuple[int, int]] = [
        (t, i) for t in range(h) for i in range(m)
        if t + pipes[i]["transit"] >= h and pipes[i]["min"] > 0
    ]

    # 分层权重：时隙优先、同隙按录入顺序字典序决胜
    q: Dict[Tuple[int, int], int] = {}
    weight = 0
    for t, i in reversed(pairs):
        q[(t, i)] = weight + 1
        weight += q[(t, i)] * (pipes[i]["max"] - pipes[i]["min"])
    primary_p = weight + 1

    # ---- 优选量预流 + 调整边；b(v,t)=优选流入-优选流出 ----
    balance = [0] * (n_biz * h)
    adj_refs: Dict[Tuple[int, int], Tuple[int, int, int, int]] = {}
    for t, i in pairs:
        p = pipes[i]
        u = vtx(p["from"], t)
        v = vtx(p["to"], t + p["transit"])
        pref = p["preferred"]
        e_up = len(mcf.g[u])
        mcf.add_edge(u, v, p["max"] - pref, primary_p + q[(t, i)])
        e_down = len(mcf.g[v])
        mcf.add_edge(v, u, pref - p["min"], primary_p - q[(t, i)])
        adj_refs[(t, i)] = (u, e_up, v, e_down)
        balance[u] -= pref
        balance[v] += pref

    # ---- 节点暂存边 (n,t)→(n,t+1)：容量=暂存上限，费用 0 ----
    storage_refs: Dict[Tuple[str, int], Tuple[int, int]] = {}
    for n in nodes:
        for t in range(h - 1):
            u = vtx(n, t)
            ei = len(mcf.g[u])
            mcf.add_edge(u, vtx(n, t + 1), capacity[n], 0)
            storage_refs[(n, t)] = (u, ei)

    # ---- 目标净流入 r(v,t)：水源 -release、分区 +demand、分流节点 0 ----
    required_in = [0] * (n_biz * h)
    for t in range(h):
        required_in[vtx(source_id, t)] = -release[t]
        for zid in zone_ids:
            required_in[vtx(zid, t)] = demands_of[zid][t]

    # delta = b-r：>0 预流盈余 → SS→该点；<0 预流缺口 → 该点→TT
    ss_edges: List[Tuple[int, int]] = []   # (顶点序号, 边序号)
    tt_edges: List[Tuple[int, int]] = []
    ss_total = tt_total = 0
    for vi in range(n_biz * h):
        delta = balance[vi] - required_in[vi]
        if delta > 0:
            ei = len(mcf.g[ss])
            mcf.add_edge(ss, vi, delta, 0)
            ss_edges.append((vi, ei))
            ss_total += delta
        elif delta < 0:
            ei = len(mcf.g[vi])
            mcf.add_edge(vi, tt, -delta, 0)
            tt_edges.append((vi, ei))
            tt_total += -delta

    pushed, _cost = mcf.flow(ss, tt, max(ss_total, tt_total))

    saturated = all(mcf.g[ss][ei].cap == 0 for _, ei in ss_edges) and all(
        mcf.g[vi][ei].cap == 0 for vi, ei in tt_edges
    )

    base = {
        "source_id": source_id,
        "horizon": horizon,
        "release": release,
        "zones": zones,
        "nodes": [{"id": n, "capacity": capacity[n]} for n in nodes],
        "pipes": pipes,
    }

    if not saturated or pushed != min(ss_total, tt_total) or blocked:
        return _infeasible(base, mcf, ss, ss_edges, tt_edges,
                           ss_total, tt_total, pushed,
                           business, kind_of, demands_of, capacity, h,
                           blocked)

    # ---- 提取逐时隙发出量与库存 ----
    flows: Dict[Tuple[int, int], int] = {}
    for (t, i), (u, e_up, v, e_down) in adj_refs.items():
        p = pipes[i]
        flows[(t, i)] = (p["preferred"] + mcf.used_flow(u, e_up)
                         - mcf.used_flow(v, e_down))

    stock: Dict[Tuple[str, int], int] = {}
    for (n, t), (u, ei) in storage_refs.items():
        stock[(n, t)] = mcf.used_flow(u, ei)
    for n in nodes:
        stock[(n, h - 1)] = 0  # 期末不得遗留库存

    return _feasible(base, flows, stock, demands_of, h)


def _feasible(base: Dict[str, Any],
              flows: Dict[Tuple[int, int], int],
              stock: Dict[Tuple[str, int], int],
              demands_of: Dict[str, List[int]],
              h: int) -> Dict[str, Any]:
    pipes = base["pipes"]
    m = len(pipes)
    source_id = base["source_id"]
    release = base["release"]
    zone_ids = [z["id"] for z in base["zones"]]
    node_ids = [n["id"] for n in base["nodes"]]
    capacity = {n["id"]: n["capacity"] for n in base["nodes"]}

    def flow_at(t: int, i: int) -> int:
        return flows.get((t, i), 0)

    # 逐管逐时隙发出台账
    dispatch = []
    objective = 0
    for i, p in enumerate(pipes):
        slots = []
        for t in range(h):
            arrival = t + p["transit"]
            usable = arrival < h
            f = flow_at(t, i)
            dev = abs(f - p["preferred"]) if usable else 0
            objective += dev
            slots.append({
                "slot": t, "usable": usable, "flow": f,
                "arrival_slot": arrival if usable else None,
                "deviation": dev,
            })
        dispatch.append({
            "pipe_id": p["id"], "order": p["order"],
            "from": p["from"], "to": p["to"], "transit": p["transit"],
            "min": p["min"], "max": p["max"], "preferred": p["preferred"],
            "slots": slots,
        })

    # 节点库存台账
    inventory = [{
        "node_id": n, "capacity": capacity[n],
        "slots": [{"slot": t, "stock": stock[(n, t)]} for t in range(h)],
    } for n in node_ids]

    # 决胜序列：时隙优先、同隙按管路录入顺序（不可用位置恒 0）
    tie_sequence = [flow_at(t, i) for t in range(h) for i in range(m)]

    # 逐时隙台账：发出 / 到达 / 节点库存 / 分区到水 / 水源放出
    ledger = []
    for t in range(h):
        departures = []
        arrivals = []
        for i, p in enumerate(pipes):
            f = flow_at(t, i)
            if t + p["transit"] < h:
                departures.append({
                    "pipe_id": p["id"], "from": p["from"], "to": p["to"],
                    "amount": f, "arrival_slot": t + p["transit"],
                })
            t0 = t - p["transit"]
            if 0 <= t0 and t0 + p["transit"] < h:
                arrivals.append({
                    "pipe_id": p["id"], "from": p["from"], "to": p["to"],
                    "amount": flow_at(t0, i), "departed_slot": t0,
                })

        released = sum(d["amount"] for d in departures
                       if d["from"] == source_id)
        zone_rows = []
        for zid in zone_ids:
            arrived = sum(a["amount"] for a in arrivals if a["to"] == zid)
            zone_rows.append({
                "id": zid, "arrived": arrived,
                "demand": demands_of[zid][t],
                "difference": arrived - demands_of[zid][t],
            })
        node_rows = []
        for n in node_ids:
            arrived_n = sum(a["amount"] for a in arrivals if a["to"] == n)
            dispatched_n = sum(d["amount"] for d in departures
                               if d["from"] == n)
            node_rows.append({
                "id": n,
                "stock_start": stock[(n, t - 1)] if t > 0 else 0,
                "arrived": arrived_n,
                "dispatched": dispatched_n,
                "stock_end": stock[(n, t)],
                "capacity": capacity[n],
            })
        in_transit = sum(
            flow_at(t0, i)
            for i, p in enumerate(pipes)
            for t0 in range(t + 1)
            if t0 + p["transit"] < h and t0 <= t < t0 + p["transit"]
        )
        ledger.append({
            "slot": t,
            "source": {
                "id": source_id, "released": released,
                "declared": release[t],
                "difference": released - release[t],
            },
            "zones": zone_rows,
            "nodes": node_rows,
            "departures": departures,
            "arrivals": arrivals,
            "in_transit_end": in_transit,
        })

    return {
        "feasible": True,
        "horizon": h,
        "source_id": source_id,
        "source_release": release,
        "objective": objective,
        "tie_sequence": tie_sequence,
        "zones": base["zones"],
        "nodes": base["nodes"],
        "pipes": [{
            "id": p["id"],
            "order": p["order"], "from": p["from"], "to": p["to"],
            "min": p["min"], "max": p["max"],
            "preferred": p["preferred"], "transit": p["transit"],
        } for p in pipes],
        "dispatch": dispatch,
        "inventory": inventory,
        "ledger": ledger,
        "infeasibility": None,
    }


def _infeasible(base: Dict[str, Any], mcf: MinCostFlow, ss: int,
                ss_edges: List[Tuple[int, int]],
                tt_edges: List[Tuple[int, int]],
                ss_total: int, tt_total: int, pushed: int,
                business: List[str], kind_of: Dict[str, str],
                demands_of: Dict[str, List[int]],
                capacity: Dict[str, int],
                h: int,
                blocked: List[Tuple[int, int]]) -> Dict[str, Any]:
    release = base["release"]
    total_release = sum(release)
    total_demand = sum(sum(z["demands"]) for z in base["zones"])

    def locate(vi: int) -> Tuple[str, int]:
        return business[vi // h], vi % h

    # SS→v 残余：预流盈余（优选量）排不出去
    surpluses = []
    for vi, ei in ss_edges:
        remaining = mcf.g[ss][ei].cap
        if remaining > 0:
            vid, t = locate(vi)
            surpluses.append({
                "vertex": vid, "kind": kind_of[vid], "slot": t,
                "excess": remaining,
            })

    # v→TT 残余：目标净流入补不齐
    deficits = []
    for vi, ei in tt_edges:
        remaining = mcf.g[vi][ei].cap
        if remaining > 0:
            vid, t = locate(vi)
            deficits.append({
                "vertex": vid, "kind": kind_of[vid], "slot": t,
                "shortfall": remaining,
            })

    reasons: List[str] = []
    if total_release != total_demand:
        reasons.append(
            f"水源逐时隙放出合计 {total_release} 与分区需求合计 {total_demand} "
            "不相等，全期收支无法闭合（守恒网络中二者必须相等）")

    for d in deficits:
        kind, vid, t, amount = d["kind"], d["vertex"], d["slot"], d["shortfall"]
        if kind == "分区":
            reasons.append(
                f"时隙 {t}：分区 {vid}（该隙需求 {demands_of[vid][t]}）"
                f"至少还差 {amount} 单位到水：通向上游的管路容量不足、"
                "行程时隙赶不上该时隙，或途中分流节点暂存上限过紧")
        elif kind == "水源":
            reasons.append(
                f"时隙 {t}：水源 {vid} 出水管路的最小量之和超过该隙申报放出量 "
                f"{release[t]}，至少需再压低 {amount} 单位："
                "请调小相关管路最小量或上调该隙放出量")
        else:
            reasons.append(
                f"时隙 {t}：分流节点 {vid} 需再净收入 {amount} 单位才能收支相抵，"
                "但上游管路送不进来（容量不足或行程时隙赶不上）")

    for s in surpluses:
        kind, vid, t, amount = s["kind"], s["vertex"], s["slot"], s["excess"]
        if kind == "水源":
            reasons.append(
                f"时隙 {t}：水源 {vid} 有 {amount} 单位放出水量送不出去："
                "下游管路容量不足、行程时隙赶不上期末，"
                "或分流节点暂存上限过紧")
        elif kind == "分区":
            reasons.append(
                f"时隙 {t}：分区 {vid} 的到水压不到该隙需求 "
                f"{demands_of[vid][t]}：至少多出 {amount} 单位，"
                "请放宽进水管路的最小量")
        else:
            reasons.append(
                f"时隙 {t}：分流节点 {vid} 有 {amount} 单位来水无处排出"
                f"（暂存上限 {capacity[vid]}）：下游管路容量不足、"
                "行程时隙赶不上期末或暂存空间不够")

    pipes = base["pipes"]
    for t, i in blocked:
        p = pipes[i]
        reasons.append(
            f"时隙 {t}：管路 {p['id']}（{p['from']}→{p['to']}，"
            f"行程 {p['transit']} 隙）最小量为 {p['min']}，"
            "但该隙发出的水量期末之后才能到达，与期末不得遗留在途水量冲突："
            "请调小该管路最小量或缩短行程时隙")

    if not reasons:
        reasons.append("时空网络不满足全部守恒/范围/暂存约束，"
                       "请检查管路连接、容量、行程时隙与暂存上限")

    slots = ([d["slot"] for d in deficits]
             + [s["slot"] for s in surpluses]
             + [t for t, _ in blocked])
    first_failure_slot: Optional[int] = min(slots) if slots else None

    return {
        "feasible": False,
        "horizon": h,
        "source_id": base["source_id"],
        "objective": None,
        "tie_sequence": None,
        "dispatch": None,
        "inventory": None,
        "ledger": None,
        "infeasibility": {
            "first_failure_slot": first_failure_slot,
            "total_release": total_release,
            "total_demand": total_demand,
            "ss_required": ss_total,
            "tt_required": tt_total,
            "achieved_flow": pushed,
            "shortfall_flow": max(ss_total, tt_total) - pushed,
            "deficits": deficits,
            "surpluses": surpluses,
            "reasons": reasons,
        },
    }
