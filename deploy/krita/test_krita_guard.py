"""krita_guard.py / krita_idle_restart.py 的测试：用假的 PSI 数字和假的 systemctl 跑判断。

在 deploy/krita 底下跑：python3 -m unittest test_krita_guard
"""

import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import krita_guard as kg
import krita_idle_restart as kr


def sample(**over):
	base = dict(dsh_active=True, dsh_restarts="0", dsh_ooms=0, avail_mb=3000, dsh_mem=0.0, cpu=0.0,
	            sys_mem=0.0, krita_mem=0.0, krita_active=True, gate_active=True)
	base.update(over)
	return kg.Sample(**base)


def run(guard, samples, start=0.0, tick=5.0):
	"""依序餵取样（每 tick 秒一笔），回 [(时间, 动作, 讯息)]。"""
	out = []
	for i, s in enumerate(samples):
		now = start + i * tick
		out += [(now, action, message) for action, message in guard.step(s, now)]
	return out


def kinds(events, kind):
	return [e for e in events if e[1] == kind]


class TripTest(unittest.TestCase):
	def test_single_spike_does_nothing(self):
		# 2026-11-28 13:21 的情况：一个取样跳到 22%，下一笔就退了。
		guard = kg.Guard()
		events = run(guard, [sample(), sample(dsh_mem=22.0), sample(dsh_mem=3.0), sample()])
		self.assertEqual(kinds(events, "stop"), [])
		self.assertEqual(guard.mode, "watching")

	def test_two_spikes_with_gap_do_nothing(self):
		guard = kg.Guard()
		seq = [sample(dsh_mem=25.0)] * 3 + [sample(dsh_mem=5.0)] + [sample(dsh_mem=25.0)] * 3
		self.assertEqual(kinds(run(guard, seq), "stop"), [])

	def test_sustained_15_seconds_stops(self):
		guard = kg.Guard()
		events = run(guard, [sample(dsh_mem=30.0)] * 4)
		stops = kinds(events, "stop")
		self.assertEqual(len(stops), 1)
		# 第一笔在 0 秒，15 秒那笔（第 4 笔）才停：3 笔（10 秒）还不够。
		self.assertEqual(stops[0][0], 15.0)
		self.assertIn("dsh 记忆体压力", stops[0][2])
		self.assertEqual(guard.mode, "tripped")

	def test_three_samples_are_not_enough(self):
		guard = kg.Guard()
		events = run(guard, [sample(dsh_mem=30.0)] * 3 + [sample()])
		self.assertEqual(kinds(events, "stop"), [])

	def test_only_dsh_pressure_counts(self):
		# 整台和 krita 自己的记忆体压力再高，只要 dsh 自己是 0，都不停。
		guard = kg.Guard()
		seq = [sample(sys_mem=40.0, krita_mem=60.0, dsh_mem=0.0)] * 20
		self.assertEqual(kinds(run(guard, seq), "stop"), [])

	def test_whole_machine_pressure_is_logged_only(self):
		s = sample(sys_mem=22.4, krita_mem=9.1, dsh_mem=0.0)
		text = kg.line(s, kg.Guard())
		self.assertIn("psi_mem=22%", text)
		self.assertIn("krita_psi_mem=9.10%", text)
		self.assertIn("dsh_psi_mem=0.00%", text)

	def test_sustained_low_memory_and_cpu_also_stop(self):
		self.assertEqual(len(kinds(run(kg.Guard(), [sample(avail_mb=300)] * 4), "stop")), 1)
		self.assertEqual(len(kinds(run(kg.Guard(), [sample(cpu=80.0)] * 4), "stop")), 1)
		self.assertEqual(kinds(run(kg.Guard(), [sample(cpu=80.0)] * 3), "stop"), [])

	def test_dsh_events_stop_at_once(self):
		for bad in (dict(dsh_active=False), dict(dsh_restarts="1"), dict(dsh_ooms=1)):
			guard = kg.Guard()
			events = run(guard, [sample(), sample(**bad)])
			self.assertEqual([e[0] for e in kinds(events, "stop")], [5.0], bad)

	def test_nothing_to_stop_when_krita_already_down(self):
		guard = kg.Guard()
		seq = [sample(krita_active=False, gate_active=False, dsh_mem=50.0)] * 10
		self.assertEqual(kinds(run(guard, seq), "stop"), [])
		# Krita 停着的时候 dsh 被重启过，之后 Krita 起来不该误判成「dsh 被重启」。
		events = run(guard, [sample(krita_active=False, gate_active=False, dsh_restarts="2"), sample(dsh_restarts="2")])
		self.assertEqual(kinds(events, "stop"), [])


