"""量 Krita 桥接就绪和第一张空白画布的时间（冷启动）。以 krita 身分执行，开一张不存档的 1024x1024 画布再关掉：

注意：Krita 已经顶在 MemoryHigh 上时开新画布会让它自己的记忆体压力飙高、卡十几秒（2026-11-28 实测），只在刚重启后或确认没人在画时跑。
    cd /tmp && runuser -u krita -- env PYTHONPATH=/opt/krita-gate/works-v1.2/plugin:/opt/krita6-mcp/current/src python3 -s measure_cold_start.py [起点的 epoch 秒]
"""
import sys, time, uuid
from krita6_mcp.bridge_client import BridgeClient
t0 = float(sys.argv[1]) if len(sys.argv) > 1 else time.time()
c = BridgeClient(state_dir="/var/lib/krita/.local/state/krita6-mcp", timeout=10.0)
inst = []
while time.time() - t0 < 300:
    try:
        inst = [i for i in c.status()["instances"] if i.get("reachable")]
    except Exception:
        inst = []
    if inst:
        break
    time.sleep(0.25)
if not inst:
    print("not ready after 300s"); sys.exit(1)
ready = time.time() - t0
iid = inst[0]["instance_id"]
t1 = time.time()
r = c.execute("create_document", instance_id=iid, operation_id="coldprobe-" + uuid.uuid4().hex,
              params={"width": 1024, "height": 1024, "name": "whale-work-coldprobe"}, wait_timeout=30)
t2 = time.time()
doc = (r.get("result") or {}).get("document_id")
r2 = c.execute("close_document", instance_id=iid, operation_id="coldprobe-" + uuid.uuid4().hex,
               target={"document_id": doc}, wait_timeout=30) if doc else {"state": "skipped"}
t3 = time.time()
print(f"bridge_ready={ready:.1f}s create_document={t2-t1:.2f}s ({r.get('state')}) close={t3-t2:.2f}s ({r2.get('state')}) first_canvas_total={t2-t0:.1f}s")
