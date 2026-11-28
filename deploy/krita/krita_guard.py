#!/usr/bin/env python3
"""krita-guard：保护 dsh（小鲸鱼），Krita 拖累她时先停 Krita，压力退了再自动开回来。

由 krita-works-guard.service 以 root 常驻执行，每 TICK 秒取样一次，写一行到 /var/log/krita-guard.log。

停 Krita（krita.service，连带 krita-gate.service）的条件：
- 立刻停：dsh 不是 active、dsh 被 systemd 自动重启过（NRestarts 变了）、dsh 有 OOM kill。
  OOM 要看两个地方：dsh cgroup 的 memory.events（oom_kill 变大），以及 systemd 自己（_PID=1）对 dsh.service
  写的日志里有没有 oom-kill。后者是因为 dsh 被 OOM kill 后 systemd 会把它停掉再自动重启，cgroup 重建，
  oom_kill 计数器回到 0，光看计数器会漏掉；所以 dsh 不是 active 或 NRestarts 变了的时候才去查日志。
- 连续超过门槛 TRIP_SECONDS 秒才停（单一次尖峰不算）：
  - dsh 自己的记忆体压力（cgroup memory.pressure 的 some avg10）> DSH_MEM_LIMIT %；
  - 整台的 CPU 压力 some avg10 > CPU_LIMIT %；
  - 整台的 MemAvailable < AVAIL_MIN_MB。
- 整台和 krita 自己的记忆体压力只写进日志，不拿来判断：Krita 顶到自己的 MemoryHigh 时，压力在它
  自己的区块里，整台的数字会跟着跳，但 dsh 没受影响（2026-11-28 两次误停都是这样）。
- Krita 和 krita-gate 都没在跑时什么都不停（例如有人手动停了、或闲置重启中），只更新基准。

停了以后要亲眼看到 krita 和 krita-gate 都不是 active，才算「已停住」（state 的 down）。还没看到就每一轮
再停一次（STOP 重试）：systemctl stop 失败、或自动恢复 start 到一半失败留下半开的服务时，都不会被误认成
「有人手动开回来」，一次性的 OOM 也不会因此被忘掉。

自动恢复：已停住以后，dsh active、dsh 记忆体压力回到 0%（avg10 < CALM_PSI）、可用记忆体够、CPU
压力不高，连续 RECOVER_SECONDS 秒，就 `systemctl start krita-gate.service`（Requires= 会带起
krita.service），并记一笔 RECOVER。保护条件：以「停的那一刻」为准，往前 TRIP_WINDOW 秒内因压力或
OOM 被停满 MAX_TRIPS 次（含这一次），这次就直接锁住（LOCKED），不再自动恢复，要人手动
`systemctl start krita-gate.service`。OOM 和 dsh 自动重启同时发生（OOM kill 后 systemd 马上重启）也算。
已停住时如果有人手动把 krita-gate 开回来，guard 记一笔 MANUAL，回到监看（近期次数保留，同一小时内
再被停就又锁住）。

和闲置重启（krita_idle_restart.py）互锁：guard 每次 stop / start 之前先拿 LOCK_FILE 的 flock，拿到以后
先把状态写进 STATE_FILE 才动 systemctl。闲置重启也要先拿同一把锁、确认 STATE_FILE 的 mode 是
watching 才会重启，所以 guard 停住的时候它不会去启动任何东西。闲置重启只在最后确认和
`systemctl try-restart` 那一段拿着锁；guard 最多等 LOCK_WAIT 秒，真的等不到（不该发生）就照样停，
保护 dsh 优先。

状态（被停的原因、时间、近期次数）存在 STATE_FILE，guard 自己重启后还记得要恢复。
测试时可以用环境变数换掉读取的档案和 systemctl（见 Config）。
"""

import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

TICK = 5
TRIP_SECONDS = 15
RECOVER_SECONDS = 90
MAX_TRIPS = 3
TRIP_WINDOW = 3600
DSH_MEM_LIMIT = 20.0
CPU_LIMIT = 60.0
AVAIL_MIN_MB = 450
CALM_PSI = 1.0
LOCK_WAIT = 330  # 闲置重启 service 的 TimeoutStartSec 是 300 秒，被 systemd 收掉时锁一定会放开

