"""时序复核求解器单元测试（标准库 unittest，无需第三方依赖）。"""

import itertools
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.balance import ValidationError  # noqa: E402
from app.schedule import solve_schedule, validate_schedule  # noqa: E402


def pipe(pid, u, v, lo, hi, pref, travel):
    return {"id": pid, "from": u, "to": v, "min": lo, "max": hi,
            "preferred": pref, "travel": travel}


def base_payload():
    """两时隙库存承载范例：时隙 0 放 6 存于 N，时隙 1 分送 A/B。"""
    return {
        "source": {"id": "S"},
        "slots": 2,
        "source_dispatch": [6, 0],
        "zones": [{"id": "A", "demands": [0, 3]},
                  {"id": "B", "demands": [0, 3]}],
        "nodes": [{"id": "N", "capacity": 6}],
        "pipes": [
            pipe("p1", "S", "N", 0, 10, 6, 0),
            pipe("p2", "N", "A", 0, 10, 0, 0),
            pipe("p3", "N", "B", 0, 10, 0, 0),
            pipe("p4", "S", "A", 0, 0, 0, 0),
        ],
    }


class TestScheduleBasic(unittest.TestCase):
    def test_inventory_carryover_feasible(self):
        r = solve_schedule(base_payload())
        self.assertTrue(r["feasible"])
        # 优选量按每时隙计：p1 时隙1 偏 6，p2/p3 时隙1 各偏 3
        self.assertEqual(r["objective"], 12)
        self.assertEqual(r["tie_sequence"], [6, 0, 0, 0, 0, 3, 3, 0])
        # 逐管台账：p1 时隙0 发出 6，p2/p3 时隙1 各发 3
        disp = {row["pipe_id"]: row["dispatch"] for row in r["pipes"]}
        self.assertEqual(disp["p1"], [6, 0])
        self.assertEqual(disp["p2"], [0, 3])
        self.assertEqual(disp["p3"], [0, 3])
        # 节点库存：时隙0 末存 6，期末清零
        node = r["nodes"][0]
        self.assertEqual(node["inventory"], [6, 0])
        self.assertEqual(node["arrivals"], [6, 0])
        self.assertEqual(node["dispatches"], [0, 6])
        # 水源/分区逐时隙收支闭合
        self.assertEqual(r["source"]["difference"], [0, 0])
        for zrow in r["zones"]:
            self.assertEqual(zrow["difference"], [0, 0])

    def test_travel_delays_arrival(self):
        # 行程 1 时隙：时隙 0 发出的水时隙 1 才到达
        payload = {
            "source": {"id": "S"}, "slots": 3,
            "source_dispatch": [2, 0, 0],
            "zones": [{"id": "A", "demands": [0, 0, 2]},
                      {"id": "B", "demands": [0, 0, 0]}],
            "nodes": [{"id": "N", "capacity": 2}],
            "pipes": [
                pipe("p1", "S", "N", 0, 5, 2, 1),   # 时隙0发 → 时隙1到 N
                pipe("p2", "N", "A", 0, 5, 2, 1),   # 时隙1发 → 时隙2到 A
                pipe("p3", "N", "B", 0, 5, 0, 1),
                pipe("p4", "S", "B", 0, 0, 0, 0),
            ],
        }
        r = solve_schedule(payload)
        self.assertTrue(r["feasible"])
        rows = {row["pipe_id"]: row for row in r["pipes"]}
        self.assertEqual(rows["p1"]["dispatch"], [2, 0, 0])
        self.assertEqual(rows["p1"]["arrival"], [0, 2, 0])
        self.assertEqual(rows["p2"]["dispatch"], [0, 2, 0])
        self.assertEqual(rows["p2"]["arrival"], [0, 0, 2])
        # 水在 N 不在库存过夜（时隙1 即到即转）
        self.assertEqual(r["nodes"][0]["inventory"], [0, 0, 0])

    def test_inventory_overflow_infeasible(self):
        # 暂存上限 4 < 需暂存 6：库存越界，首个失败时隙为 0
        payload = base_payload()
        payload["nodes"][0]["capacity"] = 4
        r = solve_schedule(payload)
        self.assertFalse(r["feasible"])
        info = r["infeasibility"]
        self.assertEqual(info["first_slot"], 0)
        sur = [s for s in info["surpluses"] if s["vertex"] == "N"]
        self.assertTrue(sur)
        self.assertEqual(sur[0]["slot"], 0)
        self.assertEqual(sur[0]["inventory_used"], 4)
        self.assertEqual(sur[0]["capacity"], 4)
        self.assertTrue(any("暂存已达上限" in t for t in info["reasons"]))
        self.assertTrue(any("库存越界" in t for t in info["reasons"]))

    def test_temporal_unreachable_infeasible(self):
        # 行程 2 时隙，需求在时隙 1：水来不及送达（时序不可达）
        payload = {
            "source": {"id": "S"}, "slots": 3,
            "source_dispatch": [6, 0, 0],
            "zones": [{"id": "A", "demands": [0, 3, 0]},
                      {"id": "B", "demands": [0, 3, 0]}],
            "nodes": [{"id": "N", "capacity": 10}],
            "pipes": [
                pipe("p1", "S", "N", 0, 10, 6, 2),  # 时隙0发 → 时隙2才到 N
                pipe("p2", "N", "A", 0, 10, 0, 0),
                pipe("p3", "N", "B", 0, 10, 0, 0),
                pipe("p4", "S", "A", 0, 0, 0, 0),
            ],
        }
        r = solve_schedule(payload)
        self.assertFalse(r["feasible"])
        info = r["infeasibility"]
        self.assertEqual(info["first_slot"], 1)
        lacking = {(d["slot"], d["vertex"]) for d in info["deficits"]}
        self.assertIn((1, "A"), lacking)
        self.assertIn((1, "B"), lacking)
        self.assertTrue(any("时序不可达" in t for t in info["reasons"]))

    def test_blocked_pipe_with_min_conflict(self):
        # 行程 ≥ 时隙数且最小量 > 0：结构性矛盾
        payload = base_payload()
        payload["pipes"][0] = pipe("p1", "S", "N", 1, 10, 6, 2)  # travel=2=T
        r = solve_schedule(payload)
        self.assertFalse(r["feasible"])
        info = r["infeasibility"]
        self.assertEqual(info["first_slot"], 0)
        self.assertTrue(any("期末前到达" in t for t in info["reasons"]))
        self.assertTrue(any("p1" in t for t in info["reasons"]))

    def test_total_mismatch_infeasible(self):
        payload = base_payload()
        payload["source_dispatch"] = [5, 0]  # 合计 5 ≠ 需求合计 6
        r = solve_schedule(payload)
        self.assertFalse(r["feasible"])
        self.assertTrue(any("不相等" in t
                            for t in r["infeasibility"]["reasons"]))

    def test_lexicographic_tie_break_slot_major(self):
        # 每时隙 p2+p3=2，(0,2)/(1,1)/(2,0) 偏差同为 2；
        # 按（时隙, 录入顺序）字典序取 p2 先小：两时隙均为 (0, 2)
        payload = {
            "source": {"id": "S"}, "slots": 2,
            "source_dispatch": [2, 2],
            "zones": [{"id": "A", "demands": [2, 2]},
                      {"id": "B", "demands": [0, 0]}],
            "nodes": [{"id": "N", "capacity": 0}],
            "pipes": [
                pipe("p1", "S", "N", 2, 2, 2, 0),
                pipe("p2", "N", "A", 0, 4, 2, 0),
                pipe("p3", "N", "A", 0, 4, 2, 0),
                pipe("p4", "N", "B", 0, 0, 0, 0),
            ],
        }
        r = solve_schedule(payload)
        self.assertTrue(r["feasible"])
        self.assertEqual(r["tie_sequence"], [2, 0, 2, 0, 2, 0, 2, 0])
        self.assertEqual(r["objective"], 4)


