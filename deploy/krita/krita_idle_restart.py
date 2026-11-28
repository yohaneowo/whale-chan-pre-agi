#!/usr/bin/env python3
"""闲置时重启 Krita，把它越长越大的记忆体清掉。由 krita-idle-restart.timer 每 10 分钟以 root 跑一次。

要同时满足才重启：
- krita.service 和 krita-gate.service 都在跑（被 guard 停住、或有人手动停掉时不碰）；
- 闲置：krita-gate 的 CPU 用量（cgroup cpu.stat 的 usage_usec）至少 IDLE_MINUTES 分钟没变过。gate
  没请求时 CPU 用量完全不动（2026-11-28 量 60 秒是 0），每一笔画图请求都会让它变，所以这比看日志准；
  重启前再看 FINAL_CHECK 秒、确认 socket 上没有连线，避免刚好有人开始画；
- 而且下面任一项：
  - Krita 的匿名记忆体（memory.stat 的 anon，约等于它自己长出来的部分）≥ ANON_LIMIT_MB；
  - 每天的闲置时段（台北时间 DAILY_HOURS 点）而且 Krita 已经连续跑了 DAILY_MIN_UPTIME_H 小时以上。

每个作品请求都是开档、存档、关档（works_gate 的 document()），请求之间重启不会弄丢作品。
重启后等到 Krita 的桥接写出新的 discovery 档、埠可以连上，把花的秒数写进 /var/log/krita-guard.log。
`--dry-run` 只印判断，不重启。
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

IDLE_MINUTES = 30
FINAL_CHECK = 20
ANON_LIMIT_MB = 1100
DAILY_HOURS = {4}
DAILY_MIN_UPTIME_H = 20
READY_TIMEOUT = 180
TAIPEI = timezone(timedelta(hours=8))  # 台北没有夏令时间，不必依赖 tzdata

CG = "/sys/fs/cgroup/system.slice"
STATE = "/var/lib/krita-guard/idle.json"
LOG = "/var/log/krita-guard.log"
DISCOVERY = Path("/var/lib/krita/.local/state/krita6-mcp")
GATE_SOCKET = "/run/krita-gate/gate.sock"


def keyed(text, key):
	for line in (text or "").splitlines():
		parts = line.split()
		if len(parts) >= 2 and parts[0] == key:
			try:
				return int(parts[1])
			except ValueError:
				return None
	return None


def track(state, gate_pid, usage, now):
	"""更新「gate 最后一次有动静」的时间。gate 换了行程或用量变了都算有动静；第一次看到也从现在算起。"""
	if state.get("gate_pid") != gate_pid or state.get("usage") != usage or "changed_at" not in state:
		return {"gate_pid": gate_pid, "usage": usage, "changed_at": now}
	return dict(state)


def decide(*, krita_active, gate_active, idle_seconds, anon_mb, uptime_h, local_hour):
	"""回 (要不要重启, 原因)。纯判断，方便测试。"""
	if not (krita_active and gate_active):
		return False, "Krita 或 krita-gate 没在跑，不动"
	if anon_mb is not None and anon_mb >= ANON_LIMIT_MB:
		why = f"Krita 匿名记忆体 {anon_mb}MB ≥ {ANON_LIMIT_MB}MB"
	elif local_hour in DAILY_HOURS and uptime_h is not None and uptime_h >= DAILY_MIN_UPTIME_H:
		why = f"每日闲置时段（台北 {local_hour} 点），Krita 已跑 {uptime_h:.0f} 小时"
	else:
		return False, f"不需要重启（anon {anon_mb}MB，已跑 {uptime_h or 0:.1f} 小时）"
	if idle_seconds < IDLE_MINUTES * 60:
		return False, f"{why}，但 gate 只闲置 {idle_seconds / 60:.0f} 分钟（要 {IDLE_MINUTES}），先不动"
	return True, f"{why}，gate 已闲置 {idle_seconds / 60:.0f} 分钟"


def systemctl(*args):
	return subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=300)


def show(unit, prop):
	return systemctl("show", unit, "-p", prop, "--value").stdout.strip()


def gate_usage():
	try:
		return keyed(Path(f"{CG}/krita-gate.service/cpu.stat").read_text(), "usage_usec")
	except OSError:
		return None


def gate_connections():
	"""gate socket 上已建立的连线数（ss 列出来的 u_str ESTAB，本地端是 gate.sock）。"""
	out = subprocess.run(["ss", "-xH", "state", "connected"], capture_output=True, text=True).stdout
	return sum(1 for line in out.splitlines() if GATE_SOCKET in line)


def uptime_hours(unit):
	since = show(unit, "ActiveEnterTimestampMonotonic")
	try:
		with open("/proc/uptime") as stream:
			now = float(stream.read().split()[0])
		return (now - int(since) / 1e6) / 3600 if int(since) else None
	except (OSError, ValueError):
		return None


def log(message):
	line = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} IDLE-RESTART: {message}"
	print(line, flush=True)
	with open(LOG, "a", encoding="utf-8") as stream:
		stream.write(line + "\n")


def load(path):
	try:
		data = json.loads(Path(path).read_text())
		return data if isinstance(data, dict) else {}
	except (OSError, ValueError):
		return {}


def save(path, data):
	tmp = Path(f"{path}.tmp")
	tmp.write_text(json.dumps(data))
	os.replace(tmp, path)


def discovery_ports(since):
	"""since 之后写出的 discovery 档里的埠（只读 port，不读 token）。"""
	ports = []
	for path in DISCOVERY.glob("instance-*.json"):
		try:
			if path.stat().st_mtime < since:
				continue
			port = json.loads(path.read_text()).get("port")
		except (OSError, ValueError, AttributeError):
			continue
		if isinstance(port, int):
			ports.append(port)
	return ports


def wait_ready(started):
	deadline = started + READY_TIMEOUT
	while time.time() < deadline:
		for port in discovery_ports(started - 1):
			try:
				with socket.create_connection(("127.0.0.1", port), timeout=1):
					return time.time() - started
			except OSError:
				pass
		time.sleep(0.5)
	return None


def restart():
	started = time.time()
	# 一起列：krita-gate Requires= krita，先停 gate 再停 Krita、先起 Krita 再起 gate（After=）。
	done = systemctl("restart", "krita.service", "krita-gate.service")
	if done.returncode != 0:
		log(f"restart 失败：{done.stderr.strip()}")
		return 1
	ready = wait_ready(started)
	if ready is None:
		log(f"已重启，但 {READY_TIMEOUT} 秒内没看到 Krita 桥接就绪，请检查 journalctl -u krita")
		return 1
	anon = keyed(Path(f"{CG}/krita.service/memory.stat").read_text(), "anon")
	log(f"完成：Krita 桥接 {ready:.1f} 秒后就绪，anon {anon // 1048576 if anon else '?'}MB，"
	    f"gate={show('krita-gate.service', 'ActiveState')}")
	return 0


def main():
	parser = argparse.ArgumentParser()
	parser.add_argument("--dry-run", action="store_true")
	args = parser.parse_args()
	now = time.time()
	krita_active = show("krita.service", "ActiveState") == "active"
	gate_active = show("krita-gate.service", "ActiveState") == "active"
	state = track(load(STATE), show("krita-gate.service", "MainPID"), gate_usage(), now)
	if not args.dry_run:
		save(STATE, state)
	anon = keyed(Path(f"{CG}/krita.service/memory.stat").read_text(), "anon") if krita_active else None
	ok, why = decide(
		krita_active=krita_active,
		gate_active=gate_active,
		idle_seconds=now - state["changed_at"],
		anon_mb=None if anon is None else anon // 1048576,
		uptime_h=uptime_hours("krita.service"),
		local_hour=datetime.now(TAIPEI).hour,
	)
	print(why, flush=True)
	if not ok:
		return 0
	if args.dry_run:
		print(f"（--dry-run）会重启 Krita；还要再看 {FINAL_CHECK} 秒确认没人在画", flush=True)
		return 0
	before = gate_usage()
	time.sleep(FINAL_CHECK)
	if gate_usage() != before or gate_connections():
		log(f"本来要重启（{why}），但最后 {FINAL_CHECK} 秒 gate 有动静，这次不动")
		save(STATE, track({}, show("krita-gate.service", "MainPID"), gate_usage(), time.time()))
		return 0
	log(f"开始重启 Krita：{why}")
	return restart()


if __name__ == "__main__":
	sys.exit(main())
