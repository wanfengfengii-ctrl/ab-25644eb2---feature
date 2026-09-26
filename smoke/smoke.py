"""API 业务冒烟脚本（仅标准库）：在真实 HTTP 服务上端到端验证。

检查项：
1. GET /healthz 返回 200 且 status=ok；
2. 可行草稿返回 feasible=true，守恒/范围/目标值全部成立，流量为整数；
3. 不可行草稿返回 feasible=false 且给出收支诊断；
4. 非法草稿返回 HTTP 400；
5. 时序复核：可行场景逐时隙台账自洽（放出=申报、到水=需求、
   库存不越限、期末无库存与在途）；
6. 时序不可达场景判不可行并指出首个无法满足的时隙；
7. 库存越界场景判不可行且原因提及暂存上限；
8. 非法时隙草稿返回 HTTP 400。

由容器内 exec 执行，默认访问本机 8000；可用 BASE_URL 覆盖。
任何断言失败即以非零退出码报告。
"""

import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8000").rstrip("/")


def request(method, path, payload=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ✓ {msg}")


def main():
    print(f"[smoke] 目标服务 {BASE}")

    status, body = request("GET", "/healthz")
    check(status == 200 and body.get("status") == "ok",
          f"健康检查 200/status=ok（实际 {status}, {body}）")

    feasible_payload = {
        "source": {"id": "S"},
        "source_total": 10,
        "zones": [{"id": "A", "demand": 6}, {"id": "B", "demand": 4}],
        "nodes": [{"id": "N"}],
        "pipes": [
            {"id": "p1", "from": "S", "to": "N",
             "min": 0, "max": 10, "preferred": 5},
            {"id": "p2", "from": "N", "to": "A",
             "min": 0, "max": 10, "preferred": 3},
            {"id": "p3", "from": "N", "to": "B",
             "min": 0, "max": 10, "preferred": 7},
            {"id": "p4", "from": "S", "to": "A",
             "min": 0, "max": 0, "preferred": 0},
        ],
    }
    status, r = request("POST", "/api/balance", feasible_payload)
    check(status == 200, f"可行草稿 HTTP 200（实际 {status}）")
    check(r["feasible"] is True, "结论为可行")
    check(r["tie_sequence"] == [10, 6, 4, 0],
          f"流量序列 [10,6,4,0]（实际 {r['tie_sequence']}）")
    check(all(isinstance(x, int) for x in r["tie_sequence"]),
          "所有流量均为整数")
    check(r["objective"] == 11, f"绝对偏差和为 11（实际 {r['objective']}）")
    src = r["balances"]["source"]
    check(src["outflow"] == 10 and src["difference"] == 0,
          "水源流出恰等于总量 10")
    node = r["balances"]["nodes"][0]
    check(node["inflow"] == node["outflow"] == 10,
          "分流节点流入=流出=10")
    for zrow, need in zip(r["balances"]["zones"], (6, 4)):
        check(zrow["inflow"] == need and zrow["difference"] == 0,
              f"分区 {zrow['id']} 流入恰等于需求 {need}")
    for f in r["flows"]:
        check(f["min"] <= f["flow"] <= f["max"],
              f"管路 {f['pipe_id']} 流量 {f['flow']} 在范围内")

    infeasible_payload = json.loads(json.dumps(feasible_payload))
    # A 需求改成 60：总量与需求不等且容量不足，必不可行
    infeasible_payload["zones"][0]["demand"] = 60
    status, r = request("POST", "/api/balance", infeasible_payload)
    check(status == 200 and r["feasible"] is False,
          "超量需求判为不可行（HTTP 200 业务结论）")
    info = r["infeasibility"]
    check(info["total_demand"] == 64 and info["shortfall_flow"] > 0,
          "诊断含需求合计与流量缺口")
    check(bool(info["reasons"]), "给出可读的不可行原因")

    bad_payload = {"source": {"id": "S"}, "source_total": 1,
                   "zones": [{"id": "A", "demand": 1}],
                   "nodes": [], "pipes": []}
    status, r = request("POST", "/api/balance", bad_payload)
    check(status == 400 and "error" in r,
          f"非法草稿返回 400（实际 {status}）")

    # ---------- 时序复核 /api/schedule ----------
    schedule_payload = {
        "source": {"id": "S"},
        "horizon": 3,
        "source_release": [5, 5, 0],
        "zones": [{"id": "A", "demands": [0, 2, 4]},
                  {"id": "B", "demands": [0, 2, 2]}],
        "nodes": [{"id": "N", "capacity": 2}],
        "pipes": [
            {"id": "p1", "from": "S", "to": "N",
             "min": 0, "max": 10, "preferred": 5, "transit": 1},
            {"id": "p2", "from": "N", "to": "A",
             "min": 0, "max": 10, "preferred": 3, "transit": 0},
            {"id": "p3", "from": "N", "to": "B",
             "min": 0, "max": 10, "preferred": 7, "transit": 0},
            {"id": "p4", "from": "S", "to": "A",
             "min": 0, "max": 0, "preferred": 0, "transit": 0},
        ],
    }
    status, r = request("POST", "/api/schedule", schedule_payload)
    check(status == 200, f"时序可行草稿 HTTP 200（实际 {status}）")
    check(r["feasible"] is True, "时序结论为可行")
    check(r["objective"] == 22,
          f"时序绝对偏差和为 22（实际 {r['objective']}）")
    check(r["tie_sequence"] == [5, 0, 0, 0, 5, 2, 2, 0, 0, 4, 2, 0],
          f"时隙×管路决胜序列正确（实际 {r['tie_sequence']}）")
    check(all(isinstance(x, int) for x in r["tie_sequence"]),
          "逐时隙发出量均为整数")
    inv = {row["node_id"]: [s["stock"] for s in row["slots"]]
           for row in r["inventory"]}
    check(inv["N"] == [0, 1, 0],
          f"节点 N 逐隙库存 [0,1,0]（实际 {inv['N']}）")
    for slot in r["ledger"]:
        check(slot["source"]["difference"] == 0,
              f"时隙 {slot['slot']} 水源放出恰等于申报")
        check(all(z["difference"] == 0 for z in slot["zones"]),
              f"时隙 {slot['slot']} 各分区到水恰等于需求")
        for n in slot["nodes"]:
            check(n["stock_start"] + n["arrived"]
                  == n["dispatched"] + n["stock_end"],
                  f"时隙 {slot['slot']} 节点 {n['id']} 库存守恒")
            check(0 <= n["stock_end"] <= n["capacity"],
                  f"时隙 {slot['slot']} 节点 {n['id']} 库存不越上限")
    last = r["ledger"][-1]
    check(last["in_transit_end"] == 0
          and all(n["stock_end"] == 0 for n in last["nodes"]),
          "期末无库存、无在途水量")

    unreachable = json.loads(json.dumps(schedule_payload))
    # 时隙 0 分区 A 需求 1：N 当隙无水、p4 容量 0，水最早时隙 1 才能到
    unreachable["zones"][0]["demands"] = [1, 2, 3]
    status, r = request("POST", "/api/schedule", unreachable)
    check(status == 200 and r["feasible"] is False,
          "时序不可达判为不可行（HTTP 200 业务结论）")
    info = r["infeasibility"]
    check(info["first_failure_slot"] == 0,
          f"首个无法满足的时隙为 0（实际 {info['first_failure_slot']}）")
    check(any("时隙 0" in t for t in info["reasons"]),
          "不可行原因指到时隙 0")

    overflow = json.loads(json.dumps(schedule_payload))
    # 水须在 N 跨隙暂存 1 单位，但暂存上限调为 0 → 库存越界
    overflow["nodes"][0]["capacity"] = 0
    status, r = request("POST", "/api/schedule", overflow)
    check(status == 200 and r["feasible"] is False,
          "库存越界判为不可行（HTTP 200 业务结论）")
    info = r["infeasibility"]
    check(info["first_failure_slot"] is not None,
          "库存越界给出首个无法满足的时隙")
    check("暂存" in "。".join(info["reasons"]),
          "库存越界原因提及暂存上限")

    bad_schedule = json.loads(json.dumps(schedule_payload))
    bad_schedule["horizon"] = 5  # 时隙数超出 2~4
    status, r = request("POST", "/api/schedule", bad_schedule)
    check(status == 400 and "error" in r,
          f"非法时隙草稿返回 400（实际 {status}）")

    print("[smoke] 全部冒烟断言通过")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"[smoke] 失败：{exc}", file=sys.stderr)
        sys.exit(1)