# 这几种停是「压力类」，算进 LOCKED 的次数；dsh 重启或停掉通常是部署，不算。
COUNTED = {"dsh_mem", "cpu", "avail", "oom"}
LABELS = {
	"dsh_mem": "dsh 记忆体压力",
	"cpu": "CPU 压力",
	"avail": "可用记忆体不足",
}


class Config:
	def __init__(self, env=None):
		env = os.environ if env is None else env
		cg = env.get("KRITA_GUARD_CGROUP_ROOT", "/sys/fs/cgroup/system.slice")
		proc = env.get("KRITA_GUARD_PROC_ROOT", "/proc")
		self.cgroup = cg
		self.dsh_psi = env.get("KRITA_GUARD_DSH_PSI_FILE", f"{cg}/dsh.service/memory.pressure")
		self.krita_psi = f"{cg}/krita.service/memory.pressure"
		self.sys_mem_psi = f"{proc}/pressure/memory"
		self.sys_cpu_psi = f"{proc}/pressure/cpu"
		self.meminfo = f"{proc}/meminfo"
		self.log = env.get("KRITA_GUARD_LOG", "/var/log/krita-guard.log")
		self.state = env.get("KRITA_GUARD_STATE_FILE", "/var/lib/krita-guard/state.json")
		self.systemctl = env.get("KRITA_GUARD_SYSTEMCTL", "systemctl")
		self.tick = float(env.get("KRITA_GUARD_TICK", TICK))
		self.lock = env.get("KRITA_GUARD_LOCK", "/var/lib/krita-guard/interlock.lock")
		self.journalctl = env.get("KRITA_GUARD_JOURNALCTL", "journalctl")


def read_text(path):
	try:
		with open(path, encoding="utf-8") as stream:
			return stream.read()
	except OSError:
		return None


def psi_avg10(text):
	"""PSI 档案里 some 那行的 avg10（百分比，浮点）；读不到回 None。"""
	for line in (text or "").splitlines():
		if line.startswith("some "):
			for field in line.split()[1:]:
				key, _, value = field.partition("=")
				if key == "avg10":
					try:
						return float(value)
					except ValueError:
						return None
	return None


def keyed(text, key):
	"""「key value」格式（memory.events、memory.stat、cpu.stat）里的整数；读不到回 None。"""
	for line in (text or "").splitlines():
		parts = line.split()
		if len(parts) >= 2 and parts[0] == key:
			try:
				return int(parts[1])
			except ValueError:
				return None
	return None


def mem_available_mb(text):
	for line in (text or "").splitlines():
		if line.startswith("MemAvailable:"):
			try:
				return int(line.split()[1]) // 1024
			except (IndexError, ValueError):
				return None
	return None


def mb(text):
	try:
		return int(text.strip()) // 1048576
	except (AttributeError, ValueError):
		return None


@dataclass
class Sample:
	dsh_active: bool
	dsh_restarts: str
	dsh_ooms: int
	avail_mb: int | None
	dsh_mem: float | None
	cpu: float | None
	sys_mem: float | None = None
	krita_mem: float | None = None
	krita_active: bool = True
	gate_active: bool = True
	dsh_peak_mb: int | None = None
	krita_peak_mb: int | None = None
	krita_anon_mb: int | None = None
	dsh_oom_logged: bool = False  # systemd 日志里 dsh 有 oom-kill（只在 dsh 停了或被自动重启时才查）