class TestScheduleValidation(unittest.TestCase):
    def test_slots_range_and_type(self):
        p = base_payload()
        p["slots"] = 1
        with self.assertRaises(ValidationError):
            validate_schedule(p)
        p = base_payload()
        p["slots"] = 5
        with self.assertRaises(ValidationError):
            validate_schedule(p)
        p = base_payload()
        p["slots"] = 2.0
        with self.assertRaises(ValidationError):
            validate_schedule(p)

    def test_plan_lengths(self):
        p = base_payload()
        p["source_dispatch"] = [6]
        with self.assertRaises(ValidationError):
            validate_schedule(p)
        p = base_payload()
        p["zones"][0]["demands"] = [0, 3, 0]
        with self.assertRaises(ValidationError):
            validate_schedule(p)
        p = base_payload()
        p["source_dispatch"] = [6, -1]
        with self.assertRaises(ValidationError):
            validate_schedule(p)

    def test_capacity_and_travel(self):
        p = base_payload()
        p["nodes"][0]["capacity"] = -1
        with self.assertRaises(ValidationError):
            validate_schedule(p)
        p = base_payload()
        p["pipes"][0]["travel"] = -1
        with self.assertRaises(ValidationError):
            validate_schedule(p)
        p = base_payload()
        p["pipes"][0]["travel"] = 3  # > slots=2
        with self.assertRaises(ValidationError):
            validate_schedule(p)
        p = base_payload()
        p["pipes"][0]["travel"] = 1.5
        with self.assertRaises(ValidationError):
            validate_schedule(p)

    def test_structure_and_ranges(self):
        p = base_payload()
        p["pipes"][1]["to"] = "GHOST"
        with self.assertRaises(ValidationError):
            validate_schedule(p)
        p = base_payload()
        p["pipes"][1]["from"] = "A"  # 分区不能出水
        with self.assertRaises(ValidationError):
            validate_schedule(p)
        p = base_payload()
        p["pipes"][0]["min"] = 8
        p["pipes"][0]["preferred"] = 9
        p["pipes"][0]["max"] = 7
        with self.assertRaises(ValidationError):
            validate_schedule(p)
        p = base_payload()
        p["zones"][0]["id"] = "N"  # 与节点重名
        with self.assertRaises(ValidationError):
            validate_schedule(p)


