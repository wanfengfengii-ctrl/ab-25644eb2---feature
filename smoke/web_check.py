"""经 Web（nginx 反代）做联调冒烟：静态健康端点 + 透传业务 API。

BASE_URL 默认 http://web（compose 网络内的 web 服务）。
"""

import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("WEB_URL", "http://web").rstrip("/")


def fail(msg):
    raise AssertionError(msg)


def main():
    print(f"[web-smoke] 目标 {BASE}")

    with urllib.request.urlopen(BASE + "/healthz", timeout=10) as resp:
        text = resp.read().decode("utf-8")
        if resp.status != 200 or "ok" not in text:
            fail(f"Web 健康检查异常：{resp.status} {text!r}")
    print("  ✓ GET /healthz 经 nginx 返回 200/ok")

    payload = {
        "source": {"id": "S"}, "source_total": 10,
        "zones": [{"id": "A", "demand": 6}, {"id": "B", "demand": 4}],
        "nodes": [{"id": "N"}],
        "pipes": [
            {"id": "p1", "from": "S", "to": "N", "min": 0, "max": 10, "preferred": 5},
            {"id": "p2", "from": "N", "to": "A", "min": 0, "max": 10, "preferred": 3},
            {"id": "p3", "from": "N", "to": "B", "min": 0, "max": 10, "preferred": 7},
            {"id": "p4", "from": "S", "to": "A", "min": 0, "max": 0, "preferred": 0},
        ],
    }
    req = urllib.request.Request(
        BASE + "/api/balance",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if resp.status != 200 or not data.get("feasible"):
        fail(f"经 nginx 的业务配平失败：{data}")
    print(f"  ✓ POST /api/balance 经 nginx 透传成功，序列 {data['tie_sequence']}")

    schedule_payload = {
        "source": {"id": "S"}, "horizon": 3,
        "source_release": [5, 5, 0],
        "zones": [{"id": "A", "demands": [0, 2, 4]},
                  {"id": "B", "demands": [0, 2, 2]}],
        "nodes": [{"id": "N", "capacity": 2}],
        "pipes": [
            {"id": "p1", "from": "S", "to": "N", "min": 0, "max": 10,
             "preferred": 5, "transit": 1},
            {"id": "p2", "from": "N", "to": "A", "min": 0, "max": 10,
             "preferred": 3, "transit": 0},
            {"id": "p3", "from": "N", "to": "B", "min": 0, "max": 10,
             "preferred": 7, "transit": 0},
            {"id": "p4", "from": "S", "to": "A", "min": 0, "max": 0,
             "preferred": 0, "transit": 0},
        ],
    }
    req = urllib.request.Request(
        BASE + "/api/schedule",
        data=json.dumps(schedule_payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if resp.status != 200 or not data.get("feasible"):
        fail(f"经 nginx 的时序复核失败：{data}")
    if data["objective"] != 22 or not data.get("ledger"):
        fail(f"经 nginx 的时序复核结果异常：{data}")
    print(f"  ✓ POST /api/schedule 经 nginx 透传成功，"
          f"逐隙台账 {len(data['ledger'])} 隙，偏差和 {data['objective']}")
    print("[web-smoke] Web/API 联调全部通过")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"[web-smoke] 失败：{exc}", file=sys.stderr)
        sys.exit(1)