class System:
	"""真的去读 /proc、cgroup，呼叫 systemctl。"""

	def __init__(self, config):
		self.config = config

	def systemctl(self, *args):
		return subprocess.run([self.config.systemctl, *args], capture_output=True, text=True, timeout=120)

	def active(self, unit):
		return self.systemctl("is-active", unit).stdout.strip() == "active"

	def sample(self):
		c = self.config
		anon = keyed(read_text(f"{c.cgroup}/krita.service/memory.stat"), "anon")
		return Sample(
			dsh_active=self.active("dsh.service"),
			dsh_restarts=self.systemctl("show", "dsh.service", "-p", "NRestarts", "--value").stdout.strip(),
			dsh_ooms=keyed(read_text(f"{c.cgroup}/dsh.service/memory.events"), "oom_kill") or 0,
			avail_mb=mem_available_mb(read_text(c.meminfo)),
			dsh_mem=psi_avg10(read_text(c.dsh_psi)),
			cpu=psi_avg10(read_text(c.sys_cpu_psi)),
			sys_mem=psi_avg10(read_text(c.sys_mem_psi)),
			krita_mem=psi_avg10(read_text(c.krita_psi)),
			krita_active=self.active("krita.service"),
			gate_active=self.active("krita-gate.service"),
			dsh_peak_mb=mb(read_text(f"{c.cgroup}/dsh.service/memory.peak")),
			krita_peak_mb=mb(read_text(f"{c.cgroup}/krita.service/memory.peak")),
			krita_anon_mb=None if anon is None else anon // 1048576,
		)

	def oom_logged(self, since):
		"""since（epoch 秒）之后 systemd 有没有记下 dsh.service 被 OOM kill。只看 _PID=1 写的，dsh 自己的输出不算。"""
		try:
			out = subprocess.run([self.config.journalctl, "-q", "--no-pager", "-o", "cat", "_PID=1", "UNIT=dsh.service",
			                      "--since", f"@{int(since)}"], capture_output=True, text=True, timeout=30).stdout
		except (OSError, subprocess.SubprocessError):
			return False
		return any("oom-kill" in line or "OOM killer" in line for line in out.splitlines())

	def stop_krita(self):
		# 先列 krita.service：krita-gate Requires= 它，同一个 job 里两个一起停，真的把 Krita 的记忆体放掉。
		# guard 不再 PartOf=/Requires= krita-gate，停 gate 不会把 guard 自己一起收掉。
		first = self.systemctl("stop", "krita.service", "krita-gate.service")
		self.systemctl("stop", "krita-probe-*.service")
		return first.returncode == 0, first.stderr.strip()

	def start_gate(self):
		done = self.systemctl("start", "krita-gate.service")
		return done.returncode == 0, done.stderr.strip()


@contextmanager
def interlock(path, timeout, sleep=time.sleep, clock=time.monotonic):
	"""和 krita_idle_restart.py 共用的互锁（flock）。yield 有没有拿到；timeout 秒内拿不到就 yield False。"""
	try:
		import fcntl  # noqa: PLC0415 - 只有 Linux 有；在 Windows 跑单元测试时没有互锁
	except ImportError:
		yield True
		return
	fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
	got = False
	try:
		deadline = clock() + timeout
		while True:
			try:
				fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
				got = True
				break
			except BlockingIOError:
				if clock() >= deadline:
					break
				sleep(0.2)
		yield got
	finally:
		os.close(fd)  # 关掉就放开 flock


