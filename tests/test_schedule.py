"""时序复核求解器单元测试（标准库 unittest，无需第三方依赖）。

含随机小时空网络上的全量整数解暴力枚举对照：可行性、最优目标值、
时隙优先+录入顺序的字典序决胜序列逐例比对。
"""

import itertools
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.schedule import ValidationError, solve_schedule, validate_schedule  # noqa: E402


def pipe(pid, u, v, lo, hi, pref, transit):
    return {"id": pid, "from": u, "to": v, "min": lo, "max": hi,
            "preferred": pref, "transit": transit}


def sample_payload():
    """全约束唯一解样例（含一次跨时隙暂存）。"""
    return {
        "source": {"id": "S"},
        "horizon": 3,
        "source_release": [5, 5, 0],
        "zones": [{"id": "A", "demands": [0, 2, 4]},
                  {"id": "B", "demands": [0, 2, 2]}],
        "nodes": [{"id": "N", "capacity": 2}],
        "pipes": [
            pipe("p1", "S", "N", 0, 10, 5, 1),
            pipe("p2", "N", "A", 0, 10, 3, 0),
            pipe("p3", "N", "B", 0, 10, 7, 0),
            pipe("p4", "S", "A", 0, 0, 0, 0),
        ],
    }


class TestScheduleBasic(unittest.TestCase):
    def test_feasible_sample(self):
        r = solve_schedule(sample_payload())
        self.assertTrue(r["feasible"])
        # 唯一解：p1=[5,5,-] p2=[0,2,4] p3=[0,2,2] p4=[0,0,0]
        self.assertEqual(r["objective"], 0 + (3 + 1 + 1) + (7 + 5 + 5) + 0)
        self.assertEqual(r["tie_sequence"],
                         [5, 0, 0, 0,
                          5, 2, 2, 0,
                          0, 4, 2, 0])
        inv = {row["node_id"]: [s["stock"] for s in row["slots"]]
               for row in r["inventory"]}
        self.assertEqual(inv["N"], [0, 1, 0])
        # 台账自洽：水源逐隙放出=申报、分区逐隙到水=需求、期末无在途
        for slot in r["ledger"]:
            self.assertEqual(slot["source"]["difference"], 0)
            self.assertTrue(all(z["difference"] == 0
                                for z in slot["zones"]))
            for n in slot["nodes"]:
                self.assertEqual(
                    n["stock_start"] + n["arrived"],
                    n["dispatched"] + n["stock_end"])
                self.assertLessEqual(n["stock_end"], n["capacity"])
        self.assertEqual(r["ledger"][-1]["in_transit_end"], 0)
        self.assertEqual(r["ledger"][-1]["nodes"][0]["stock_end"], 0)
        # 逐管逐隙明细与范围
        for d in r["dispatch"]:
            for s in d["slots"]:
                self.assertLessEqual(d["min"], s["flow"])
                self.assertLessEqual(s["flow"], d["max"])
                if not s["usable"]:
                    self.assertEqual(s["flow"], 0)
                    self.assertIsNone(s["arrival_slot"])

    def test_lexicographic_tie_break_slot_major(self):
        # X 时隙0需求2，由并联管 p1/p2 供给（优选量均为 2）：
        # (0,2)/(1,1)/(2,0) 偏差和同为 2，按时隙0、录入顺序字典序取 p1@0=0。
        payload = {
            "source": {"id": "S"},
            "horizon": 2,
            "source_release": [2, 2],
            "zones": [{"id": "X", "demands": [2, 0]},
                      {"id": "Y", "demands": [0, 2]}],
            "nodes": [],
            "pipes": [
                pipe("p1", "S", "X", 0, 4, 2, 0),
                pipe("p2", "S", "X", 0, 4, 2, 0),
                pipe("p3", "S", "Y", 0, 4, 2, 0),
                pipe("p4", "S", "Y", 0, 4, 0, 0),
            ],
        }
        r = solve_schedule(payload)
        self.assertTrue(r["feasible"])
        # 逐隙偏差：p1@0=0、p2@0=2 合计偏差 2；p1@1/p2@1/p3@0 为 0 流量
        # 各偏离优选量 2，合计 6；其余为 0 → 总偏差 8
        self.assertEqual(r["objective"], 8)
        self.assertEqual(r["tie_sequence"], [0, 2, 0, 0,
                                             0, 0, 2, 0])

    def test_storage_boundary_feasible(self):
        # 水在时隙0放出、时隙2才被分区接收，必须在 N 暂存 3 单位两隙
        payload = {
            "source": {"id": "S"},
            "horizon": 3,
            "source_release": [3, 0, 0],
            "zones": [{"id": "A", "demands": [0, 0, 3]},
                      {"id": "B", "demands": [0, 0, 0]}],
            "nodes": [{"id": "N", "capacity": 3}],
            "pipes": [
                pipe("p1", "S", "N", 0, 5, 3, 0),
                pipe("p2", "N", "A", 0, 5, 0, 0),
                pipe("p3", "N", "B", 0, 5, 0, 0),
                pipe("p4", "S", "B", 0, 5, 0, 0),
            ],
        }
        r = solve_schedule(payload)
        self.assertTrue(r["feasible"])
        inv = {row["node_id"]: [s["stock"] for s in row["slots"]]
               for row in r["inventory"]}
        self.assertEqual(inv["N"], [3, 3, 0])
        # p2@2=3 偏离优选量 0（+3）；p1@1=p1@2=0 各偏离优选量 3（+6）
        self.assertEqual(r["objective"], 9)

    def test_infeasible_temporally_unreachable(self):
        # 分区 A 时隙0需求1，但唯一通向 A 的管路行程为 1 隙，当隙无法到达
        payload = {
            "source": {"id": "S"},
            "horizon": 2,
            "source_release": [1, 0],
            "zones": [{"id": "A", "demands": [1, 0]},
                      {"id": "B", "demands": [0, 0]}],
            "nodes": [],
            "pipes": [
                pipe("p1", "S", "A", 0, 5, 1, 1),
                pipe("p2", "S", "B", 0, 5, 0, 0),
                pipe("p3", "S", "B", 0, 5, 0, 0),
                pipe("p4", "S", "B", 0, 5, 0, 0),
            ],
        }
        r = solve_schedule(payload)
        self.assertFalse(r["feasible"])
        info = r["infeasibility"]
        self.assertEqual(info["first_failure_slot"], 0)
        self.assertTrue(any(d["vertex"] == "A" and d["slot"] == 0
                            for d in info["deficits"]))
        self.assertTrue(any("时隙 0" in t and "A" in t
                            for t in info["reasons"]))

    def test_infeasible_inventory_overflow(self):
        # 水必须在 N 暂存 3 单位，但暂存上限只有 2 → 库存越界
        payload = {
            "source": {"id": "S"},
            "horizon": 3,
            "source_release": [3, 0, 0],
            "zones": [{"id": "A", "demands": [0, 0, 3]},
                      {"id": "B", "demands": [0, 0, 0]}],
            "nodes": [{"id": "N", "capacity": 2}],
            "pipes": [
                pipe("p1", "S", "N", 0, 5, 3, 0),
                pipe("p2", "N", "A", 0, 5, 0, 0),
                pipe("p3", "N", "B", 0, 5, 0, 0),
                pipe("p4", "S", "B", 0, 5, 0, 0),
            ],
        }
        r = solve_schedule(payload)
        self.assertFalse(r["feasible"])
        info = r["infeasibility"]
        self.assertIsNotNone(info["first_failure_slot"])
        self.assertIn("暂存", "。".join(info["reasons"]))

    def test_total_mismatch(self):
        p = sample_payload()
        p["source_release"] = [5, 4, 0]  # 合计 9 ≠ 需求合计 10
        r = solve_schedule(p)
        self.assertFalse(r["feasible"])
        self.assertIn("不相等", r["infeasibility"]["reasons"][0])

    def test_infeasible_min_vs_in_transit(self):
        # p2 最小量 1，但行程 2 隙在 2 隙期内永远期末后才能到达：
        # "任一时隙发出量不得超出管路范围"与"期末无在途"冲突
        payload = {
            "source": {"id": "S"},
            "horizon": 2,
            "source_release": [1, 1],
            "zones": [{"id": "A", "demands": [1, 1]},
                      {"id": "B", "demands": [0, 0]}],
            "nodes": [],
            "pipes": [
                pipe("p1", "S", "A", 0, 5, 1, 0),
                pipe("p2", "S", "B", 1, 5, 1, 2),
                pipe("p3", "S", "B", 0, 5, 0, 0),
                pipe("p4", "S", "B", 0, 5, 0, 0),
            ],
        }
        r = solve_schedule(payload)
        self.assertFalse(r["feasible"])
        info = r["infeasibility"]
        self.assertEqual(info["first_failure_slot"], 0)
        self.assertTrue(any("p2" in t and "在途" in t
                            for t in info["reasons"]))


