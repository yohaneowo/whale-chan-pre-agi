#!/usr/bin/env python3
"""闲置时重启 Krita，把它越长越大的记忆体清掉。由 krita-idle-restart.timer 每 10 分钟以 root 跑一次。

要同时满足才重启：
- krita.service 和 krita-gate.service 都在跑，而且 guard 的状态（GUARD_STATE 的 mode）是 watching：被 guard
  停住、锁住，或有人手动停掉时不碰；guard 的状态档读不出来也不碰；
- 闲置：krita-gate 的 CPU 用量（cgroup cpu.stat 的 usage_usec）至少 IDLE_MINUTES 分钟没变过。gate
  没请求时 CPU 用量完全不动（2026-11-28 量 60 秒是 0），每一笔画图请求都会让它变，所以这比看日志准；
- 而且下面任一项：
  - Krita 的匿名记忆体（memory.stat 的 anon，约等于它自己长出来的部分）≥ ANON_LIMIT_MB；
  - 每天的闲置时段（台北时间 DAILY_HOURS 点）而且 Krita 已经连续跑了 DAILY_MIN_UPTIME_H 小时以上。

重启前的最后确认（避免刚好有人开始画，也避免把 guard 刚停掉的服务拉起来）：
1. 再看 FINAL_CHECK 秒：gate 的 CPU 用量没变、socket 上没有连线、没有进行中的操作记号（gate 的 stuck.json）。
2. 拿和 guard 共用的互锁（LOCK 的 flock，最多等 LOCK_WAIT 秒）。拿着锁的时候 guard 不会停、也不会开 Krita。
3. 锁里再确认一次 guard 的 mode 是 watching、两个服务都还在跑。
4. 「关门」：把 gate.sock 暂时改成 0600，dsh 就连不进来（新的请求会马上被拒，不会画到一半被打断）。
   等 SETTLE 秒，再看一次第 1 项（CPU 用量从第 1 项开始算都没变）。有动静就把门开回来，这次不动。
5. 用 `systemctl try-restart`（只重启正在跑的，不会启动停着的）重启，放开锁。gate 重启时会重建 0660 的
   socket；不管成功与否、或被 systemd 收掉（SIGTERM），都会把门开回来。

每个作品请求都是开档、存档、关档（works_gate 的 document()），请求之间重启不会弄丢作品。
重启后等到 Krita 的桥接写出新的 discovery 档、埠可以连上，把花的秒数写进 /var/log/krita-guard.log。
`--dry-run` 只印判断，不重启。
"""

import argparse
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

IDLE_MINUTES = 30
FINAL_CHECK = 20
SETTLE = 2
LOCK_WAIT = 30
ANON_LIMIT_MB = 1100
DAILY_HOURS = {4}
DAILY_MIN_UPTIME_H = 20
READY_TIMEOUT = 180
TAIPEI = timezone(timedelta(hours=8))  # 台北没有夏令时间，不必依赖 tzdata

CG = "/sys/fs/cgroup/system.slice"
STATE = "/var/lib/krita-guard/idle.json"
GUARD_STATE = "/var/lib/krita-guard/state.json"
LOCK = "/var/lib/krita-guard/interlock.lock"
LOG = "/var/log/krita-guard.log"
DISCOVERY = Path("/var/lib/krita/.local/state/krita6-mcp")
GATE_SOCKET = "/run/krita-gate/gate.sock"
MARKER = "/var/lib/krita/gate/stuck.json"


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


def decide(*, krita_active, gate_active, idle_seconds, anon_mb, uptime_h, local_hour, guard_mode="watching"):
	"""回 (要不要重启, 原因)。纯判断，方便测试。"""
	if guard_mode != "watching":
		return False, f"guard 的状态是 {guard_mode}（停住或读不出来），不动"
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


def active(unit):
	return show(unit, "ActiveState") == "active"


def gate_usage():
	try:
		return keyed(Path(f"{CG}/krita-gate.service/cpu.stat").read_text(), "usage_usec")
	except OSError:
		return None


def gate_connections():
	"""gate socket 上已建立的连线数（ss 列出来的 u_str ESTAB，本地端是 gate.sock）。"""
	out = subprocess.run(["ss", "-xH", "state", "connected"], capture_output=True, text=True).stdout
	return sum(1 for line in out.splitlines() if GATE_SOCKET in line)


def guard_mode():
	"""guard 状态档里的 mode；没有状态档（guard 还没停过）算 watching，读不出来回 None（当成停住）。"""
	try:
		data = json.loads(Path(GUARD_STATE).read_text())
	except FileNotFoundError:
		return "watching"
	except (OSError, ValueError):
		return None
	return data.get("mode") if isinstance(data, dict) else None