class Guard:
	"""判断逻辑，不碰系统：给它取样和时间，它回要做的事。"""

	def __init__(self, state=None):
		state = state or {}
		self.mode = state.get("mode", "watching")
		self.reason = state.get("reason", "")
		self.trips = [float(t) for t in state.get("trips", [])]
		self.tripped_at = state.get("tripped_at")
		# 旧版状态档没有 down：当成还没看到停住，先确认一次（都停着就马上算停住）。
		self.down = bool(state.get("down", False))
		self.base_restarts = None
		self.base_ooms = None
		self.over_since = {}
		self.calm_since = None

	def state(self):
		return {"mode": self.mode, "reason": self.reason, "trips": self.trips, "tripped_at": self.tripped_at,
		        "down": self.down}

	def rebase(self, sample):
		self.base_restarts = sample.dsh_restarts
		self.base_ooms = sample.dsh_ooms
		self.over_since = {}

	def recent_trips(self, now):
		return [t for t in self.trips if now - t < TRIP_WINDOW]

	@staticmethod
	def over(sample):
		return {
			"dsh_mem": sample.dsh_mem is not None and sample.dsh_mem > DSH_MEM_LIMIT,
			"cpu": sample.cpu is not None and sample.cpu > CPU_LIMIT,
			"avail": sample.avail_mb is not None and sample.avail_mb < AVAIL_MIN_MB,
		}

	@staticmethod
	def calm(sample):
		return (
			sample.dsh_active
			and sample.dsh_mem is not None
			and sample.dsh_mem < CALM_PSI
			and (sample.avail_mb is None or sample.avail_mb >= AVAIL_MIN_MB)
			and (sample.cpu is None or sample.cpu <= CPU_LIMIT)
		)

	def step(self, sample, now):
		"""回 [(动作, 讯息)]，动作是 log / stop / start。"""
		if self.base_restarts is None:
			self.rebase(sample)
		if self.mode == "watching":
			return self.watch(sample, now)
		return self.wait(sample, now)

	def watch(self, sample, now):
		if not (sample.krita_active or sample.gate_active):
			self.rebase(sample)
			return []
		# 同一笔取样可能同时有好几个原因（例如 OOM kill 后 systemd 马上重启 dsh：oom_kill 和 NRestarts
		# 一起变）。全部列出来，只要有一个是压力类就算进锁住的次数。
		causes = []
		if sample.dsh_ooms < self.base_ooms:
			self.base_ooms = sample.dsh_ooms  # dsh 重启过，cgroup 重建，计数器从 0 重新算
		if sample.dsh_ooms > self.base_ooms:
			causes.append(("oom", "dsh 的 cgroup 有 OOM kill"))
		elif sample.dsh_oom_logged:
			causes.append(("oom", "systemd 日志：dsh 被 OOM kill"))
		if not sample.dsh_active:
			causes.append(("dsh", "dsh 不是 active"))
		elif sample.dsh_restarts != self.base_restarts:
			causes.append(("dsh", f"dsh 被自动重启（NRestarts {self.base_restarts} → {sample.dsh_restarts}）"))
		actions = []
		for key, bad in self.over(sample).items():
			if not bad:
				self.over_since.pop(key, None)
			elif key not in self.over_since:
				self.over_since[key] = now
				actions.append(("log", f"超过门槛，开始计时：{self.describe(sample, key)}"))
		for key, since in self.over_since.items():
			if now - since >= TRIP_SECONDS:
				causes.append((key, f"{self.describe(sample, key)}，已持续 {now - since:.0f} 秒"))
		if not causes:
			return actions
		return actions + self.trip(causes, now)

	def trip(self, causes, now):
		"""停的那一刻：记下原因，压力类的算进次数；含这一次满 MAX_TRIPS 次就直接锁住。"""
		reason = "；".join(text for _, text in causes)
		self.mode = "tripped"
		actions = [("stop", reason)]
		if any(kind in COUNTED for kind, _ in causes):
			self.trips = self.recent_trips(now) + [now]
			if len(self.trips) >= MAX_TRIPS:
				self.mode = "locked"
				actions.append(("log", f"LOCKED: {TRIP_WINDOW // 60} 分钟内因压力被停 {len(self.trips)} 次（含这次），"
				                       "不再自动恢复；确认没问题后手动 systemctl start krita-gate.service"))
		self.reason, self.tripped_at, self.calm_since, self.down = reason, now, None, False
		self.over_since = {}
		return actions

	def wait(self, sample, now):
		if not self.down:
			if sample.krita_active or sample.gate_active:
				# 还没看到真的停住（stop 失败、还在停、或 start 失败留下半开的）：再停一次，不当成手动开回来。
				self.calm_since = None
				return [("stop", f"STOP 重试：还没停住（krita={'active' if sample.krita_active else 'inactive'}，"
				                 f"gate={'active' if sample.gate_active else 'inactive'}），原因：{self.reason}")]
			self.down = True
		if sample.gate_active:
			mode = self.mode
			self.mode, self.reason, self.calm_since = "watching", "", None
			self.rebase(sample)
			return [("log", f"MANUAL: krita-gate 已经在跑（手动或开机启动），从 {mode} 回到监看")]
		if not self.calm(sample):
			self.calm_since = None
			return []
		if self.calm_since is None:
			self.calm_since = now
		if now - self.calm_since < RECOVER_SECONDS or self.mode == "locked":
			return []
		reason = self.reason
		self.mode, self.reason, self.calm_since = "watching", "", None
		self.rebase(sample)
		return [("start", f"RECOVER: 压力回到 0% 已 {RECOVER_SECONDS} 秒，开回 krita-gate（上次停的原因：{reason}）")]

	def start_failed(self, now):
		# start 可能做到一半（krita 起来、gate 没起来）：回到「还没停住」，下一轮先把半开的停掉，再重新等。
		self.mode, self.reason, self.calm_since, self.down = "tripped", "自动恢复时 start 失败", None, False
		self.tripped_at = now

	@staticmethod
	def describe(sample, key):
		if key == "dsh_mem":
			return f"{LABELS[key]} {sample.dsh_mem:.2f}% > {DSH_MEM_LIMIT:.0f}%"
		if key == "cpu":
			return f"{LABELS[key]} {sample.cpu:.2f}% > {CPU_LIMIT:.0f}%"
		return f"{LABELS[key]} {sample.avail_mb}MB < {AVAIL_MIN_MB}MB"