class RecoverTest(unittest.TestCase):
	def tripped(self):
		guard = kg.Guard()
		run(guard, [sample(dsh_mem=30.0)] * 4)
		self.assertEqual(guard.mode, "tripped")
		return guard

	def test_recovers_after_90_seconds_calm(self):
		guard = self.tripped()
		down = dict(krita_active=False, gate_active=False)
		seq = [sample(dsh_mem=12.0, **down)] * 4 + [sample(dsh_mem=0.0, **down)] * 20
		events = run(guard, seq, start=100.0)
		starts = kinds(events, "start")
		self.assertEqual(len(starts), 1)
		# 第 5 笔（120 秒）开始是 0%，满 90 秒是 210 秒。
		self.assertEqual(starts[0][0], 210.0)
		self.assertIn("RECOVER", starts[0][2])
		self.assertEqual(guard.mode, "watching")

	def test_calm_must_be_continuous(self):
		guard = self.tripped()
		down = dict(krita_active=False, gate_active=False)
		seq = ([sample(**down)] * 15 + [sample(dsh_mem=2.0, **down)] + [sample(**down)] * 15)
		self.assertEqual(kinds(run(guard, seq, start=100.0), "start"), [])
		self.assertEqual(guard.mode, "tripped")

	def test_no_recover_while_dsh_down(self):
		guard = kg.Guard()
		run(guard, [sample(dsh_active=False)])
		down = dict(krita_active=False, gate_active=False, dsh_active=False)
		self.assertEqual(kinds(run(guard, [sample(**down)] * 40, start=10.0), "start"), [])

	def test_dsh_restart_trip_recovers_and_rebases(self):
		guard = kg.Guard()
		run(guard, [sample(), sample(dsh_restarts="1")])
		down = dict(krita_active=False, gate_active=False, dsh_restarts="1")
		events = run(guard, [sample(**down)] * 20, start=10.0)
		self.assertEqual(len(kinds(events, "start")), 1)
		# 恢复后用新的 NRestarts 当基准，不会马上又停。
		self.assertEqual(kinds(run(guard, [sample(dsh_restarts="1")] * 5, start=200.0), "stop"), [])

	def test_manual_start_returns_to_watching(self):
		guard = self.tripped()
		# 先看到真的停住，之后 gate 又在跑，才是有人手动开回来（还没停住就看到在跑是 stop 失败，见 StopFailTest）。
		run(guard, [sample(krita_active=False, gate_active=False)], start=95.0)
		events = run(guard, [sample(dsh_mem=10.0)], start=100.0)
		self.assertIn("MANUAL", events[0][2])
		self.assertEqual(guard.mode, "watching")

	def test_locks_after_three_pressure_trips_in_an_hour(self):
		guard = kg.Guard()
		t = 0.0
		for _ in range(3):
			run(guard, [sample(dsh_mem=30.0)] * 4, start=t)
			t += 20
			run(guard, [sample(krita_active=False, gate_active=False)] * 20, start=t)
			t += 100
		self.assertEqual(guard.mode, "locked")
		down = dict(krita_active=False, gate_active=False)
		self.assertEqual(kinds(run(guard, [sample(**down)] * 50, start=t), "start"), [])
		# 有人手动开回来就解开。
		run(guard, [sample()], start=t + 300)
		self.assertEqual(guard.mode, "watching")

	def test_start_failure_retries_later(self):
		guard = self.tripped()
		down = dict(krita_active=False, gate_active=False)
		events = run(guard, [sample(**down)] * 19, start=100.0)
		self.assertEqual(len(kinds(events, "start")), 1)
		guard.start_failed(190.0)
		events = run(guard, [sample(**down)] * 20, start=195.0)
		self.assertEqual(len(kinds(events, "start")), 1)
		self.assertGreaterEqual(kinds(events, "start")[0][0], 195.0 + kg.RECOVER_SECONDS)

	def test_state_survives_restart(self):
		guard = self.tripped()
		again = kg.Guard(json.loads(json.dumps(guard.state())))
		self.assertEqual(again.mode, "tripped")
		down = dict(krita_active=False, gate_active=False)
		self.assertEqual(len(kinds(run(again, [sample(**down)] * 20, start=100.0), "start")), 1)


DOWN = dict(krita_active=False, gate_active=False)


class StopFailTest(unittest.TestCase):
	"""审查 1：停服务失败时不能当成「有人手动开回来」。"""

	def test_stop_failure_retries_instead_of_manual(self):
		guard = kg.Guard()
		self.assertEqual(len(kinds(run(guard, [sample(dsh_mem=30.0)] * 4), "stop")), 1)
		# systemctl stop 失败：下一轮 Krita 和 gate 都还在跑，要再停，不是 MANUAL。
		events = run(guard, [sample(dsh_mem=30.0)] * 3, start=20.0)
		self.assertEqual(len(kinds(events, "stop")), 3, events)
		self.assertFalse([e for e in events if "MANUAL" in e[2]], events)
		self.assertNotEqual(guard.mode, "watching")

	def test_one_time_oom_keeps_retrying_after_stop_failure(self):
		# OOM 只发生一次（oom_kill 停在 1）：stop 失败以后不能 rebase 掉，要一直重试到真的停下来。
		guard = kg.Guard()
		run(guard, [sample(), sample(dsh_ooms=1)])
		events = run(guard, [sample(dsh_ooms=1)] * 3, start=10.0)
		self.assertEqual(len(kinds(events, "stop")), 3, events)
		self.assertNotEqual(guard.mode, "watching")
		# 真的停下来以后，有人手动开回来，才算 MANUAL。
		run(guard, [sample(dsh_ooms=1, **DOWN)], start=30.0)
		events = run(guard, [sample(dsh_ooms=1)], start=35.0)
		self.assertIn("MANUAL", events[0][2])
		self.assertEqual(guard.mode, "watching")

	def test_half_started_after_start_failure_is_stopped_again(self):
		guard = kg.Guard()
		run(guard, [sample(dsh_mem=30.0)] * 4)
		run(guard, [sample(**DOWN)] * 20, start=20.0)
		guard.start_failed(120.0)
		# start 失败时 krita 起来了、gate 没起来：把半开的 krita 停掉，不是 MANUAL，也不是放着。
		events = run(guard, [sample(krita_active=True, gate_active=False)], start=125.0)
		self.assertEqual(len(kinds(events, "stop")), 1, events)


