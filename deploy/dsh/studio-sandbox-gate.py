"""沙盒用的临时 Krita 作品闸道（只在 studio-sandbox.sh 里用，不进 release、不动正式闸道）。

正式闸道只收「正式 dsh.service 主程序直接启动的 node」，沙盒连不进去（这是安全设计）。这里用同一份
works_gate.py 另开一个闸道：连同一个 Krita，作品与登记都放沙盒的临时目录，socket 放沙盒目录，
只收沙盒那个 uid（SBX_PEER_UID）的连线。以 krita 身分执行。
"""

import importlib.util
import os
import socket
import struct
import sys

GATE = os.environ.get("SBX_WORKS_GATE", "/opt/krita-gate/works-v1.4.2/works_gate.py")
PEER_UID = int(os.environ["SBX_PEER_UID"])
# works_gate.py 会 import 同目录的模组（cpu_plan 等），跟直接执行它时一样把它的目录放最前面。
sys.path.insert(0, os.path.dirname(GATE))

spec = importlib.util.spec_from_file_location("works_gate", GATE)
works_gate = importlib.util.module_from_spec(spec)
sys.modules["works_gate"] = works_gate
spec.loader.exec_module(works_gate)


def sandbox_peer(conn):
    _pid, uid, _gid = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    return uid == PEER_UID


def main():
    os.umask(0o007)
    output, registry = os.environ["KRITA_GATE_OUT"], os.environ["KRITA_WORK_REGISTRY"]
    folders = works_gate.WorkFolders(output)
    client = works_gate.StrokeBridgeClient(state_dir=os.environ["KRITA6_MCP_STATE_DIR"], timeout=10.0)
    gate = works_gate.WorkGate(client, folders, stuck_file=os.environ["KRITA_GATE_STUCK"])
    works_gate.serve(works_gate.Works(gate, output, registry), os.environ["KRITA_GATE_SOCKET"],
                     peer_allowed=sandbox_peer)


if __name__ == "__main__":
    main()
