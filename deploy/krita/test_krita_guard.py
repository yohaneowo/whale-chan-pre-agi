"""krita_guard.py / krita_idle_restart.py 的测试：用假的 PSI 数字和假的 systemctl 跑判断。

在 deploy/krita 底下跑：python3 -m unittest test_krita_guard
"""

import json
import os
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
show) echo 0 ;;
stop) shift; for u in "$@"; do [ -f "$U/$u" ] && echo inactive > "$U/$u"; done; true ;;
start) echo active > "$U/krita-gate.service"; echo active > "$U/krita.service" ;;
esac
""")
		fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
		self.env = dict(os.environ, KRITA_GUARD_CGROUP_ROOT=str(cg), KRITA_GUARD_PROC_ROOT=str(proc),
		                KRITA_GUARD_LOG=str(self.dir / "log"), KRITA_GUARD_STATE_FILE=str(self.dir / "state.json"),
		                KRITA_GUARD_SYSTEMCTL=str(fake), KRITA_GUARD_TICK="0.05")
		self.dsh_psi = cg / "dsh.service" / "memory.pressure"
		self.sys_psi = proc / "pressure" / "memory"

	def test_stop_then_recover_end_to_end(self):
		script = Path(__file__).with_name("krita_guard.py")
		# 时间常数在子程序里改不到，所以用一个小包装把它们缩短（逻辑不变）。
		wrapper = self.dir / "wrap.py"
		wrapper.write_text(f"import sys; sys.path.insert(0, {str(script.parent)!r})\n"
		                   "import krita_guard as g\ng.TRIP_SECONDS = 0.3\ng.RECOVER_SECONDS = 0.5\ng.main()\n")
		proc = subprocess.Popen([sys.executable, str(wrapper)], env=self.env)
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