def pct(value):
	return "?" if value is None else f"{value:.2f}%"


def line(sample, guard):
	# 前半段沿用旧版格式（psi_mem 是整台的记忆体压力、取整数），旧的统计脚本还能读。
	whole = "?" if sample.sys_mem is None else f"{int(sample.sys_mem)}%"
	cpu = "?" if sample.cpu is None else f"{int(sample.cpu)}%"
	return (f"avail={sample.avail_mb}MB psi_mem={whole} psi_cpu={cpu} "
	        f"dsh={'active' if sample.dsh_active else 'inactive'}/{sample.dsh_peak_mb}MBpeak "
	        f"krita={'active' if sample.krita_active else 'inactive'}/{sample.krita_peak_mb}MBpeak "
	        f"dsh_psi_mem={pct(sample.dsh_mem)} krita_psi_mem={pct(sample.krita_mem)} "
	        f"krita_anon={sample.krita_anon_mb}MB gate={'active' if sample.gate_active else 'inactive'} mode={guard.mode}")


def load_state(path):
	try:
		with open(path, encoding="utf-8") as stream:
			data = json.load(stream)
		return data if isinstance(data, dict) else {}
	except (OSError, ValueError):
		return {}


def save_state(path, state):
	tmp = f"{path}.tmp"
	with open(tmp, "w", encoding="utf-8") as stream:
		json.dump(state, stream)
	os.replace(tmp, path)


def main():
	config = Config()
	system = System(config)
	guard = Guard(load_state(config.state))

	def log(message):
		with open(config.log, "a", encoding="utf-8") as stream:
			stream.write(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {message}\n")

	first = system.sample()
	last = time.time()
	log(f"guard start: mode={guard.mode} dsh restarts={first.dsh_restarts} ooms={first.dsh_ooms} "
	    f"avail={first.avail_mb}MB（只看 dsh 自己的记忆体压力，持续 {TRIP_SECONDS} 秒才停，"
	    f"压力回 0% {RECOVER_SECONDS} 秒后自动恢复）")
	while True:
		sample = system.sample()
		now = time.time()
		if guard.mode == "watching" and guard.base_restarts is not None and (
				not sample.dsh_active or sample.dsh_restarts != guard.base_restarts):
			sample.dsh_oom_logged = system.oom_logged(last - 2 * config.tick)
		last = now
		before = guard.state()
		log(line(sample, guard))
		for action, message in guard.step(sample, now):
			if action == "stop" and not message.startswith("STOP"):
				message = f"STOP: {message}"
			log(message)
			if action == "log":
				continue
			with interlock(config.lock, LOCK_WAIT) as got:
				if not got:
					log(f"等互锁 {LOCK_WAIT} 秒还拿不到，照样执行")
				# 先写状态再动 systemctl：闲置重启拿到锁时看到的一定是最新的 mode。
				save_state(config.state, guard.state())
				if action == "stop":
					ok, err = system.stop_krita()
					if not ok:
						log(f"STOP 失败（下一轮再停）：{err}")
				else:
					ok, err = system.start_gate()
					if not ok:
						log(f"RECOVER 失败：{err}")
						guard.start_failed(now)
						save_state(config.state, guard.state())
		if guard.state() != before:
			save_state(config.state, guard.state())
		time.sleep(config.tick)


if __name__ == "__main__":
	sys.exit(main())