def activity(usage_before):
	"""gate 现在有没有人在用；有就回原因，没有回空字串。"""
	if gate_usage() != usage_before:
		return "gate 有 CPU 用量"
	count = gate_connections()
	if count:
		return f"gate socket 上有 {count} 个连线"
	if os.path.lexists(MARKER):
		return "gate 有进行中的操作记号（stuck.json）"
	return ""


@contextmanager
def interlock(path, timeout):
	"""和 krita_guard.py 共用的互锁（flock）。yield 有没有拿到。"""
	import fcntl  # noqa: PLC0415 - 只在 Linux 上跑

	fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
	got = False
	try:
		deadline = time.monotonic() + timeout
		while True:
			try:
				fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
				got = True
				break
			except BlockingIOError:
				if time.monotonic() >= deadline:
					break
				time.sleep(0.2)
		yield got
	finally:
		os.close(fd)  # 关掉就放开 flock


def set_door(mode):
	"""改 gate.sock 的权限。/run/krita-gate 归 krita，所以不跟随连结：先用 O_PATH|O_NOFOLLOW 开，确认是
	socket，再经由 /proc/self/fd 改那个 inode。不是 socket（或不在）就回 False，什么都不改。"""
	try:
		fd = os.open(GATE_SOCKET, os.O_PATH | os.O_NOFOLLOW)
	except OSError:
		return False
	try:
		if not stat.S_ISSOCK(os.fstat(fd).st_mode):
			return False
		os.chmod(f"/proc/self/fd/{fd}", mode)
		return True
	except OSError:
		return False
	finally:
		os.close(fd)


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


def restart_locked(why, usage_before):
	"""拿着互锁做最后确认和重启。回 (重启的结果或 None, 开始时间)；None 表示这次不动（已写日志）。"""
	mode = guard_mode()
	if mode != "watching":
		log(f"本来要重启（{why}），但 guard 的状态是 {mode}（停住或读不出来），这次不动")
		return None, None
	if not (active("krita.service") and active("krita-gate.service")):
		log(f"本来要重启（{why}），但 Krita 或 krita-gate 已经没在跑，这次不动")
		return None, None
	if not set_door(0o600):
		log(f"本来要重启（{why}），但关不了 gate 的门（{GATE_SOCKET} 不在或不是 socket），这次不动")
		return None, None
	try:
		time.sleep(SETTLE)
		busy = activity(usage_before)
		if busy:
			log(f"本来要重启（{why}），但关门后再看一次：{busy}，这次不动")
			save(STATE, track({}, show("krita-gate.service", "MainPID"), gate_usage(), time.time()))
			return None, None
		log(f"开始重启 Krita：{why}")
		started = time.time()
		# 一起列：krita-gate Requires= krita，先停 gate 再停 Krita、先起 Krita 再起 gate（After=）。
		# try-restart 只重启正在跑的，就算有什么漏网之鱼先停了它们，也不会被这里拉起来。
		return systemctl("try-restart", "krita.service", "krita-gate.service"), started
	finally:
		set_door(0o660)  # 重启成功时 gate 已经重建 0660 的 socket，这里只是保险；没重启就是把门开回来


def main():
	parser = argparse.ArgumentParser()
	parser.add_argument("--dry-run", action="store_true")
	args = parser.parse_args()
	now = time.time()
	krita_active = active("krita.service")
	gate_active = active("krita-gate.service")
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
		guard_mode=guard_mode(),
	)
	print(why, flush=True)
	if not ok:
		return 0
	if args.dry_run:
		print(f"（--dry-run）会重启 Krita；还要再看 {FINAL_CHECK} 秒、拿互锁、关门再确认没人在画", flush=True)
		return 0
	before = gate_usage()
	time.sleep(FINAL_CHECK)
	busy = activity(before)
	if busy:
		log(f"本来要重启（{why}），但最后 {FINAL_CHECK} 秒{busy}，这次不动")
		save(STATE, track({}, show("krita-gate.service", "MainPID"), gate_usage(), time.time()))
		return 0
	with interlock(LOCK, LOCK_WAIT) as got:
		if not got:
			log(f"本来要重启（{why}），但 {LOCK_WAIT} 秒拿不到互锁（guard 正在停或开 Krita），这次不动")
			return 0
		done, started = restart_locked(why, before)
	if done is None:
		return 0
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


if __name__ == "__main__":
	# 被 systemd 收掉（TimeoutStartSec）时也要跑 finally，把 gate 的门开回来、放开互锁。
	signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
	sys.exit(main())
