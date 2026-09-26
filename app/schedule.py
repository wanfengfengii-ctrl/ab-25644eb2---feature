"""时序复核：连续雾化时隙的时空网络整数配水。

业务约束
--------
- 在静态配平草稿旁补充 2~4 个连续雾化时隙：每时隙水源放水量、
  每分区逐时隙精确需求、每管路整数行程时隙、每分流节点暂存上限；
- 管路 i 在时隙 t 发出 x 单位，经行程 τ_i 个时隙后（时隙 t+τ_i）才到达下游；
  t+τ_i ≥ T 时该时隙禁止发出（期末不得遗留在途水量）；
- 每时隙水源放出恰等于申报量，每分区每时隙到水恰等于需求；
- 分流节点 n 时隙 t 末库存 ∈ [0, cap_n]，期初为 0、期末为 0；
- 每管路每时隙发出量为整数且落在 [min, max]。

目标
----
1. 主目标：各管路各时隙发出量相对优选量的绝对偏差和最小；
2. 决胜：按（时隙, 管路录入顺序）展开的发量序列字典序最小
   （序列稳定、可复现，不依赖求解器内部遍历顺序）。

建模
----
时空展开网络 + 优选量预流 + 超源/超汇调整（与 balance.py 同一框架，
全部调整边费用为正，无负费用环，连续最短路增广即得全局最小费用整数流）：
- 顶点 (v, t)：水源/分流节点/分区 × 时隙；
- 管路调整边 (u,t)→(v,t+τ)：增大边费用 P+q、减小边费用 P-q，
  每偏离优选量 1 单位主目标分量恰为 P，净变化携带决胜权重 q；
- 库存边 (n,t)→(n,t+1)：容量 cap、费用 0，流量即该时隙末库存；
  不建出最后时隙的库存边，期末库存自然为 0；
- 目标净流入 r(v,t)：水源 -supply[t]、分区 +demand[z][t]、分流节点 0；
- 超源/超汇两侧必须同时饱和才可行（放出合计 ≠ 需求合计时两侧总量
  不等，必然不可行）。

权重（整数大权，Python 原生大整数）：
  自由变量按（时隙, 管路录入顺序）排列，q_last = 1，
  q_k = 1 + Σ_{j>k} q_j·(max_j-min_j)（早位置字典序主导）；
  P = 1 + Σ_k q_k·(max_k-min_k)（1 单位主偏差压倒一切决胜差异）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from .balance import (MAX_NODES, MAX_PIPES, MAX_ZONES, MIN_PIPES, MIN_ZONES,
                      ValidationError, _is_int, _require_int)
from .mincost import MinCostFlow

# 时隙数量约束
MIN_SLOTS, MAX_SLOTS = 2, 4


def validate_schedule(payload: Any) -> Dict[str, Any]:
    """校验时序复核草稿并整理为内部结构。"""
    if not isinstance(payload, dict):
        raise ValidationError("请求体必须是 JSON 对象")

    src = payload.get("source")
    if not isinstance(src, dict) or not str(src.get("id", "")).strip():
        raise ValidationError("必须指定一处水源（source.id）")
    source_id = str(src["id"]).strip()

    slots = payload.get("slots")
    if not _is_int(slots):
        raise ValidationError("时隙数 slots 必须是整数")
    if not (MIN_SLOTS <= slots <= MAX_SLOTS):
        raise ValidationError(f"时隙数必须在 {MIN_SLOTS}~{MAX_SLOTS} 个之间")

    supply = payload.get("source_dispatch")
    if not isinstance(supply, list) or len(supply) != slots:
        raise ValidationError(
            f"source_dispatch 必须是长度 {slots} 的数组（逐时隙水源放水量）")
    for t, v in enumerate(supply):
        if not _is_int(v) or v < 0:
            raise ValidationError(f"时隙 {t} 的水源放水量必须是非负整数")

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

    def check_plan(arr: Any, label: str) -> List[int]:
        if not isinstance(arr, list) or len(arr) != slots:
            raise ValidationError(f"{label}必须是长度 {slots} 的数组（逐时隙）")
        for t, v in enumerate(arr):
            if not _is_int(v) or v < 0:
                raise ValidationError(f"{label}在时隙 {t} 的值必须是非负整数")
        return list(arr)

    zones: List[Dict[str, Any]] = []
    for z in raw_zones:
        if not isinstance(z, dict):
            raise ValidationError("每个分区必须是对象")
        zid = check_id(z.get("id"), "分区")
        zones.append({"id": zid,
                      "demands": check_plan(z.get("demands"),
                                            f"分区 {zid} 的逐时隙需求 demands")})

    nodes: List[Dict[str, Any]] = []
    for nd in raw_nodes:
        if not isinstance(nd, dict):
            raise ValidationError("每个分流节点必须是对象")
        nid = check_id(nd.get("id"), "分流节点")
        cap = _require_int(nd, "capacity", f"分流节点 {nid} 的暂存上限")
        nodes.append({"id": nid, "capacity": cap})

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

        travel = p.get("travel")
        if not _is_int(travel):
            raise ValidationError(f"{label}（{pid}）行程时隙 travel 必须是整数")
        if travel < 0:
            raise ValidationError(f"{label}（{pid}）行程时隙不能为负")
        if travel > slots:
            raise ValidationError(
                f"{label}（{pid}）行程时隙 {travel} 超过时隙总数 {slots}，"
                "任何时隙发出都无法在期末前到达")

        pipes.append({"id": pid, "from": u, "to": v, "min": lo, "max": hi,
                      "preferred": pref, "travel": travel, "order": i})

    return {
        "source_id": source_id,
        "slots": slots,
        "supply": list(supply),
        "zones": zones,
        "nodes": nodes,
        "pipes": pipes,
    }


def solve_schedule(payload: Any) -> Dict[str, Any]:
    """求时序复核结果。校验失败由调用方转 400。"""
    model = validate_schedule(payload)
    source_id: str = model["source_id"]
    slots: int = model["slots"]
    supply: List[int] = model["supply"]
    zones: List[Dict[str, Any]] = model["zones"]
    nodes: List[Dict[str, Any]] = model["nodes"]
    pipes: List[Dict[str, Any]] = model["pipes"]

    node_ids = [n["id"] for n in nodes]
    zone_ids = [z["id"] for z in zones]
    cap_of = {n["id"]: n["capacity"] for n in nodes}
    kind_of: Dict[str, str] = {source_id: "水源"}
    kind_of.update({n: "分流节点" for n in node_ids})
    kind_of.update({z: "分区" for z in zone_ids})

    # ---- 顶点：(业务点, 时隙) 分层，再加 SS / TT ----
    business = [source_id] + node_ids + zone_ids
    pos = {vid: k for k, vid in enumerate(business)}
    n_biz = len(business)
    ss, tt = slots * n_biz, slots * n_biz + 1
    mcf = MinCostFlow(tt + 1)
    m = len(pipes)

    def at(vid: str, t: int) -> int:
        return t * n_biz + pos[vid]

    # ---- 自由变量（时隙优先、时隙内按录入顺序）与分层权重 ----
    free_vars = [(t, i) for t in range(slots) for i in range(m)
                 if t + pipes[i]["travel"] < slots]
    fv_index = {k: idx for idx, k in enumerate(free_vars)}
    q = [0] * len(free_vars)
    weight = 0
    for k in range(len(free_vars) - 1, -1, -1):
        q[k] = weight + 1
        _, i = free_vars[k]
        weight += q[k] * (pipes[i]["max"] - pipes[i]["min"])
    primary_p = weight + 1

    # ---- 优选量预流 + 调整边；行程超期末的时隙禁止发出 ----
    balance = [[0] * n_biz for _ in range(slots)]
    adj_refs: Dict[Tuple[int, int], Tuple[int, int, int, int]] = {}
    blocked: List[Tuple[int, int]] = []
    for t in range(slots):
        for i, p in enumerate(pipes):
            tau = p["travel"]
            pref = p["preferred"]
            if t + tau < slots:
                u = at(p["from"], t)
                v = at(p["to"], t + tau)
                e_up = len(mcf.g[u])
                mcf.add_edge(u, v, p["max"] - pref, primary_p + q[fv_index[(t, i)]])
                e_down = len(mcf.g[v])
                mcf.add_edge(v, u, pref - p["min"], primary_p - q[fv_index[(t, i)]])
                adj_refs[(t, i)] = (u, e_up, v, e_down)
                balance[t][pos[p["from"]]] -= pref
                balance[t + tau][pos[p["to"]]] += pref
            else:
                blocked.append((t, i))

    # 禁止发出却受最小量迫使 → 结构性矛盾（时序不可达）
    conflicts = [(t, i) for (t, i) in blocked if pipes[i]["min"] > 0]
    if conflicts:
        first = min(t for t, _ in conflicts)
        reasons = []
        for t, i in sorted(conflicts):
            p = pipes[i]
            reasons.append(
                f"管路 {p['id']}（行程 {p['travel']} 时隙）在时隙 {t} 发出"
                f"将无法在期末前到达下游，期末不得遗留在途水量；但其最小量 "
                f"{p['min']} 迫使必须发出：请缩短行程或调小最小量（时序不可达）")
        return _infeasible_result(
            model, slots, first_slot=first, deficits=[], surpluses=[],
            reasons=reasons, ss_total=0, tt_total=0, pushed=0)

    # ---- 库存边（n,t)→(n,t+1)，流量即该时隙末库存 ----
    inv_refs: Dict[Tuple[str, int], Tuple[int, int]] = {}
    for nid in node_ids:
        for t in range(slots - 1):
            u = at(nid, t)
            ei = len(mcf.g[u])
            mcf.add_edge(u, at(nid, t + 1), cap_of[nid], 0)
            inv_refs[(nid, t)] = (u, ei)

    # ---- 目标净流入 r(v,t)：水源 -supply、分区 +demand、节点 0 ----
    required = [[0] * n_biz for _ in range(slots)]
    for t in range(slots):
        required[t][pos[source_id]] = -supply[t]
        for z in zones:
            required[t][pos[z["id"]]] = z["demands"][t]

    # delta = b-r：>0 预流盈余 → SS→v 注入送走；<0 预流缺口 → v→TT 补入
    ss_edges: List[Tuple[int, str, int]] = []
    tt_edges: List[Tuple[int, str, int]] = []
    ss_total = tt_total = 0
    for t in range(slots):
        for k, vid in enumerate(business):
            delta = balance[t][k] - required[t][k]
            if delta > 0:
                ei = len(mcf.g[ss])
                mcf.add_edge(ss, t * n_biz + k, delta, 0)
                ss_edges.append((t, vid, ei))
                ss_total += delta
            elif delta < 0:
                ei = len(mcf.g[t * n_biz + k])
                mcf.add_edge(t * n_biz + k, tt, -delta, 0)
                tt_edges.append((t, vid, ei))
                tt_total += -delta

    pushed, _cost = mcf.flow(ss, tt, max(ss_total, tt_total))

    saturated = all(mcf.g[ss][ei].cap == 0 for _, _, ei in ss_edges) and all(
        mcf.g[at(vid, t)][ei].cap == 0 for t, vid, ei in tt_edges)

    if not saturated or pushed != min(ss_total, tt_total):
        return _diagnose(model, mcf, ss, ss_edges, tt_edges,
                         ss_total, tt_total, pushed, kind_of,
                         cap_of, inv_refs, pos, n_biz)

    # ---- 读取每管路每时隙发出量 ----
    dispatch: List[List[int]] = []
    for i, p in enumerate(pipes):
        row = []
        for t in range(slots):
            ref = adj_refs.get((t, i))
            if ref is None:
                row.append(0)  # 行程超期末，禁止发出
            else:
                u, e_up, v, e_down = ref
                row.append(p["preferred"] + mcf.used_flow(u, e_up)
                           - mcf.used_flow(v, e_down))
        dispatch.append(row)

    return _feasible_result(model, mcf, dispatch, inv_refs)


def _feasible_result(model: Dict[str, Any], mcf: MinCostFlow,
                     dispatch: List[List[int]],
                     inv_refs: Dict[Tuple[str, int], Tuple[int, int]]
                     ) -> Dict[str, Any]:
    source_id = model["source_id"]
    slots = model["slots"]
    supply = model["supply"]
    zones = model["zones"]
    nodes = model["nodes"]
    pipes = model["pipes"]
    m = len(pipes)

    # 逐管：发出/到达台账
    pipe_rows = []
    objective = 0
    for i, p in enumerate(pipes):
        tau = p["travel"]
        disp = dispatch[i]
        arr = [0] * slots
        for t in range(slots):
            if t + tau < slots:
                arr[t + tau] = disp[t]
        dev = sum(abs(disp[t] - p["preferred"]) for t in range(slots))
        objective += dev
        pipe_rows.append({
            "pipe_id": p["id"], "order": p["order"],
            "from": p["from"], "to": p["to"], "travel": tau,
            "min": p["min"], "max": p["max"], "preferred": p["preferred"],
            "dispatch": disp, "arrival": arr,
            "blocked_slots": [t for t in range(slots) if t + tau >= slots],
            "deviation": dev,
        })

    def arrivals_into(vid: str) -> List[int]:
        acc = [0] * slots
        for row in pipe_rows:
            if row["to"] == vid:
                for t in range(slots):
                    acc[t] += row["arrival"][t]
        return acc

    def dispatches_from(vid: str) -> List[int]:
        acc = [0] * slots
        for row in pipe_rows:
            if row["from"] == vid:
                for t in range(slots):
                    acc[t] += row["dispatch"][t]
        return acc

    # 分流节点：逐时隙到达/发出/期末库存
    node_rows = []
    for n in nodes:
        nid = n["id"]
        inv = [0] * slots
        for t in range(slots - 1):
            u, ei = inv_refs[(nid, t)]
            inv[t] = mcf.used_flow(u, ei)
        # 最后时隙无库存边，期末库存恒为 0
        node_rows.append({
            "id": nid, "capacity": n["capacity"],
            "arrivals": arrivals_into(nid),
            "dispatches": dispatches_from(nid),
            "inventory": inv,
        })

    src_dispatch = dispatches_from(source_id)
    zone_rows = []
    for z in zones:
        arr = arrivals_into(z["id"])
        zone_rows.append({
            "id": z["id"], "demands": z["demands"], "arrivals": arr,
            "difference": [arr[t] - z["demands"][t] for t in range(slots)],
        })

    # 决胜序列：按（时隙, 管路录入顺序）展开
    tie_sequence = [dispatch[i][t] for t in range(slots) for i in range(m)]

    return {
        "feasible": True,
        "objective": objective,
        "tie_sequence": tie_sequence,
        "slots": slots,
        "source": {
            "id": source_id, "declared": supply, "dispatch": src_dispatch,
            "difference": [src_dispatch[t] - supply[t] for t in range(slots)],
        },
        "pipes": pipe_rows,
        "nodes": node_rows,
        "zones": zone_rows,
        "infeasibility": None,
    }


def _diagnose(model: Dict[str, Any], mcf: MinCostFlow, ss: int,
              ss_edges: List[Tuple[int, str, int]],
              tt_edges: List[Tuple[int, str, int]],
              ss_total: int, tt_total: int, pushed: int,
              kind_of: Dict[str, str], cap_of: Dict[str, int],
              inv_refs: Dict[Tuple[str, int], Tuple[int, int]],
              pos: Dict[str, int], n_biz: int) -> Dict[str, Any]:
    """由超源/超汇残余定位首个无法满足的时隙并生成可读原因。"""
    slots = model["slots"]
    supply = model["supply"]
    zones = model["zones"]
    demand_of = {z["id"]: z["demands"] for z in zones}

    # v→TT 残余：目标净流入补不齐（进水不足）
    deficits = []
    for t, vid, ei in tt_edges:
        remaining = mcf.g[t * n_biz + pos[vid]][ei].cap
        if remaining > 0:
            deficits.append({"slot": t, "vertex": vid, "kind": kind_of[vid],
                             "shortfall": remaining})

    # SS→v 残余：预流盈余排不出去（来水积压）
    surpluses = []
    for t, vid, ei in ss_edges:
        remaining = mcf.g[ss][ei].cap
        if remaining > 0:
            entry: Dict[str, Any] = {"slot": t, "vertex": vid,
                                     "kind": kind_of[vid], "excess": remaining}
            if kind_of[vid] == "分流节点":
                used = 0
                if t < slots - 1:
                    u, ie = inv_refs[(vid, t)]
                    used = mcf.used_flow(u, ie)
                entry["inventory_used"] = used
                entry["capacity"] = cap_of[vid]
            surpluses.append(entry)

    reasons: List[str] = []
    total_supply = sum(supply)
    total_demand = sum(z["demands"][t] for z in zones for t in range(slots))
    if total_supply != total_demand:
        reasons.append(
            f"各时隙水源放出合计 {total_supply} 与分区需求合计 {total_demand} "
            "不相等：期末不得遗留库存或在途水量，全系统收支无法闭合"
            "（二者必须相等）")

    for d in sorted(deficits, key=lambda x: x["slot"]):
        t, kind, vid, amount = d["slot"], d["kind"], d["vertex"], d["shortfall"]
        if kind == "分区":
            reasons.append(
                f"时隙 {t} 分区 {vid}（需求 {demand_of[vid][t]}）到水至少缺 "
                f"{amount} 单位：上游管路行程过长或容量不足，水量无法在该时隙"
                "结束前送达（时序不可达）")
        elif kind == "水源":
            reasons.append(
                f"时隙 {t} 水源 {vid} 出水管路的最小量之和已超过申报放水量 "
                f"{supply[t]}，至少需再压低 {amount} 单位：请调小相关管路"
                "最小量或上调该时隙放水量")
        else:
            reasons.append(
                f"时隙 {t} 分流节点 {vid} 需再净收入 {amount} 单位，"
                "但上游水量无法按时送达（行程过长或容量不足）")

    for s in sorted(surpluses, key=lambda x: x["slot"]):
        t, kind, vid, amount = s["slot"], s["kind"], s["vertex"], s["excess"]
        if kind == "水源":
            reasons.append(
                f"时隙 {t} 水源 {vid} 申报放出 {supply[t]}，但有 {amount} "
                "单位送不出去：出水管路容量不足或行程过长")
        elif kind == "分区":
            reasons.append(
                f"时隙 {t} 分区 {vid} 的到水压不到需求 {demand_of[vid][t]}："
                f"至少多出 {amount} 单位，请调小进水管路最小量")
        else:
            cap = s["capacity"]
            if t == slots - 1:
                reasons.append(
                    f"时隙 {t} 分流节点 {vid} 有 {amount} 单位来水无处排出："
                    "已是最后时隙，期末不得遗留库存，且下游管路容量不足"
                    "或行程过长（库存越界）")
            elif s["inventory_used"] >= cap:
                reasons.append(
                    f"时隙 {t} 分流节点 {vid} 暂存已达上限 {cap}，仍有 "
                    f"{amount} 单位来水无处暂存或排出（库存越界）："
                    "请上调暂存上限或增大下游管路容量")
            else:
                reasons.append(
                    f"时隙 {t} 分流节点 {vid} 有 {amount} 单位来水无处排出："
                    "下游管路容量不足或行程过长")

    if not reasons:
        reasons.append("时空网络不满足全部守恒/范围/库存约束，"
                       "请检查管路行程、容量与暂存上限")

    first_slot = min([d["slot"] for d in deficits]
                     + [s["slot"] for s in surpluses], default=None)
    return _infeasible_result(model, slots, first_slot, deficits, surpluses,
                              reasons, ss_total, tt_total, pushed)


def _infeasible_result(model: Dict[str, Any], slots: int,
                       first_slot: Any, deficits: List[Dict[str, Any]],
                       surpluses: List[Dict[str, Any]], reasons: List[str],
                       ss_total: int, tt_total: int, pushed: int
                       ) -> Dict[str, Any]:
    supply = model["supply"]
    zones = model["zones"]
    total_supply = sum(supply)
    total_demand = sum(z["demands"][t] for z in zones for t in range(slots))
    return {
        "feasible": False,
        "objective": None,
        "tie_sequence": None,
        "slots": slots,
        "source": None,
        "pipes": None,
        "nodes": None,
        "zones": None,
        "infeasibility": {
            "first_slot": first_slot,
            "total_supply": total_supply,
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