class TripCountTest(unittest.TestCase):
	"""审查 3：「1 小时内停满 3 次就锁住」以停的那一刻为准。"""

	def recover(self, guard, start, **over):
		events = run(guard, [sample(**DOWN, **over)] * 20, start=start)
		return kinds(events, "start")

	def test_third_trip_locks_at_stop_time(self):
		# 0、1700、3590 秒各停一次：第 3 次停的那一刻 1 小时内正好 3 次。旧版到恢复前（3590 + 90 秒以后）
		# 才数，那时第 1 次已经滑出窗口，就又开回来了。
		guard = kg.Guard()
		for t in (0.0, 1700.0, 3590.0):
			stops = kinds(run(guard, [sample(dsh_mem=30.0)] * 4, start=t - 15), "stop")
			self.assertEqual([s[0] for s in stops], [t])
			starts = self.recover(guard, t + 5)
			if t < 3590.0:
				self.assertEqual(len(starts), 1)
		self.assertEqual(starts, [])
		self.assertEqual(guard.mode, "locked")
		self.assertEqual(self.recover(guard, 3800.0), [])

	def test_oom_with_auto_restart_counts(self):
		# dsh 被 OOM kill、systemd 马上把它重启：同一笔取样 NRestarts 和 oom_kill 都变了，要算进次数。
		guard = kg.Guard()
		run(guard, [sample()])
		for n in (1, 2, 3):
			t = n * 300.0
			state = dict(dsh_restarts=str(n), dsh_ooms=n)
			self.assertEqual(len(kinds(run(guard, [sample(**state)], start=t), "stop")), 1)
			starts = self.recover(guard, t + 5, **state)
			if n < 3:
				self.assertEqual(len(starts), 1)
		self.assertEqual(starts, [])
		self.assertEqual(guard.mode, "locked")

	def test_oom_seen_only_in_journal_counts(self):
		# 实际上 dsh 被 OOM kill、自动重启以后 cgroup 是新的，oom_kill 计数器回到 0；只有 systemd 的日志看得到。
		guard = kg.Guard()
		run(guard, [sample()])
		for n in (1, 2, 3):
			t = n * 300.0
			state = dict(dsh_restarts=str(n), dsh_ooms=0)
			stops = kinds(run(guard, [sample(dsh_oom_logged=True, **state)], start=t), "stop")
			self.assertEqual(len(stops), 1)
			self.assertIn("OOM", stops[0][2])
			starts = self.recover(guard, t + 5, **state)
		self.assertEqual(starts, [])
		self.assertEqual(guard.mode, "locked")

	def test_oom_counter_reset_after_restart_is_not_an_oom(self):
		# 以前有过 OOM（计数 1），dsh 后来重启、cgroup 重建，计数变回 0：这不是新的 OOM。之后再变成 1 才是。
		guard = kg.Guard()
		run(guard, [sample(dsh_ooms=1)])
		self.assertEqual(kinds(run(guard, [sample(dsh_ooms=0)] * 3, start=5.0), "stop"), [])
		stops = kinds(run(guard, [sample(dsh_ooms=1)], start=20.0), "stop")
		self.assertEqual(len(stops), 1)
		self.assertIn("OOM", stops[0][2])

	def test_deploy_restarts_do_not_count(self):
		# 单纯的 dsh 重启（部署）不算进次数。
		guard = kg.Guard()
		run(guard, [sample()])
		for n in (1, 2, 3, 4):
			t = n * 300.0
			state = dict(dsh_restarts=str(n))
			run(guard, [sample(**state)], start=t)
			self.assertEqual(len(self.recover(guard, t + 5, **state)), 1)
		self.assertEqual(guard.mode, "watching")


class FakeClock:
	"""假的 time：sleep 只把时间往前拨，并在指定的时间点跑测试的钩子（模拟那段时间里发生的事）。"""

	def __init__(self, start=1_000_000.0):
		self.now = start
		self.hooks = []

	def time(self):
		return self.now

	def sleep(self, seconds):
		self.now += seconds
		for hook in list(self.hooks):
			hook()

	def monotonic(self):
		return self.now


class Result:
	def __init__(self, stdout="", returncode=0, stderr=""):
		self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