def _brute_force_schedule(payload):
    """枚举所有管路所有时隙的整数发出量，返回 [(seq, objective)...]。"""
    model = validate_schedule(payload)
    slots = model["slots"]
    pipes = model["pipes"]
    m = len(pipes)
    supply = model["supply"]
    zones = model["zones"]
    nodes = model["nodes"]
    source_id = model["source_id"]

    free = [(t, i) for t in range(slots) for i in range(m)
            if t + pipes[i]["travel"] < slots]
    # 行程超期末却受最小量迫使 → 结构性无可行解
    for t in range(slots):
        for i in range(m):
            if t + pipes[i]["travel"] >= slots and pipes[i]["min"] > 0:
                return []

    spans = [range(pipes[i]["min"], pipes[i]["max"] + 1) for (_, i) in free]
    sols = []
    for vals in itertools.product(*spans):
        x = [[0] * m for _ in range(slots)]
        for (t, i), v in zip(free, vals):
            x[t][i] = v

        ok = True
        for t in range(slots):
            if sum(x[t][i] for i in range(m)
                   if pipes[i]["from"] == source_id) != supply[t]:
                ok = False
                break
        if not ok:
            continue

        for z in zones:
            zid = z["id"]
            for t in range(slots):
                got = 0
                for i in range(m):
                    if pipes[i]["to"] == zid:
                        t0 = t - pipes[i]["travel"]
                        if t0 >= 0:
                            got += x[t0][i]
                if got != z["demands"][t]:
                    ok = False
                    break
            if not ok:
                break
        if not ok:
            continue

        for n in nodes:
            nid, cap = n["id"], n["capacity"]
            inv = 0
            for t in range(slots):
                got = 0
                for i in range(m):
                    if pipes[i]["to"] == nid:
                        t0 = t - pipes[i]["travel"]
                        if t0 >= 0:
                            got += x[t0][i]
                sent = sum(x[t][i] for i in range(m)
                           if pipes[i]["from"] == nid)
                inv += got - sent
                if inv < 0 or inv > cap:
                    ok = False
                    break
            if inv != 0:  # 期末不得遗留库存
                ok = False
            if not ok:
                break
        if not ok:
            continue

        obj = sum(abs(x[t][i] - pipes[i]["preferred"])
                  for t in range(slots) for i in range(m))
        seq = tuple(x[t][i] for t in range(slots) for i in range(m))
        sols.append((seq, obj))
    return sols