class TestScheduleValidation(unittest.TestCase):
    def test_horizon_bounds_and_type(self):
        p = sample_payload()
        p["horizon"] = 1
        with self.assertRaises(ValidationError):
            validate_schedule(p)
        p = sample_payload()
        p["horizon"] = 5
        with self.assertRaises(ValidationError):
            validate_schedule(p)
        p = sample_payload()
        p["horizon"] = 3.0
        with self.assertRaises(ValidationError):
            validate_schedule(p)
        p = sample_payload()
        p["horizon"] = True  # bool 不得当作整数
        with self.assertRaises(ValidationError):
            validate_schedule(p)

    def test_release_shape(self):
        p = sample_payload()
        p["source_release"] = [5, 5]  # 长度必须等于时隙数
        with self.assertRaises(ValidationError):
            validate_schedule(p)
        p = sample_payload()
        p["source_release"] = [5, -1, 0]
        with self.assertRaises(ValidationError):
            validate_schedule(p)

    def test_zone_demands_shape(self):
        p = sample_payload()
        p["zones"][0]["demands"] = [0, 2]
        with self.assertRaises(ValidationError):
            validate_schedule(p)
        p = sample_payload()
        p["zones"][0]["demands"] = [0, 2, 1.5]
        with self.assertRaises(ValidationError):
            validate_schedule(p)

    def test_node_capacity_and_transit(self):
        p = sample_payload()
        p["nodes"][0]["capacity"] = -1
        with self.assertRaises(ValidationError):
            validate_schedule(p)
        p = sample_payload()
        p["pipes"][0]["transit"] = 4  # 超过时隙数 3
        with self.assertRaises(ValidationError):
            validate_schedule(p)
        p = sample_payload()
        p["pipes"][0]["transit"] = -1
        with self.assertRaises(ValidationError):
            validate_schedule(p)

    def test_counts_and_duplicates(self):
        p = sample_payload()
        p["zones"] = p["zones"][:1]  # 分区少于 2 个
        with self.assertRaises(ValidationError):
            validate_schedule(p)
        p = sample_payload()
        p["pipes"] = p["pipes"][:3]  # 管路少于 4 条
        with self.assertRaises(ValidationError):
            validate_schedule(p)
        p = sample_payload()
        p["pipes"][1]["id"] = "p1"
        with self.assertRaises(ValidationError):
            validate_schedule(p)
        p = sample_payload()
        p["zones"][0]["id"] = "N"  # 与分流节点重名
        with self.assertRaises(ValidationError):
            validate_schedule(p)