class FakeUnits:
	"""假的 systemctl：记下每个呼叫，restart / try-restart / start 会把单元改成 active。"""

	def __init__(self):
		self.active = {"krita.service": True, "krita-gate.service": True}
		self.calls = []

	def __call__(self, *args):
		self.calls.append(args)
		verb = args[0]
		if verb == "show":
			unit, prop = args[1], args[3]
			if prop == "ActiveState":
				return Result("active\n" if self.active.get(unit) else "inactive\n")
			if prop == "MainPID":
				return Result("4242\n")
			return Result("0\n")
		if verb == "is-active":
			return Result("active\n" if self.active.get(args[1]) else "inactive\n")
		if verb in ("restart", "start"):
			for unit in args[1:]:
				self.active[unit] = True
		if verb == "try-restart":
			pass
		if verb == "stop":
			for unit in args[1:]:
				self.active[unit] = False
		return Result()

	def restarted(self):
		return [c for c in self.calls if c[0] in ("restart", "try-restart", "start")]


@unittest.skipUnless(os.name == "posix", "要 Linux 的 flock 和档案权限")
class IdleFlowTest(unittest.TestCase):
	"""审查 2：闲置重启的最后确认，以及和 guard 的互锁。直接在同一个行程里跑 krita_idle_restart.main()。"""

	def setUp(self):
		self.dir = Path(tempfile.mkdtemp())
		cg = self.dir / "cg"
		(cg / "krita.service").mkdir(parents=True)
		(cg / "krita-gate.service").mkdir(parents=True)
		(cg / "krita.service" / "memory.stat").write_text(f"anon {1200 * 1048576}\n")
		self.cpu = cg / "krita-gate.service" / "cpu.stat"
		self.cpu.write_text("usage_usec 5000\n")
		self.socket = self.dir / "gate.sock"
		self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
		self.server.bind(str(self.socket))
		self.addCleanup(self.server.close)
		os.chmod(self.socket, 0o660)
		self.guard_state = self.dir / "state.json"
		self.clock = FakeClock()
		(self.dir / "idle.json").write_text(json.dumps({"gate_pid": "4242", "usage": 5000,
		                                                "changed_at": self.clock.now - 3600}))
		self.units = FakeUnits()
		self.connections = []
		self.saved = {}
		patch = dict(CG=str(cg), STATE=str(self.dir / "idle.json"), LOG=str(self.dir / "log"),
		             GATE_SOCKET=str(self.socket), GUARD_STATE=str(self.guard_state),
		             LOCK=str(self.dir / "interlock.lock"), MARKER=str(self.dir / "stuck.json"),
		             systemctl=self.units, gate_connections=self.count_connections, time=self.clock,
		             wait_ready=lambda started: 1.0)
		for name, value in patch.items():
			self.saved[name] = getattr(kr, name, None)
			setattr(kr, name, value)
		self.argv = sys.argv
		sys.argv = ["krita_idle_restart.py"]

	def tearDown(self):
		for name, value in self.saved.items():
			setattr(kr, name, value)
		sys.argv = self.argv

	def count_connections(self):
		self.connections.append(self.door_open())
		return self.busy() if callable(getattr(self, "busy", None)) else 0

	def door_open(self):
		return bool(self.socket.stat().st_mode & 0o060)

	def log(self):
		path = self.dir / "log"
		return path.read_text() if path.exists() else ""

	def test_idle_and_big_restarts(self):
		self.assertEqual(kr.main(), 0)
		self.assertEqual(len(self.units.restarted()), 1, self.units.calls)
		self.assertIn("完成", self.log())
		self.assertTrue(self.door_open(), "重启完要让 dsh 连得进来")

	def test_guard_stopping_during_final_check_is_respected(self):
		# 最后 20 秒确认期间 guard 刚好把 Krita 停掉：闲置重启不能把它拉回来。
		def guard_trips():
			self.units.active = {"krita.service": False, "krita-gate.service": False}
			self.guard_state.write_text(json.dumps({"mode": "tripped", "reason": "dsh 记忆体压力"}))
		self.clock.hooks.append(guard_trips)
		kr.main()
		self.assertEqual(self.units.restarted(), [], self.units.calls)
		self.assertEqual(self.units.active, {"krita.service": False, "krita-gate.service": False})

	def test_guard_tripped_state_blocks_even_if_units_look_active(self):
		# guard 已经决定停（state 是 tripped），stop 还没生效、单元看起来还在跑：也不能重启。
		self.guard_state.write_text(json.dumps({"mode": "tripped", "reason": "OOM"}))
		kr.main()
		self.assertEqual(self.units.restarted(), [], self.units.calls)

	def test_request_starting_right_after_last_look_is_not_interrupted(self):
		# 一笔画图请求刚好在最后一次检查之后进来：门开着就进得来，之后一直有连线。
		started = []

		def busy():
			if started:
				return 1
			if self.door_open():
				started.append(True)  # 这次看的时候还没连上，紧接着就连上了
			return 0
		self.busy = busy
		kr.main()
		self.assertEqual(self.units.restarted(), [], f"请求进行中被重启：{self.units.calls}")
		self.assertTrue(self.door_open(), "不重启的话要把门开回来")

	def test_in_flight_marker_blocks_restart(self):
		(self.dir / "stuck.json").write_text('{"state": "in-flight"}')
		kr.main()
		self.assertEqual(self.units.restarted(), [], self.units.calls)

	def test_lock_held_by_guard_blocks_restart(self):
		import fcntl
		fd = os.open(self.dir / "interlock.lock", os.O_RDWR | os.O_CREAT, 0o600)
		fcntl.flock(fd, fcntl.LOCK_EX)
		try:
			kr.main()
		finally:
			os.close(fd)
		self.assertEqual(self.units.restarted(), [], self.units.calls)

	def test_corrupt_guard_state_blocks_restart(self):
		self.guard_state.write_text("{not json")
		kr.main()
		self.assertEqual(self.units.restarted(), [], self.units.calls)

	def test_door_does_not_follow_symlink(self):
		# /run/krita-gate 归 krita：gate.sock 被换成连结时，root 不能顺着连结去改别的档案的权限。
		target = self.dir / "target"
		target.write_text("")
		os.chmod(target, 0o644)
		self.server.close()
		self.socket.unlink()
		self.socket.symlink_to(target)
		kr.main()
		self.assertEqual(self.units.restarted(), [], self.units.calls)
		self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)
		self.assertIn("关不了 gate 的门", self.log())