def _split(amount, k, rng):
    """把 amount 随机切成 k 份非负整数（均匀 compositions）。"""
    if k == 1:
        return [amount]
    cuts = sorted(rng.randrange(0, amount + 1) for _ in range(k - 1))
    parts = []
    prev = 0
    for c in cuts:
        parts.append(c - prev)
        prev = c
    parts.append(amount - prev)
    return parts


def _random_feasible_schedule(rng):
    """正向模拟一组合法时空流，再反推申报量/需求/范围，保证可行。"""
    slots = rng.choice([2, 2, 3])
    n_nodes = rng.randrange(0, 3)
    n_zones = rng.randrange(2, 4)
    nodes = [f"N{i}" for i in range(n_nodes)]
    zones = [f"Z{i}" for i in range(n_zones)]

    # 无环分层网络：S→节点/分区；N_i→N_j(j>i)/分区
    edges = []
    for i, ni in enumerate(nodes):
        edges.append(("S", ni))
        targets = [f"N{j}" for j in range(i + 1, n_nodes)] + zones
        edges.append((ni, rng.choice(targets)))
    edges.append(("S", rng.choice(zones)))
    candidates = [("S", t) for t in nodes + zones]
    for i, ni in enumerate(nodes):
        for t in [f"N{j}" for j in range(i + 1, n_nodes)] + zones:
            candidates.append((ni, t))
    rng.shuffle(candidates)
    target_n = rng.randrange(4, 7)
    for c in candidates:
        if len(edges) >= target_n:
            break
        edges.append(c)  # 允许并联：同端点可有多条不同管路
    while len(edges) < 4:
        edges.append(rng.choice(candidates))
    edges = edges[:6]
    m = len(edges)

    taus = [rng.randrange(0, slots) for _ in edges]
    caps = {n: rng.randrange(0, 4) for n in nodes}
    out_e = {}
    for i, (u, v) in enumerate(edges):
        out_e.setdefault(u, []).append(i)

    for _attempt in range(80):
        x = [[0] * m for _ in range(slots)]
        arr = {v: [0] * slots for v in nodes + zones}
        inv = {n: 0 for n in nodes}
        supply = [rng.randrange(0, 4) for _ in range(slots)]
        ok = True
        for t in range(slots):
            outs = [i for i in out_e.get("S", []) if t + taus[i] < slots]
            if supply[t] > 0 and not outs:
                ok = False
                break
            for i, val in zip(outs, _split(supply[t], len(outs), rng)):
                x[t][i] = val
                arr[edges[i][1]][t + taus[i]] += val
            for n in nodes:  # 拓扑序即编号序
                avail = inv[n] + arr[n][t]
                outs_n = [i for i in out_e.get(n, [])
                          if t + taus[i] < slots]
                can_store = caps[n] if t < slots - 1 else 0
                if not outs_n:
                    if avail > can_store:
                        ok = False
                        break
                    inv[n] = avail
                    continue
                store = rng.randrange(0, min(can_store, avail) + 1)
                for i, val in zip(outs_n,
                                  _split(avail - store, len(outs_n), rng)):
                    x[t][i] = val
                    arr[edges[i][1]][t + taus[i]] += val
                inv[n] = store
            if not ok:
                break
        if not ok:
            continue

        pipes = []
        for i, (u, v) in enumerate(edges):
            xs = [x[t][i] for t in range(slots) if t + taus[i] < slots]
            has_blocked = any(t + taus[i] >= slots for t in range(slots))
            lo = 0 if has_blocked else rng.randrange(0, min(xs) + 1)
            hi = max(xs) + rng.randrange(0, 2)
            if hi - lo > 2:
                hi = lo + 2  # 控制暴力枚举规模
            pref = rng.randrange(lo, hi + 1)
            pipes.append(pipe(f"e{i}", u, v, lo, hi, pref, taus[i]))

        payload = {
            "source": {"id": "S"}, "slots": slots, "source_dispatch": supply,
            "zones": [{"id": z, "demands": arr[z]} for z in zones],
            "nodes": [{"id": n, "capacity": caps[n]} for n in nodes],
            "pipes": pipes,
        }
        combos = 1
        for t in range(slots):
            for i in range(m):
                if t + taus[i] < slots:
                    combos *= pipes[i]["max"] - pipes[i]["min"] + 1
        if combos > 20000:
            continue
        return payload
    return None


