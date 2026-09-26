"""API 业务冒烟脚本（仅标准库）：在真实 HTTP 服务上端到端验证。

检查项：
1. GET /healthz 返回 200 且 status=ok；
2. 可行草稿返回 feasible=true，守恒/范围/目标值全部成立，流量为整数；
3. 不可行草稿返回 feasible=false 且给出收支诊断；
4. 非法草稿返回 HTTP 400；
5. 时序复核：可行（库存承载）、时序不可达、库存越界三场景及校验 400。

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

    # ---------- 时序复核：可行（库存承载） ----------
    # 时隙 0 放出 6 暂存于 N（上限 6），时隙 1 分送 A/B；p1 行程 0。
    schedule_ok = {
        "source": {"id": "S"}, "slots": 2, "source_dispatch": [6, 0],
        "zones": [{"id": "A", "demands": [0, 3]},
                  {"id": "B", "demands": [0, 3]}],
        "nodes": [{"id": "N", "capacity": 6}],
        "pipes": [
            {"id": "p1", "from": "S", "to": "N",
             "min": 0, "max": 10, "preferred": 6, "travel": 0},
            {"id": "p2", "from": "N", "to": "A",
             "min": 0, "max": 10, "preferred": 0, "travel": 0},
            {"id": "p3", "from": "N", "to": "B",
             "min": 0, "max": 10, "preferred": 0, "travel": 0},
            {"id": "p4", "from": "S", "to": "A",
             "min": 0, "max": 0, "preferred": 0, "travel": 0},
        ],
    }
    status, r = request("POST", "/api/schedule", schedule_ok)
    check(status == 200, f"时序复核可行草稿 HTTP 200（实际 {status}）")
    check(r["feasible"] is True, "时序复核结论为可行")
    check(all(isinstance(x, int) for x in r["tie_sequence"]),
          "逐时隙发出量均为整数")
    disp = {row["pipe_id"]: row["dispatch"] for row in r["pipes"]}
    check(disp["p1"] == [6, 0] and disp["p2"] == [0, 3]
          and disp["p3"] == [0, 3],
          f"逐时隙发出台账正确（实际 {disp}）")
    node = r["nodes"][0]
    check(node["inventory"] == [6, 0] and node["capacity"] == 6,
          f"节点库存台账：时隙0 末存 6、期末清零（实际 {node['inventory']}）")
    check(r["source"]["difference"] == [0, 0],
          "水源逐时隙放出恰等于申报量")
    for zrow in r["zones"]:
        check(zrow["difference"] == [0, 0],
              f"分区 {zrow['id']} 逐时隙到水恰等于需求")

    # ---------- 时序复核：时序不可达 ----------
    # 需求在时隙 1，但 p1 行程 2 时隙，水时隙 2 才到 N，来不及。
    schedule_late = {
        "source": {"id": "S"}, "slots": 3, "source_dispatch": [6, 0, 0],
        "zones": [{"id": "A", "demands": [0, 3, 0]},
                  {"id": "B", "demands": [0, 3, 0]}],
        "nodes": [{"id": "N", "capacity": 10}],
        "pipes": [
            {"id": "p1", "from": "S", "to": "N",
             "min": 0, "max": 10, "preferred": 6, "travel": 2},
            {"id": "p2", "from": "N", "to": "A",
             "min": 0, "max": 10, "preferred": 0, "travel": 0},
            {"id": "p3", "from": "N", "to": "B",
             "min": 0, "max": 10, "preferred": 0, "travel": 0},
            {"id": "p4", "from": "S", "to": "A",
             "min": 0, "max": 0, "preferred": 0, "travel": 0},
        ],
    }
    status, r = request("POST", "/api/schedule", schedule_late)
    check(status == 200 and r["feasible"] is False,
          "行程过慢判为不可行（HTTP 200 业务结论）")
    info = r["infeasibility"]
    check(info["first_slot"] == 1,
          f"首个无法满足的时隙为 1（实际 {info['first_slot']}）")
    lacking = {(d["slot"], d["vertex"]) for d in info["deficits"]}
    check((1, "A") in lacking and (1, "B") in lacking,
          f"缺口定位到时隙 1 的分区 A/B（实际 {lacking}）")
    check(any("时序不可达" in t for t in info["reasons"]),
          "原因指明时序不可达")

    # ---------- 时序复核：库存越界 ----------
    # 与可行例同构，但 N 暂存上限 4 < 需暂存 6。
    schedule_over = json.loads(json.dumps(schedule_ok))
    schedule_over["nodes"][0]["capacity"] = 4
    status, r = request("POST", "/api/schedule", schedule_over)
    check(status == 200 and r["feasible"] is False,
          "暂存不足判为不可行（HTTP 200 业务结论）")
    info = r["infeasibility"]
    check(info["first_slot"] == 0,
          f"首个无法满足的时隙为 0（实际 {info['first_slot']}）")
    sur = [s for s in info["surpluses"] if s["vertex"] == "N"]
    check(sur and sur[0]["inventory_used"] == 4
          and sur[0]["capacity"] == 4,
          f"积压定位到节点 N 且暂存已用满（实际 {sur}）")
    check(any("库存越界" in t for t in info["reasons"]),
          "原因指明库存越界")

    # ---------- 时序复核：非法草稿 400 ----------
    bad_schedule = {"source": {"id": "S"}, "slots": 5,
                    "source_dispatch": [1],
                    "zones": [{"id": "A", "demands": [1]}],
                    "nodes": [], "pipes": []}
    status, r = request("POST", "/api/schedule", bad_schedule)
    check(status == 400 and "error" in r,
          f"非法时序草稿返回 400（实际 {status}）")

    print("[smoke] 全部冒烟断言通过")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"[smoke] 失败：{exc}", file=sys.stderr)
        sys.exit(1)