class ParseTest(unittest.TestCase):
	def test_psi(self):
		text = "some avg10=22.41 avg60=3.00 avg300=0.75 total=1\nfull avg10=20.00 avg60=0 avg300=0 total=1\n"
		self.assertEqual(kg.psi_avg10(text), 22.41)
		self.assertIsNone(kg.psi_avg10(None))
		self.assertIsNone(kg.psi_avg10("garbage"))

	def test_keyed_and_meminfo(self):
		self.assertEqual(kg.keyed("low 0\nhigh 5\noom_kill 2\n", "oom_kill"), 2)
		self.assertIsNone(kg.keyed("", "oom_kill"))
		self.assertEqual(kg.mem_available_mb("MemTotal: 5000000 kB\nMemAvailable:    3100000 kB\n"), 3027)


@unittest.skipUnless(os.name == "posix", "需要能执行的假 systemctl")
class ProcessTest(unittest.TestCase):
	"""真的跑 krita_guard.py：假的 cgroup / proc 档案，假的 systemctl 把呼叫记下来。"""

	def setUp(self):
		self.dir = Path(tempfile.mkdtemp())
		cg, proc = self.dir / "cg", self.dir / "proc"
		for unit in ("dsh.service", "krita.service"):
			(cg / unit).mkdir(parents=True)
			(cg / unit / "memory.events").write_text("oom_kill 0\n")
			(cg / unit / "memory.peak").write_text("104857600\n")
			(cg / unit / "memory.stat").write_text("anon 104857600\n")
			(cg / unit / "memory.pressure").write_text("some avg10=0.00 avg60=0 avg300=0 total=0\n")
		(proc / "pressure").mkdir(parents=True)
		(proc / "pressure" / "memory").write_text("some avg10=0.00 avg60=0 avg300=0 total=0\n")
		(proc / "pressure" / "cpu").write_text("some avg10=0.00 avg60=0 avg300=0 total=0\n")
		(proc / "meminfo").write_text("MemAvailable: 3000000 kB\n")
		self.calls = self.dir / "calls"
		self.units = self.dir / "units"
		self.units.mkdir()
		for unit in ("dsh.service", "krita.service", "krita-gate.service"):
			(self.units / unit).write_text("active")
		fake = self.dir / "systemctl"
		fake.write_text(f"""#!/bin/sh
echo "$*" >> {self.calls}
U={self.units}
case "$1" in
is-active) cat "$U/$2" 2>/dev/null || echo inactive ;;
show) cat "$U/nrestarts" 2>/dev/null || echo 0 ;;
stop) [ -f {self.dir}/fail_stop ] && {{ echo "Job failed" >&2; exit 1; }}; shift; for u in "$@"; do [ -f "$U/$u" ] && echo inactive > "$U/$u"; done; true ;;
start) echo active > "$U/krita-gate.service"; echo active > "$U/krita.service" ;;
esac
""")
		fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
		# 假的 journalctl：印出 journal 档的内容（测试要模拟 systemd 记下 OOM 时才写这个档）。
		self.journal = self.dir / "journal"
		journalctl = self.dir / "journalctl"
		journalctl.write_text(f'#!/bin/sh\necho "$*" >> {self.dir}/journal-calls\ncat {self.journal} 2>/dev/null\ntrue\n')
		journalctl.chmod(0o755)
		self.env = dict(os.environ, KRITA_GUARD_JOURNALCTL=str(journalctl),
		                KRITA_GUARD_CGROUP_ROOT=str(cg), KRITA_GUARD_PROC_ROOT=str(proc),
		                KRITA_GUARD_LOG=str(self.dir / "log"), KRITA_GUARD_STATE_FILE=str(self.dir / "state.json"),
		                KRITA_GUARD_SYSTEMCTL=str(fake), KRITA_GUARD_TICK="0.05",
		                KRITA_GUARD_LOCK=str(self.dir / "interlock.lock"))
		self.dsh_psi = cg / "dsh.service" / "memory.pressure"
		self.sys_psi = proc / "pressure" / "memory"

	def start_guard(self):
		script = Path(__file__).with_name("krita_guard.py")
		# 时间常数在子程序里改不到，所以用一个小包装把它们缩短（逻辑不变）。
		wrapper = self.dir / "wrap.py"
		wrapper.write_text(f"import sys; sys.path.insert(0, {str(script.parent)!r})\n"
		                   "import krita_guard as g\ng.TRIP_SECONDS = 0.3\ng.RECOVER_SECONDS = 0.5\ng.main()\n")
		return subprocess.Popen([sys.executable, str(wrapper)], env=self.env)

	def calls_text(self):
		return self.calls.read_text() if self.calls.exists() else ""

	def log_text(self):
		return (self.dir / "log").read_text() if (self.dir / "log").exists() else ""

	def test_stop_failure_is_retried_not_manual(self):
		# 审查 1：systemctl stop 失败，Krita 和 gate 都还在跑。旧版下一轮就记 MANUAL、回到监看，不再停。
		(self.dir / "fail_stop").write_text("")
		proc = self.start_guard()
		try:
			self.dsh_psi.write_text("some avg10=30.00 avg60=0 avg300=0 total=0\n")
			self.wait_for(lambda: self.calls_text().count("stop krita.service krita-gate.service") >= 3)
			self.assertNotIn("MANUAL", self.log_text())
			# stop 恢复正常以后就真的停下来。
			(self.dir / "fail_stop").unlink()
			self.wait_for(lambda: (self.units / "krita.service").read_text().strip() == "inactive")
		finally:
			proc.kill()
			proc.wait()

	def test_oom_restart_from_journal_is_counted(self):
		# 审查 3：dsh 被 OOM kill 后自动重启（NRestarts 0 → 1），新的 cgroup 里 oom_kill 是 0；
		# systemd 的日志有 oom-kill，要算进锁住的次数。
		self.journal.write_text("dsh.service: A process of this unit has been killed by the OOM killer.\n"
		                        "dsh.service: Failed with result 'oom-kill'.\n")
		proc = self.start_guard()
		try:
			self.wait_for(lambda: "mode=watching" in self.log_text())
			(self.units / "nrestarts").write_text("1")
			self.wait_for(lambda: "STOP" in self.log_text() and (self.dir / "state.json").exists())
			self.wait_for(lambda: len(json.loads((self.dir / "state.json").read_text()).get("trips", [])) == 1)
			self.assertIn("OOM", self.log_text())
			self.assertIn("UNIT=dsh.service", (self.dir / "journal-calls").read_text())
		finally:
			proc.kill()
			proc.wait()

	def test_guard_takes_interlock_before_stopping(self):
		# 审查 2：闲置重启拿着互锁（正在重启 Krita）时，guard 要等它放开再停，而且停之前先把状态写好。
		import fcntl
		fd = os.open(self.dir / "interlock.lock", os.O_RDWR | os.O_CREAT, 0o600)
		fcntl.flock(fd, fcntl.LOCK_EX)
		proc = self.start_guard()
		try:
			self.dsh_psi.write_text("some avg10=30.00 avg60=0 avg300=0 total=0\n")
			self.wait_for(lambda: "STOP" in self.log_text())
			time.sleep(1.0)
			self.assertNotIn("stop krita.service", self.calls_text(), "拿不到互锁时不能停")
			os.close(fd)
			self.wait_for(lambda: "stop krita.service krita-gate.service" in self.calls_text())
			self.assertEqual(json.loads((self.dir / "state.json").read_text())["mode"], "tripped")
		finally:
			proc.kill()
			proc.wait()

	def test_stop_then_recover_end_to_end(self):
		proc = self.start_guard()
		try:
			self.sys_psi.write_text("some avg10=40.00 avg60=0 avg300=0 total=0\n")
			time.sleep(0.6)
			self.assertNotIn("stop", self.calls.read_text() if self.calls.exists() else "")
			self.dsh_psi.write_text("some avg10=30.00 avg60=0 avg300=0 total=0\n")
			self.wait_for(lambda: (self.units / "krita.service").read_text().strip() == "inactive")
			self.assertIn("stop krita.service krita-gate.service", self.calls.read_text())
			self.dsh_psi.write_text("some avg10=0.00 avg60=0 avg300=0 total=0\n")
			self.wait_for(lambda: (self.units / "krita-gate.service").read_text().strip() == "active")
			log = (self.dir / "log").read_text()
			self.assertIn("STOP: dsh 记忆体压力 30.00%", log)
			self.assertIn("RECOVER:", log)
			self.assertEqual(proc.poll(), None, "guard 停掉 Krita 以后自己要还在跑")
		finally:
			proc.kill()
			proc.wait()

	def wait_for(self, check, timeout=10.0):
		deadline = time.time() + timeout
		while time.time() < deadline:
			if check():
				return
			time.sleep(0.05)
		self.fail("等太久：" + ((self.dir / "log").read_text() if (self.dir / "log").exists() else ""))


