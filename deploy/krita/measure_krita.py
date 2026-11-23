#!/usr/bin/env python3
"""Phase 3 measurement: time Krita's native strokes, batches, preview and .kra save through
krita-gate, the same way the bot would. Run as the dsh user while krita.service and
krita-gate.service are up:

    sudo -u dsh python3 -I /opt/krita-gate/measure_krita.py > /srv/dsh/discord/draw/krita-measure.json

Reports milliseconds per operation (from the gate), wall time per batch, and Krita's cgroup
memory peak. It only creates a scratch document; Krita writes it to out/measure-<time>/.
"""

import json
import math
import socket
import sys
import time

SOCKET = "/run/krita-gate/gate.sock"
CGROUP = "/sys/fs/cgroup/system.slice/krita.service"


def call(job, ops):
    started = time.monotonic()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(900)
        conn.connect(SOCKET)
        conn.sendall((json.dumps({"job": job, "ops": ops}) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
    reply = json.loads(data)
    reply["wall_ms"] = round((time.monotonic() - started) * 1000)
    if not reply.get("ok"):
        raise SystemExit(json.dumps(reply, ensure_ascii=False))
    return reply


def wave(n, y, amplitude=60, x0=40, x1=1160):
    return [[x0 + (x1 - x0) * i / (n - 1), y + amplitude * math.sin(i / max(n - 1, 1) * 6.28)] for i in range(n)]


def memory_peak_mb():
    try:
        with open(f"{CGROUP}/memory.peak") as file:
            return round(int(file.read()) / 1048576)
    except OSError:
        return None


def main():
    job = time.strftime("measure-%m%d-%H%M%S").lower()
    report = {"job": job, "memory_peak_before_mb": memory_peak_mb()}
    doc = call(job, [{"command": "create_document", "params": {"width": 1200, "height": 800, "name": job}}])
    document = doc["results"][0]["result"]["document_id"]
    layer = call(job, [{"command": "create_paint_layer", "target": {"document_id": document},
                        "params": {"name": "strokes"}}])["results"][0]["result"]["node_id"]
    presets = call(job, [{"command": "list_brush_presets", "params": {"query": "Basic-5", "limit": 5}}])
    preset = next(p for p in presets["results"][0]["result"]["presets"] if p["supported_for_painting"])
    report["preset"] = preset["name"]
    target = {"document_id": document, "node_id": layer}

    def stroke(points, size, y):
        return {"command": "paint_path", "target": target,
                "params": {"preset_id": preset["preset_id"], "size_px": size, "opacity": 1.0,
                           "color": "#3366CC", "points": wave(points, y)}}

    singles = []
    for points in (10, 100, 500):
        for size in (8, 60):
            reply = call(job, [stroke(points, size, 100 + len(singles) * 50)])
            singles.append({"points": points, "size_px": size, "op_ms": reply["results"][0]["ms"],
                            "wall_ms": reply["wall_ms"]})
    report["single_strokes"] = singles
    batches = []
    for count in (30, 50):
        ops = [stroke(100, 12, 70 + 13 * i) for i in range(count)]
        reply = call(job, ops)
        times = [r["ms"] for r in reply["results"]]
        batches.append({"strokes": count, "wall_ms": reply["wall_ms"], "gate_ms": reply["ms"],
                        "per_stroke_ms_avg": round(sum(times) / len(times)), "per_stroke_ms_max": max(times)})
    report["batches"] = batches
    preview = call(job, [{"command": "get_preview", "target": {"document_id": document}, "params": {"max_edge": 1024}}])
    report["preview"] = {"wall_ms": preview["wall_ms"], "file": preview["results"][0].get("preview")}
    save = call(job, [{"command": "save_document", "target": {"document_id": document}, "params": {"file": "measure.kra"}},
                      {"command": "export_png", "target": {"document_id": document}, "params": {"file": "measure.png"}}])
    report["save"] = {"kra_ms": save["results"][0]["ms"], "png_ms": save["results"][1]["ms"]}
    report["memory_peak_after_mb"] = memory_peak_mb()
    json.dump(report, sys.stdout, ensure_ascii=False, indent=1)
    print()


if __name__ == "__main__":
    main()