def _brute_schedule(payload):
    """枚举所有可用（时隙,管路）整数发出量，返回 [(seq, objective)] 全量可行解。"""
    model = validate_schedule(payload)
    h = model["horizon"]
    pipes = model["pipes"]
    release = model["release"]
    source_id = model["source_id"]
    zones = {z["id"]: z["demands"] for z in model["zones"]}
    nodes = model["nodes"]
    capacity = model["capacity"]

    pairs = [(t, i) for t in range(h) for i in range(len(pipes))
             if t + pipes[i]["transit"] < h]
    # 期末后才能到达的时隙发出量恒 0；这些时隙最小量 > 0 则直接不可行
    if any(pipes[i]["min"] > 0
           for t in range(h) for i in range(len(pipes))
           if t + pipes[i]["transit"] >= h):
        return []
    options = [range(pipes[i]["min"], pipes[i]["max"] + 1) for t, i in pairs]

    solutions = []
    for vals in itertools.product(*options):
        f = dict(zip(pairs, vals))

        def outflow(vid, t):
            return sum(f.get((t, i), 0) for i, p in enumerate(pipes)
                       if p["from"] == vid)

        def inflow(vid, t):
            return sum(f.get((t - p["transit"], i), 0)
                       for i, p in enumerate(pipes)
                       if p["to"] == vid and 0 <= t - p["transit"]
                       and t - p["transit"] + p["transit"] < h)

        if any(outflow(source_id, t) != release[t] for t in range(h)):
            continue
        if any(inflow(z, t) != dz[t] for z, dz in zones.items()
               for t in range(h)):
            continue
        ok = True
        for n in nodes:
            stock = 0
            for t in range(h):
                stock += inflow(n, t) - outflow(n, t)
                if stock < 0 or stock > capacity[n]:
                    ok = False
                    break
            if stock != 0:
                ok = False
            if not ok:
                break
        if not ok:
            continue
        obj = sum(abs(f[(t, i)] - pipes[i]["preferred"]) for t, i in pairs)
        seq = tuple(f.get((t, i), 0)
                    for t in range(h) for i in range(len(pipes)))
        solutions.append((seq, obj))
    return solutions