OLD_GUARD_UNIT = """[Unit]
Description=旧版 guard
Requires=krita-gate.service
After=dsh.service krita-gate.service
PartOf=krita-gate.service

[Service]
ExecStart=/bin/sh /opt/krita-gate/krita_guard.sh

[Install]
WantedBy=multi-user.target
"""

NEW_GUARD_UNIT = """[Unit]
Description=新版 guard
After=dsh.service krita-gate.service

[Service]
ExecStart=/usr/bin/python3 -I /opt/krita-gate/krita_guard.py

[Install]
WantedBy=multi-user.target
"""


@unittest.skipUnless(os.name == "posix", "要能执行的 sh 和假 systemctl")
class RollbackTest(unittest.TestCase):
	"""审查 4：rollback.sh 照实回报每一步，不把停着的 gate 拉起来。用假的 systemctl 和暂存资料夹跑。"""

	def setUp(self):
		self.dir = Path(tempfile.mkdtemp())
		self.unit_dir, self.opt, self.state = self.dir / "units", self.dir / "opt", self.dir / "state"
		self.backup = self.dir / "backup"
		for d in (self.unit_dir, self.opt, self.state, self.backup / "units", self.backup / "opt"):
			d.mkdir(parents=True)
		# 现在装的是新版；备份里是部署前的版本。
		(self.unit_dir / "krita-works-guard.service").write_text(NEW_GUARD_UNIT)
		for f in ("krita-idle-restart.service", "krita-idle-restart.timer"):
			(self.unit_dir / f).write_text("[Unit]\n")
		for f in ("krita_guard.py", "krita_idle_restart.py", "krita_guard.sh"):
			(self.opt / f).write_text("new\n")
		self.calls = self.dir / "calls"
		fake = self.dir / "systemctl"
		fake.write_text(f"""#!/bin/sh
echo "$*" >> {self.calls}
S={self.state}; U={self.unit_dir}
case "$1" in
is-active) v=$(cat "$S/$2" 2>/dev/null || echo inactive); echo "$v"; [ "$v" = active ] ;;
show)
	case "$2" in
	dsh.service) echo 1234; echo 0 ;;
	*) sed -n 's/^\\(Requires\\|BindsTo\\)=//p' "$U/$2" 2>/dev/null | tr '\\n' ' '; echo ;;
	esac ;;
daemon-reload) [ ! -f "$S/fail_reload" ] || {{ echo "Failed to reload daemon" >&2; exit 1; }} ;;
stop|disable) shift; for u; do case "$u" in --*) ;; *) echo inactive > "$S/$u" ;; esac; done ;;
start|restart|enable)
	verb=$1; shift
	[ "$verb" = enable ] && case " $* " in *" --now "*) ;; *) exit 0 ;; esac
	for u; do
		case "$u" in --*) continue ;; esac
		[ ! -f "$S/fail_start" ] || {{ echo "Job failed" >&2; exit 1; }}
		echo active > "$S/$u"
		if grep -q '^Requires=krita-gate.service' "$U/$u" 2>/dev/null; then
			echo active > "$S/krita-gate.service"; echo active > "$S/krita.service"
		fi
	done ;;
esac
""")
		fake.chmod(0o755)
		self.env = dict(os.environ, KRITA_ROLLBACK_UNIT_DIR=str(self.unit_dir), KRITA_ROLLBACK_OPT_DIR=str(self.opt),
		                KRITA_ROLLBACK_SYSTEMCTL=str(fake))

	def units(self, **active):
		for key, value in active.items():
			name, _, suffix = key.replace("_", "-").rpartition("-")  # krita_gate_service → krita-gate.service
			(self.state / f"{name}.{suffix}").write_text("active" if value else "inactive")

	def active(self, unit):
		path = self.state / unit
		return path.exists() and path.read_text().strip() == "active"

	def old_backup(self, gate="active"):
		"""2026-11-28 以前的版本：只有旧 guard（Requires= krita-gate）和 krita_guard.sh，没有 timer。"""
		(self.backup / "units" / "krita-works-guard.service").write_text(OLD_GUARD_UNIT)
		(self.backup / "opt" / "krita_guard.sh").write_text("old\n")
		(self.backup / "status-before.txt").write_text(
			f"dsh: active=active enabled=enabled\nkrita: active={gate} enabled=enabled\n"
			f"krita-gate: active={gate} enabled=enabled\nkrita-works-guard: active=active enabled=enabled\n"
			"krita-idle-restart.timer: active=inactive enabled=not-found\n")

	def rollback(self):
		script = self.backup / "rollback.sh"
		script.write_bytes(Path(__file__).with_name("rollback_guard.sh").read_bytes())
		return subprocess.run(["sh", str(script)], env=self.env, capture_output=True, text=True)

	def test_stopped_gate_stays_stopped(self):
		self.old_backup()
		self.units(krita_service=False, krita_gate_service=False, krita_works_guard_service=True, dsh_service=True)
		done = self.rollback()
		self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
		self.assertFalse(self.active("krita-gate.service"), f"停着的 gate 被拉起来了：\n{done.stdout}")
		self.assertFalse(self.active("krita.service"))
		self.assertIn("systemctl start krita-gate.service", done.stdout)
		self.assertEqual((self.unit_dir / "krita-works-guard.service").read_text(), OLD_GUARD_UNIT)
		self.assertFalse((self.unit_dir / "krita-idle-restart.timer").exists())
		self.assertFalse((self.opt / "krita_guard.py").exists())

	def test_failure_is_reported_as_failure(self):
		self.old_backup()
		self.units(krita_service=True, krita_gate_service=True, krita_works_guard_service=True, dsh_service=True)
		(self.state / "fail_reload").write_text("")
		done = self.rollback()
		self.assertNotEqual(done.returncode, 0, done.stdout)
		self.assertIn("失败", done.stdout)
		self.assertIn("daemon-reload", done.stdout)
		self.assertIn("回滚没完成", done.stdout)
		self.assertNotIn("回滚完成", done.stdout.replace("回滚没完成", ""))

	def test_guard_start_failure_is_reported(self):
		self.old_backup()
		self.units(krita_service=True, krita_gate_service=True, krita_works_guard_service=True, dsh_service=True)
		(self.state / "fail_start").write_text("")
		done = self.rollback()
		self.assertNotEqual(done.returncode, 0, done.stdout)
		self.assertIn("启动 guard", done.stdout)
		self.assertIn("回滚没完成", done.stdout)

	def test_running_gate_rolls_back_cleanly(self):
		self.old_backup()
		self.units(krita_service=True, krita_gate_service=True, krita_works_guard_service=True, dsh_service=True,
		           krita_idle_restart_timer=True)
		done = self.rollback()
		self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
		self.assertIn("回滚完成", done.stdout)
		self.assertTrue(self.active("krita-works-guard.service"))
		self.assertTrue(self.active("krita-gate.service"))
		self.assertFalse(self.active("krita-idle-restart.timer"))
		self.assertEqual((self.opt / "krita_guard.sh").read_text(), "old\n")

	def test_restores_timer_from_backup(self):
		# 回滚到有 timer 的版本（例如 9a1ce2e）：timer 回到部署前启用的状态，guard 照常启动。
		(self.backup / "units" / "krita-works-guard.service").write_text(NEW_GUARD_UNIT)
		for f in ("krita-idle-restart.service", "krita-idle-restart.timer"):
			(self.backup / "units" / f).write_text("[Unit]\n# old\n")
		for f in ("krita_guard.py", "krita_idle_restart.py", "krita_guard.sh"):
			(self.backup / "opt" / f).write_text("old\n")
		(self.backup / "status-before.txt").write_text(
			"dsh: active=active enabled=enabled\nkrita: active=inactive enabled=enabled\n"
			"krita-gate: active=inactive enabled=enabled\nkrita-works-guard: active=active enabled=enabled\n"
			"krita-idle-restart.timer: active=active enabled=enabled\n")
		self.units(krita_service=False, krita_gate_service=False, krita_works_guard_service=True, dsh_service=True,
		           krita_idle_restart_timer=True)
		done = self.rollback()
		self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
		self.assertTrue(self.active("krita-idle-restart.timer"), done.stdout)
		self.assertTrue(self.active("krita-works-guard.service"), done.stdout)
		self.assertFalse(self.active("krita-gate.service"))
		self.assertEqual((self.opt / "krita_guard.py").read_text(), "old\n")