def _random_loose_schedule(rng):
    """完全松散的随机小时空网络，大概率不可行。"""
    slots = rng.choice([2, 3])
    n_nodes = rng.randrange(0, 3)
    n_zones = rng.randrange(2, 4)
    nodes = [f"N{i}" for i in range(n_nodes)]
    zones = [f"Z{i}" for i in range(n_zones)]
    candidates = [(u, v) for u in ["S"] + nodes for v in nodes + zones
                  if u != v]
    m = rng.randrange(4, 7)
    pipes = []
    for i in range(m):
        u, v = rng.choice(candidates)
        lo = rng.randrange(0, 2)
        hi = lo + rng.randrange(0, 3)
        if hi - lo > 2:
            hi = lo + 2
        pref = rng.randrange(lo, hi + 1)
        travel = rng.randrange(0, slots + 1)  # 可能 == slots（全程在途）
        pipes.append(pipe(f"e{i}", u, v, lo, hi, pref, travel))
    return {
        "source": {"id": "S"}, "slots": slots,
        "source_dispatch": [rng.randrange(0, 4) for _ in range(slots)],
        "zones": [{"id": z,
                   "demands": [rng.randrange(0, 4) for _ in range(slots)]}
                  for z in zones],
        "nodes": [{"id": n, "capacity": rng.randrange(0, 3)} for n in nodes],
        "pipes": pipes,
    }


class TestScheduleRandomAgainstBruteForce(unittest.TestCase):
    """随机小时空网络上与暴力枚举逐例对照：可行性、目标值、决胜序列。"""

    def test_random_schedules(self):
        rng = random.Random(20260926)
        cases = 0
        feasible_checked = 0
        infeasible_checked = 0
        attempts = 0
        while cases < 120 and attempts < 6000:
            attempts += 1
            if rng.random() < 0.65:
                payload = _random_feasible_schedule(rng)
                if payload is None:
                    continue
            else:
                payload = _random_loose_schedule(rng)
            cases += 1

            brute = _brute_force_schedule(payload)
            r = solve_schedule(payload)
            if not brute:
                self.assertFalse(r["feasible"], msg=str(payload))
                infeasible_checked += 1
                continue

            self.assertTrue(r["feasible"], msg=str(payload))
            best_obj = min(o for _, o in brute)
            self.assertEqual(r["objective"], best_obj, msg=str(payload))
            best_seq = min(s for s, o in brute if o == best_obj)
            self.assertEqual(tuple(r["tie_sequence"]), best_seq,
                             msg=str(payload))

            # 台账自洽：逐时隙收支、库存平衡与容量、行程到达
            slots = payload["slots"]
            self.assertTrue(all(d == 0 for d in r["source"]["difference"]))
            for zrow in r["zones"]:
                self.assertTrue(all(d == 0 for d in zrow["difference"]))
            caps = {n["id"]: n["capacity"] for n in payload["nodes"]}
            for nrow in r["nodes"]:
                prev = 0
                for t in range(slots):
                    cur = nrow["inventory"][t]
                    self.assertEqual(
                        cur, prev + nrow["arrivals"][t] - nrow["dispatches"][t],
                        msg=str(payload))
                    self.assertGreaterEqual(cur, 0)
                    self.assertLessEqual(cur, caps[nrow["id"]])
                    prev = cur
                self.assertEqual(nrow["inventory"][slots - 1], 0,
                                 msg="期末不得遗留库存")
            disp_rows = []
            for prow in r["pipes"]:
                for t in range(slots):
                    d = prow["dispatch"][t]
                    if t in prow["blocked_slots"]:
                        self.assertEqual(d, 0, msg="在途超期末时隙禁止发出")
                    else:
                        self.assertGreaterEqual(d, prow["min"])
                        self.assertLessEqual(d, prow["max"])
                        self.assertEqual(
                            prow["arrival"][t + prow["travel"]], d)
                disp_rows.append(prow["dispatch"])
            # 决胜序列按（时隙, 管路录入顺序）展开
            expanded = [disp_rows[i][t] for t in range(slots)
                        for i in range(len(disp_rows))]
            self.assertEqual(expanded, r["tie_sequence"])
            feasible_checked += 1

        self.assertGreater(feasible_checked, 40)
        self.assertGreater(infeasible_checked, 15)


if __name__ == "__main__":
    unittest.main(verbosity=2)