def _random_feasible_schedule(rng):
    """按时间顺序随机放水构造必然可行的时隙草稿。"""
    h = rng.randrange(2, 4)
    n_nodes = rng.randrange(0, 3)
    n_zones = rng.randrange(2, 4)
    nodes = [f"N{i}" for i in range(n_nodes)]
    zones = [f"Z{i}" for i in range(n_zones)]

    # 有向边：S/N_i → N_j(j>i)/分区（无环），保证连通
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
    for c in candidates:
        if len(edges) >= 5:
            break
        if c not in edges:
            edges.append(c)
    while len(edges) < 4:
        edges.append(rng.choice(candidates))
    edges = edges[:6]

    transits = [rng.randrange(0, h) for _ in edges]
    # 逐时隙推进：先到达、再决定发出，保证库存非负；
    # 边按 N_i→N_j(j>i) 无环，同隙 0 行程级联在单遍内被正确处理
    dispatches = [dict() for _ in edges]  # edge -> {t: flow}
    release = [0] * h
    arrivals = {(v, t): 0 for v in nodes + zones for t in range(h)}
    stock = {v: 0 for v in nodes}
    out_edges = {}
    for i, (u, v) in enumerate(edges):
        out_edges.setdefault(u, []).append(i)
    for t in range(h):
        for v in ["S"] + nodes:
            ids = out_edges.get(v, [])
            if v == "S":
                budget = rng.randrange(0, 4)
            else:
                budget = stock[v] + arrivals[(v, t)]
                if not ids:
                    if budget > 0:
                        return None  # 节点有水无出口，弃用该样本
                    continue
            for ei in ids:
                if budget <= 0:
                    break
                tau = transits[ei]
                if t + tau >= h:
                    continue  # 期末后到达的时隙不发
                take = rng.randrange(0, budget + 1)
                if take <= 0:
                    continue
                budget -= take
                dispatches[ei][t] = take
                _, to = edges[ei]
                arrivals[(to, t + tau)] += take
            if v == "S":
                release[t] = sum(dispatches[ei].get(t, 0) for ei in ids)
            else:
                stock[v] = budget  # 剩余结转到下一时隙
    # 期末库存必须为 0
    if any(stock[v] != 0 for v in nodes):
        return None
    demands = {z: [arrivals[(z, t)] for t in range(h)] for z in zones}
    if sum(release) != sum(sum(d) for d in demands.values()):
        return None

    pipes = []
    for i, ((u, v), dis) in enumerate(zip(edges, dispatches)):
        usable_vals = [dis.get(t, 0) for t in range(h)
                       if t + transits[i] < h]
        # min 对每个时隙生效：不得超过各可用时隙发出量的最小值
        lo = max(0, (min(usable_vals) if usable_vals else 0)
                 - rng.randrange(0, 2))
        if transits[i] > 0:
            lo = 0  # 末隙期末后才能到达，最小量必须为 0 才可行
        hi = (max(usable_vals) if usable_vals else 0) + rng.randrange(0, 2)
        pref = rng.randrange(lo, hi + 1)
        pipes.append(pipe(f"e{i}", u, v, lo, hi, pref, transits[i]))
    cap = {}
    # 重放一遍求各节点峰值库存，作为上限（偶尔再收紧 0/1 制造临界）
    stock2 = {v: 0 for v in nodes}
    peak_stock = {v: 0 for v in nodes}
    for t in range(h):
        for v in nodes:
            stock2[v] += arrivals[(v, t)]
            out = sum(dispatches[ei].get(t, 0)
                      for ei in out_edges.get(v, []))
            stock2[v] -= out
            peak_stock[v] = max(peak_stock[v], stock2[v])
    for v in nodes:
        cap[v] = peak_stock[v] + rng.randrange(0, 2)

    return {
        "source": {"id": "S"},
        "horizon": h,
        "source_release": release,
        "zones": [{"id": z, "demands": demands[z]} for z in zones],
        "nodes": [{"id": n, "capacity": cap[n]} for n in nodes],
        "pipes": pipes,
    }