class IdleRestartTest(unittest.TestCase):
	def decide(self, **over):
		base = dict(krita_active=True, gate_active=True, idle_seconds=3600, anon_mb=500, uptime_h=5, local_hour=15)
		base.update(over)
		return kr.decide(**base)[0]

	def test_big_and_idle_restarts(self):
		self.assertTrue(self.decide(anon_mb=1150))

	def test_big_but_busy_waits(self):
		self.assertFalse(self.decide(anon_mb=1150, idle_seconds=10 * 60))

	def test_small_does_nothing_outside_daily_window(self):
		self.assertFalse(self.decide(anon_mb=900))

	def test_daily_window_needs_long_uptime(self):
		self.assertTrue(self.decide(local_hour=4, uptime_h=30))
		self.assertFalse(self.decide(local_hour=4, uptime_h=2))
		self.assertFalse(self.decide(local_hour=4, uptime_h=30, idle_seconds=60))

	def test_never_touches_stopped_krita(self):
		self.assertFalse(self.decide(anon_mb=1500, gate_active=False))
		self.assertFalse(self.decide(anon_mb=1500, krita_active=False))

	def test_never_touches_krita_while_guard_holds_it(self):
		for mode in ("tripped", "locked", None):
			self.assertFalse(self.decide(anon_mb=1500, guard_mode=mode), mode)

	def test_track_idle_time(self):
		state = kr.track({}, "100", 5000, now=0)
		self.assertEqual(state["changed_at"], 0)
		state = kr.track(state, "100", 5000, now=600)
		self.assertEqual(state["changed_at"], 0)
		state = kr.track(state, "100", 5100, now=1200)
		self.assertEqual(state["changed_at"], 1200)
		state = kr.track(state, "200", 5100, now=1800)
		self.assertEqual(state["changed_at"], 1800)


if __name__ == "__main__":
	unittest.main()
