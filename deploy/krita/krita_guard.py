#!/usr/bin/env python3
"""krita-guard：保护 dsh（小鲸鱼），Krita 拖累她时先停 Krita，压力退了再自动开回来。

由 krita-works-guard.service 以 root 常驻执行，每 TICK 秒取样一次，写一行到 /var/log/krita-guard.log。

停 Krita（krita.service，连带 krita-gate.service）的条件：
- 立刻停：dsh 不是 active、dsh 被 systemd 自动重启过（NRestarts 变了）、dsh 的 cgroup 有 OOM kill。
- 连续超过门槛 TRIP_SECONDS 秒才停（单一次尖峰不算）：
  - dsh 自己的记忆体压力（cgroup memory.pressure 的 some avg10）> DSH_MEM_LIMIT %；
  - 整台的 CPU 压力 some avg10 > CPU_LIMIT %；
  - 整台的 MemAvailable < AVAIL_MIN_MB。
- 整台和 krita 自己的记忆体压力只写进日志，不拿来判断：Krita 顶到自己的 MemoryHigh 时，压力在它
  自己的区块里，整台的数字会跟着跳，但 dsh 没受影响（2026-11-28 两次误停都是这样）。
- Krita 和 krita-gate 都没在跑时什么都不停（例如有人手动停了、或闲置重启中），只更新基准。

自动恢复：被 guard 停掉以后，dsh active、dsh 记忆体压力回到 0%（avg10 < CALM_PSI）、可用记忆体
够、CPU 压力不高，连续 RECOVER_SECONDS 秒，就 `systemctl start krita-gate.service`（Requires= 会
带起 krita.service），并记一笔 RECOVER。保护条件：TRIP_WINDOW 秒内因压力或 OOM 被停满
MAX_TRIPS 次，就不再自动恢复（LOCKED），要人手动 `systemctl start krita-gate.service`。
停住时如果有人手动把 krita-gate 开回来，guard 记一笔 MANUAL，回到监看。

状态（被停的原因、时间、近期次数）存在 STATE_FILE，guard 自己重启后还记得要恢复。
测试时可以用环境变数换掉读取的档案和 systemctl（见 Config）。
"""

import json
import os
import subprocess
import sys
import time
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

	def stop_krita(self):
		# 先列 krita.service：krita-gate Requires= 它，同一个 job 里两个一起停，真的把 Krita 的记忆体放掉。
		# guard 不再 PartOf=/Requires= krita-gate，停 gate 不会把 guard 自己一起收掉。
		first = self.systemctl("stop", "krita.service", "krita-gate.service")
		self.systemctl("stop", "krita-probe-*.service")
		return first.returncode == 0, first.stderr.strip()

	def start_gate(self):
		done = self.systemctl("start", "krita-gate.service")
		return done.returncode == 0, done.stderr.strip()


class Guard:
	"""判断逻辑，不碰系统：给它取样和时间，它回要做的事。"""

	def __init__(self, state=None):
		state = state or {}
		self.mode = state.get("mode", "watching")
		self.reason = state.get("reason", "")
		self.trips = [float(t) for t in state.get("trips", [])]
		self.tripped_at = state.get("tripped_at")
		self.base_restarts = None
		self.base_ooms = None
		self.over_since = {}
		self.calm_since = None

	def state(self):
		return {"mode": self.mode, "reason": self.reason, "trips": self.trips, "tripped_at": self.tripped_at}

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
		hard = None
		if not sample.dsh_active:
			hard = ("dsh", "dsh 不是 active")
		elif sample.dsh_restarts != self.base_restarts:
			hard = ("dsh", f"dsh 被自动重启（NRestarts {self.base_restarts} → {sample.dsh_restarts}）")
		elif sample.dsh_ooms != self.base_ooms:
			hard = ("oom", "dsh 的 cgroup 有 OOM kill")
		actions = []
		for key, bad in self.over(sample).items():
			if not bad:
				self.over_since.pop(key, None)
			elif key not in self.over_since:
				self.over_since[key] = now
				actions.append(("log", f"超过门槛，开始计时：{self.describe(sample, key)}"))
		lasting = [k for k, t in self.over_since.items() if now - t >= TRIP_SECONDS]
		if hard is None and lasting:
			key = lasting[0]
			hard = (key, f"{self.describe(sample, key)}，已持续 {now - self.over_since[key]:.0f} 秒")
		if hard is None:
			return actions
		kind, reason = hard
		if kind in COUNTED:
			self.trips = self.recent_trips(now) + [now]
		self.mode, self.reason, self.tripped_at, self.calm_since = "tripped", reason, now, None
		self.over_since = {}
		return actions + [("stop", reason)]

	def wait(self, sample, now):
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
		count = len(self.recent_trips(now))
		if count >= MAX_TRIPS:
			self.mode = "locked"
			return [("log", f"LOCKED: {TRIP_WINDOW // 60} 分钟内因压力被停 {count} 次，不再自动恢复；"
			                "确认没问题后手动 systemctl start krita-gate.service")]
		reason = self.reason
		self.mode, self.reason, self.calm_since = "watching", "", None
		self.rebase(sample)
		return [("start", f"RECOVER: 压力回到 0% 已 {RECOVER_SECONDS} 秒，开回 krita-gate（上次停的原因：{reason}）")]

	def start_failed(self, now):
		self.mode, self.reason, self.calm_since = "tripped", "自动恢复时 start 失败", None
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
	log(f"guard start: mode={guard.mode} dsh restarts={first.dsh_restarts} ooms={first.dsh_ooms} "
	    f"avail={first.avail_mb}MB（只看 dsh 自己的记忆体压力，持续 {TRIP_SECONDS} 秒才停，"
	    f"压力回 0% {RECOVER_SECONDS} 秒后自动恢复）")
	while True:
		sample = system.sample()
		now = time.time()
		before = guard.state()
		log(line(sample, guard))
		for action, message in guard.step(sample, now):
			if action == "stop":
				log(f"STOP: {message}")
				ok, err = system.stop_krita()
				if not ok:
					log(f"STOP 失败：{err}")
			elif action == "start":
				log(message)
				ok, err = system.start_gate()
				if not ok:
					log(f"RECOVER 失败：{err}")
					guard.start_failed(now)
			else:
				log(message)
		if guard.state() != before:
			save_state(config.state, guard.state())
		time.sleep(config.tick)


if __name__ == "__main__":
	sys.exit(main())