def _random_loose_schedule(rng):
    """完全松散的随机小时空草稿，大概率不可行。"""
    h = rng.randrange(2, 4)
    n_nodes = rng.randrange(0, 3)
    n_zones = rng.randrange(2, 4)
    nodes = [f"N{i}" for i in range(n_nodes)]
    zones = [f"Z{i}" for i in range(n_zones)]
    sources = ["S"] + nodes
    targets = nodes + zones
    candidates = [(u, v) for u in sources for v in targets if u != v]
    n_pipes = rng.randrange(4, 7)
    pipes = []
    for i in range(n_pipes):
        u, v = rng.choice(candidates)
        lo = rng.randrange(0, 2)
        hi = lo + rng.randrange(0, 2)
        pipes.append(pipe(f"e{i}", u, v, lo, hi,
                          rng.randrange(lo, hi + 1), rng.randrange(0, h)))
    return {
        "source": {"id": "S"},
        "horizon": h,
        "source_release": [rng.randrange(0, 4) for _ in range(h)],
        "zones": [{"id": z,
                   "demands": [rng.randrange(0, 3) for _ in range(h)]}
                  for z in zones],
        "nodes": [{"id": n, "capacity": rng.randrange(0, 3)} for n in nodes],
        "pipes": pipes,
    }


class TestScheduleRandomAgainstBruteForce(unittest.TestCase):
    """随机小时空网络与暴力枚举逐例对照：可行性、目标值、决胜序列。"""

    def test_random_schedules(self):
        rng = random.Random(20260926)
        cases = 0
        feasible_checked = 0
        infeasible_checked = 0
        while cases < 300:
            if rng.random() < 0.6:
                payload = _random_feasible_schedule(rng)
                if payload is None:
                    continue
            else:
                payload = _random_loose_schedule(rng)
            # 枚举空间过大则跳过（范围 0..2，至多 6 管 × 3 隙）
            model = validate_schedule(payload)
            space = 1
            for t in range(model["horizon"]):
                for p in model["pipes"]:
                    if t + p["transit"] < model["horizon"]:
                        space *= p["max"] - p["min"] + 1
                        if space > 200000:
                            break
                if space > 200000:
                    break
            if space > 200000:
                continue
            cases += 1

            brute = _brute_schedule(payload)
            r = solve_schedule(payload)
            if not brute:
                self.assertFalse(r["feasible"], msg=str(payload))
                infeasible_checked += 1
                continue
            self.assertTrue(r["feasible"], msg=str(payload))
            best_obj = min(o for _, o in brute)
            self.assertEqual(r["objective"], best_obj, msg=str(payload))
            best_seq = min(seq for seq, o in brute if o == best_obj)
            self.assertEqual(tuple(r["tie_sequence"]), best_seq,
                             msg=str(payload))
            # 台账自洽：逐时隙守恒、库存不越限、期末清空
            for slot in r["ledger"]:
                self.assertEqual(slot["source"]["difference"], 0)
                self.assertTrue(all(z["difference"] == 0
                                    for z in slot["zones"]))
                for n in slot["nodes"]:
                    self.assertEqual(
                        n["stock_start"] + n["arrived"],
                        n["dispatched"] + n["stock_end"])
                    self.assertLessEqual(n["stock_end"], n["capacity"])
            self.assertEqual(r["ledger"][-1]["in_transit_end"], 0)
            feasible_checked += 1
        self.assertGreater(feasible_checked, 60)
        self.assertGreater(infeasible_checked, 30)


if __name__ == "__main__":
    unittest.main(verbosity=2)
