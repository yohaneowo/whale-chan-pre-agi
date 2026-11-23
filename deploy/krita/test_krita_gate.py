import json
import os
import socket
import stat
import tempfile
import threading
import time
import unittest

import krita_gate as gate

POSIX = os.name == "posix" and hasattr(os, "O_NOFOLLOW")


def batch(*ops, job="d1101-a3f9"):
    return {"job": job, "ops": list(ops)}


def op(command, target=None, **params):
    return {"command": command, "target": target or {"document_id": "doc1", "node_id": "n1"}, "params": params}


def stroke(n, **extra):
    params = dict(preset_id="p1", size_px=8, opacity=1, color="#FF0000", points=[[i % 100, i % 100] for i in range(n)])
    params.update(extra)
    return op("paint_path", **params)


class FakeClient:
    """Each operation takes `duration` seconds; execute() returns early (running) if wait_timeout is
    shorter, like the real bridge."""

    def __init__(self, fail_at=None, duration=0.0, never_finish=False):
        self.calls, self.cancels = [], []
        self.fail_at, self.duration, self.never_finish = fail_at, duration, never_finish
        self.done_at = {}
        # What inspect_document reports: a 1200 x 800 picture with one full layer.
        self.document = {"width": 1200, "height": 800,
                         "layers": [{"node_id": "n1", "type": "paintlayer",
                                     "bounds": {"x": 0, "y": 0, "width": 1200, "height": 800}}]}

    def status(self):
        return {"instances": [{"instance_id": "inst1", "reachable": True}]}

    def execute(self, command, *, instance_id, operation_id, target, params, wait_timeout):
        self.calls.append((command, target, params, operation_id))
        self.done_at[operation_id] = time.monotonic() + self.duration
        if self.fail_at is not None and len(self.calls) - 1 == self.fail_at:
            return {"state": "failed", "error": {"code": "BOOM", "message": "no"}}
        if self.never_finish or self.duration > wait_timeout:
            time.sleep(min(wait_timeout, 0.5))
            return {"state": "running"}
        time.sleep(self.duration)
        if command == "get_preview":
            return {"state": "succeeded", "result": {"artifact_id": "a" * 32}}
        if command == "inspect_document":
            return {"state": "succeeded", "result": self.document}
        return {"state": "succeeded", "result": {"node_id": "n2"}}

    def get_operation(self, instance_id, operation_id):
        if self.never_finish:
            return {"state": "running"}
        return {"state": "succeeded" if time.monotonic() >= self.done_at[operation_id] else "running"}

    def cancel_operation(self, instance_id, operation_id):
        self.cancels.append(operation_id)

    def get_artifact(self, instance_id, artifact_id):
        return b"\x89PNG fake"


class NoFolders:
    def __init__(self):
        self.reserved = []

    def job_dir(self, job):
        raise AssertionError("not expected")

    def write(self, job, name, data):
        return f"{job}/{name}"

    def stage_import(self, job, name):
        return f"{job}/staged.png"

    def check_room(self, job, adding=0):
        self.reserved.append(adding)
        return {"bytes": 0, "jobs": {}, "removed": 0}


class DirFolders(NoFolders):
    """Like NoFolders, but save/export may open the job folder."""

    def job_dir(self, job):
        return os.open(os.path.abspath(__file__), os.O_RDONLY)  # any descriptor; the gate only closes it


class CheckBatchTest(unittest.TestCase):
    def test_allowed_paint_batch_passes(self):
        job, ops = gate.check_batch(batch(stroke(5)))
        self.assertEqual((job, len(ops[0]["params"]["points"])), ("d1101-a3f9", 5))

    def test_forbidden_commands_are_refused(self):
        for command in ("open_document", "create_file_layer", "set_file_layer", "generate_diffusion",
                        "configure_diffusion", "add_vector_shape", "exec", "run_python"):
            with self.assertRaises(gate.Refused, msg=command):
                gate.check_batch(batch(op(command)))

    def test_nested_points_cannot_bypass_the_limit(self):
        coords = [[i, i] for i in range(25000)]
        for points in ([coords], [[coords]], [[[0, 0], coords]], [{"x": 1, "y": 2}] * 3, [[0, 0], [1, [2, 3]]]):
            with self.assertRaises(gate.Refused):
                gate.check_batch(batch(stroke(2, points=points)))

    def test_values_have_semantic_ranges(self):
        bad = [
            dict(size_px=1e308), dict(size_px=0), dict(size_px=10**400), dict(opacity=1.5), dict(opacity=-0.1),
            dict(opacity=True), dict(color="red"), dict(color="#FF00001"), dict(preset_id="../x"),
            dict(points=[[1e308, 0], [0, 0]]), dict(points=[[10**400, 0], [0, 0]]), dict(points=[[20000, 0], [0, 0]]),
            dict(points=[[float("nan"), 0], [1, 1]]), dict(points=[[float("inf"), 0], [1, 1]]),
            dict(unknown_number=5), dict(pressure_start=2),
        ]
        for extra in bad:
            with self.assertRaises(gate.Refused, msg=str(extra)[:60]):
                gate.check_batch(batch(stroke(2, **extra)))
        for params in (dict(width=10**400, height=10, name="x"), dict(width=0, height=10, name="x"),
                       dict(width=9000, height=10, name="x"), dict(width=10.5, height=10, name="x"),
                       dict(width=4097, height=10, name="x"), dict(width=10, height=8192, name="x")):
            with self.assertRaises(gate.Refused, msg=str(params)[:60]):
                gate.check_batch(batch(op("create_document", {}, **params)))
        gate.check_batch(batch(op("create_document", {}, width=1200, height=800, name="ok")))
        gate.check_batch(batch(op("create_document", {}, width=4096, height=4096, name="max")))
        gate.check_batch(batch(op("get_preview", {"document_id": "doc1"}, max_edge=1024)))
        with self.assertRaises(gate.Refused):
            gate.check_batch(batch(op("get_preview", {"document_id": "doc1"}, max_edge=4096)))
        with self.assertRaises(gate.Refused):
            gate.check_batch(batch(op("clear_selection", {"document_id": "doc1", "extra": "x"})))

    def test_lists_are_refused_where_none_are_expected(self):
        for key, value in (("color", [[1, 2]] * 5000), ("opacity", [1]), ("preset_id", {"a": [1, 2]}), ("name", ["x"])):
            with self.assertRaises(gate.Refused, msg=key):
                gate.check_batch(batch(stroke(2, **{key: value})))
        with self.assertRaises(gate.Refused):
            gate.check_batch(batch(op("set_selection", shape={"kind": "polygon", "points": [[0, [1, 2]]]})))
        with self.assertRaises(gate.Refused):
            gate.check_batch(batch(op("set_selection", shape={"inner": {"points": [[0, 0], [1, 1]]}})))

    def test_point_and_size_limits(self):
        gate.check_batch(batch(*[stroke(2000)] * 10))
        for request in (batch(*[stroke(2000)] * 10, stroke(2)), batch(stroke(2049)),
                        batch(op("paint_bezier_path", start=[0, 0], segments=[[[0, 0], [1, 1], [2, 2]]] * 257)),
                        batch(op("paint_path", preset_id="p" * 300)), batch(*[op("clear_selection")] * 51),
                        batch(op("clear_selection"), job="../../x"),
                        {"job": "d1101-a3f9", "ops": [{"command": "paint_path", "extra": 1}]}):
            with self.assertRaises(gate.Refused):
                gate.check_batch(request)

    def test_file_paths_come_from_the_gate_only(self):
        _, ops = gate.check_batch(batch(op("save_document", {"document_id": "doc1"}, file="art.kra"),
                                        op("export_png", {"document_id": "doc1"}, file="art.png", overwrite=True),
                                        op("import_image_layer", {"document_id": "doc1"}, file="whale.png",
                                           name="whale", x=0, y=0)))
        self.assertEqual(ops[0]["params"], {"root": "out", "path": "d1101-a3f9/art.kra"})
        self.assertEqual(ops[2]["params"]["root"], "in")
        for bad in ("../x.kra", "/etc/passwd.kra", "a/b.kra", "x.png", ".hidden.kra", "", "a.b.kra"):
            with self.assertRaises(gate.Refused, msg=bad):
                gate.check_batch(batch(op("save_document", {"document_id": "doc1"}, file=bad)))
        with self.assertRaises(gate.Refused):
            gate.check_batch(batch(op("save_document", {"document_id": "doc1"}, file="a.kra", path="/tmp/x.kra")))


class GateRunTest(unittest.TestCase):
    def setUp(self):
        self.saved = (gate.BATCH_DEADLINE, gate.OP_WAIT, gate.SETTLE)

    def tearDown(self):
        gate.BATCH_DEADLINE, gate.OP_WAIT, gate.SETTLE = self.saved

    def test_runs_in_order(self):
        client = FakeClient()
        reply = gate.Gate(client, NoFolders()).run(batch(op("clear_selection"), op("get_preview", {"document_id": "d"})))
        self.assertTrue(reply["ok"])
        self.assertEqual([c[0] for c in client.calls], ["clear_selection", "get_preview"])

    def test_import_uses_the_staged_copy(self):
        client = FakeClient()
        gate.Gate(client, NoFolders()).run(batch(op("import_image_layer", {"document_id": "d"}, file="w.png",
                                                    name="w", x=0, y=0)))
        self.assertEqual(client.calls[0][2]["path"], "d1101-a3f9/staged.png")

    def test_stops_at_first_failure(self):
        client = FakeClient(fail_at=1)
        reply = gate.Gate(client, NoFolders()).run(batch(*[op("clear_selection")] * 3))
        self.assertEqual((reply["ok"], reply["done"], len(client.calls)), (False, 2, 2))

    def test_batch_deadline_bounds_the_operation_in_progress(self):
        """Rin 的复现：批次期限 0.05 秒、操作 0.2 秒，以前回 ok。"""
        gate.BATCH_DEADLINE = 0.05
        client = FakeClient(duration=0.2)
        g = gate.Gate(client, NoFolders())
        started = time.monotonic()
        reply = g.run(batch(op("clear_selection"), op("clear_selection")))
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["results"][0]["state"], "unfinished")
        self.assertEqual(len(client.calls), 1, "nothing after the unfinished operation")
        self.assertEqual(len(client.cancels), 1, "Krita was asked to cancel it")
        self.assertGreaterEqual(time.monotonic() - started, 0.2, "lock held until the operation really ended")
        self.assertIsNone(g.stuck)

    def test_operation_krita_never_finishes_keeps_the_gate_busy(self):
        gate.OP_WAIT, gate.SETTLE = 0.05, 0.1
        client = FakeClient(never_finish=True)
        g = gate.Gate(client, NoFolders())
        self.assertFalse(g.run(batch(op("clear_selection")))["ok"])
        self.assertIsNotNone(g.stuck)
        reply = g.run(batch(op("clear_selection"), job="next-job"))
        self.assertTrue(reply["busy"])
        self.assertEqual(len(client.calls), 1, "no new operation while Krita is stuck")
        client.never_finish = False
        client.duration = 0
        self.assertTrue(g.run(batch(op("clear_selection"), job="next-job"))["ok"])

    def test_cancel_stops_after_the_current_operation_and_cancels_it(self):
        client = FakeClient(duration=0.15)
        g = gate.Gate(client, NoFolders())
        replies = {}
        worker = threading.Thread(target=lambda: replies.setdefault("a", g.run(batch(*[op("clear_selection")] * 20))))
        worker.start()
        time.sleep(0.2)
        self.assertTrue(g.run(batch(op("clear_selection"), job="other-job"))["busy"])
        self.assertTrue(g.cancel("d1101-a3f9")["ok"])
        self.assertEqual(len(client.cancels), 1, "the operation in progress is cancelled at once")
        worker.join(5)
        self.assertEqual(replies["a"]["stopped"], "cancelled")
        self.assertLess(len(client.calls), 20)

    def test_refuses_when_not_exactly_one_krita(self):
        client = FakeClient()
        client.status = lambda: {"instances": []}
        with self.assertRaises(gate.Refused):
            gate.Gate(client, NoFolders()).run(batch(op("clear_selection")))


@unittest.skipUnless(POSIX, "needs POSIX O_NOFOLLOW (run in the Linux container)")
class FoldersTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.out, self.inp, self.stage, self.elsewhere = (os.path.join(t, n) for n in ("out", "in", "staging", "elsewhere"))
        for d in (self.out, self.inp, self.stage, self.elsewhere):
            os.mkdir(d)
        self.folders = gate.Folders(self.out, self.inp, self.stage)

    def tearDown(self):
        self.tmp.cleanup()

    def test_writes_inside_its_own_job_folder(self):
        path = self.folders.write("d1101-a3f9", "preview-1-0.png", b"png")
        with open(os.path.join(self.out, path), "rb") as file:
            self.assertEqual(file.read(), b"png")

    def test_job_folder_mode_has_no_explicit_setgid(self):
        """阶段 2 实测：mkdir(0o2750) 在 RestrictSUIDSGID=yes 的服务里回 EPERM，第一批就失败。
        setgid 要靠上层目录继承，不能在 mode 里明写。"""
        import inspect
        source = inspect.getsource(gate.Folders._job_dir)
        self.assertNotIn("0o2", source.split("os.mkdir(", 1)[1].split(")", 1)[0])
        os.chmod(self.out, 0o2750)
        os.close(self.folders.job_dir("d1101-a3f9"))
        mode = os.stat(os.path.join(self.out, "d1101-a3f9")).st_mode
        self.assertTrue(mode & stat.S_ISGID, "上层是 setgid，新目录自动继承")

    def test_symlinked_job_folder_is_refused(self):
        os.symlink(self.elsewhere, os.path.join(self.out, "d1101-a3f9"))
        with self.assertRaises(OSError):
            self.folders.write("d1101-a3f9", "preview-1-0.png", b"png")
        self.assertEqual(os.listdir(self.elsewhere), [])

    def test_symlinked_out_root_is_refused(self):
        link = os.path.join(self.tmp.name, "out-link")
        os.symlink(self.elsewhere, link)
        with self.assertRaises(OSError):
            gate.Folders(link).write("d1101-a3f9", "preview-1-0.png", b"png")

    def test_symlinked_file_is_refused(self):
        os.mkdir(os.path.join(self.out, "d1101-a3f9"))
        target = os.path.join(self.elsewhere, "victim")
        os.symlink(target, os.path.join(self.out, "d1101-a3f9", "preview-1-0.png"))
        with self.assertRaises(OSError):
            self.folders.write("d1101-a3f9", "preview-1-0.png", b"png")
        self.assertFalse(os.path.exists(target))

    def test_hardlinked_file_is_not_overwritten(self):
        os.mkdir(os.path.join(self.out, "d1101-a3f9"))
        victim = os.path.join(self.elsewhere, "victim")
        with open(victim, "wb") as file:
            file.write(b"keep")
        os.link(victim, os.path.join(self.out, "d1101-a3f9", "preview-1-0.png"))
        with self.assertRaises(FileExistsError):
            self.folders.write("d1101-a3f9", "preview-1-0.png", b"png")
        with open(victim, "rb") as file:
            self.assertEqual(file.read(), b"keep")

    def test_import_is_copied_into_staging(self):
        os.mkdir(os.path.join(self.inp, "d1101-a3f9"))
        with open(os.path.join(self.inp, "d1101-a3f9", "w.png"), "wb") as file:
            file.write(gate.PNG_MAGIC + b"data")
        staged = self.folders.stage_import("d1101-a3f9", "w.png")
        with open(os.path.join(self.stage, staged), "rb") as file:
            self.assertEqual(file.read(), gate.PNG_MAGIC + b"data")
        # Replacing the source afterwards does not change what Krita reads.
        with open(os.path.join(self.inp, "d1101-a3f9", "w.png"), "wb") as file:
            file.write(b"swapped")
        with open(os.path.join(self.stage, staged), "rb") as file:
            self.assertTrue(file.read().startswith(gate.PNG_MAGIC))

    def test_import_through_links_is_refused(self):
        secret = os.path.join(self.elsewhere, "secret.png")
        with open(secret, "wb") as file:
            file.write(gate.PNG_MAGIC + b"secret")
        os.mkdir(os.path.join(self.inp, "d1101-a3f9"))
        os.symlink(secret, os.path.join(self.inp, "d1101-a3f9", "w.png"))
        with self.assertRaises(OSError):
            self.folders.stage_import("d1101-a3f9", "w.png")
        os.symlink(self.elsewhere, os.path.join(self.inp, "job-link"))
        with self.assertRaises(OSError):
            self.folders.stage_import("job-link", "secret.png")
        self.assertEqual(os.listdir(self.stage), [] if not os.path.exists(os.path.join(self.stage, "d1101-a3f9"))
                         else os.listdir(self.stage))

    def test_import_must_be_a_small_png(self):
        os.mkdir(os.path.join(self.inp, "d1101-a3f9"))
        with open(os.path.join(self.inp, "d1101-a3f9", "w.png"), "wb") as file:
            file.write(b"not a png")
        with self.assertRaises(gate.Refused):
            self.folders.stage_import("d1101-a3f9", "w.png")
        os.mkfifo(os.path.join(self.inp, "d1101-a3f9", "fifo.png"))
        with self.assertRaises(gate.Refused):
            self.folders.stage_import("d1101-a3f9", "fifo.png")


@unittest.skipUnless(POSIX, "needs a Unix socket")
class SocketTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "gate.sock")
        self.saved = gate.READ_DEADLINE
        threading.Thread(target=gate.serve, args=(gate.Gate(FakeClient(), NoFolders()), self.path), daemon=True).start()
        for _ in range(50):
            if os.path.exists(self.path):
                break
            time.sleep(0.05)

    def tearDown(self):
        gate.READ_DEADLINE = self.saved
        self.tmp.cleanup()

    def send(self, payload):
        with socket.socket(socket.AF_UNIX) as conn:
            conn.connect(self.path)
            try:
                conn.sendall(payload)
            except OSError:
                pass
            return json.loads(conn.makefile().readline())

    def test_oversized_request_is_refused(self):
        big = json.dumps(batch(op("paint_path", preset_id="x" * 2_000_000))).encode() + b"\n"
        self.assertIn("larger than", self.send(big)["refused"])
        self.assertTrue(self.send((json.dumps(batch(op("clear_selection"))) + "\n").encode())["ok"])

    def test_slow_trickle_hits_the_absolute_deadline(self):
        """Rin 的复现：每次 recv 都在期限内，但一个字节一个字节地送，以前可以无限续时。"""
        gate.READ_DEADLINE = 0.5
        payload = (json.dumps(batch(op("clear_selection"))) + "\n").encode()
        with socket.socket(socket.AF_UNIX) as conn:
            conn.connect(self.path)
            started = time.monotonic()
            for byte in payload:
                try:
                    conn.sendall(bytes([byte]))
                except OSError:
                    break
                time.sleep(0.05)
                if time.monotonic() - started > 3:
                    break
            reply = json.loads(conn.makefile().readline())
        self.assertIn("not received", reply["refused"])
        self.assertLess(time.monotonic() - started, 3)

    def test_connection_cap(self):
        gate.READ_DEADLINE = 1.0
        held = []
        for _ in range(gate.MAX_CONNECTIONS):
            conn = socket.socket(socket.AF_UNIX)
            conn.connect(self.path)
            conn.sendall(b'{"job":')       # half a request: occupies a worker slot
            held.append(conn)
        time.sleep(0.1)
        threads_before = threading.active_count()
        with socket.socket(socket.AF_UNIX) as extra:
            extra.connect(self.path)
            reply = json.loads(extra.makefile().readline())
        self.assertIn("too many connections", reply["refused"])
        self.assertLessEqual(threading.active_count(), threads_before, "no extra worker thread")
        for conn in held:
            conn.close()


class DuplicateKeyTest(unittest.TestCase):
    def test_duplicate_keys_are_refused(self):
        for text in ('{"job":"d1101-a3f9","job":"x","ops":[]}',
                     '{"job":"d1101-a3f9","ops":[{"command":"clear_selection","command":"open_document"}]}',
                     '{"job":"d1101-a3f9","ops":[{"command":"paint_path","params":{"opacity":1,"opacity":0}}]}'):
            with self.assertRaises(gate.Refused):
                gate.parse_request(text)
        self.assertEqual(gate.parse_request('{"cancel":"x"}'), {"cancel": "x"})


class LateCancelTest(unittest.TestCase):
    def test_cancel_during_the_last_operation_is_reported_truthfully(self):
        """Rin 的复现：单一操作的批次，取消在执行中送到，以前最后仍回 ok:true。"""
        client = FakeClient(duration=0.3)
        g = gate.Gate(client, NoFolders())
        replies = {}
        worker = threading.Thread(target=lambda: replies.setdefault("a", g.run(batch(op("clear_selection")))))
        worker.start()
        time.sleep(0.1)
        self.assertTrue(g.cancel("d1101-a3f9")["ok"])
        worker.join(5)
        reply = replies["a"]
        self.assertFalse(reply["ok"])
        self.assertIn("too late", reply["stopped"])
        self.assertEqual(reply["results"][0]["state"], "succeeded", "the completed operation is still reported")


class StuckMarkerTest(unittest.TestCase):
    def setUp(self):
        self.saved = (gate.OP_WAIT, gate.SETTLE)
        gate.OP_WAIT, gate.SETTLE = 0.05, 0.1
        self.tmp = tempfile.TemporaryDirectory()
        self.marker = os.path.join(self.tmp.name, "stuck.json")

    def tearDown(self):
        gate.OP_WAIT, gate.SETTLE = self.saved
        self.tmp.cleanup()

    def test_stuck_survives_a_gate_restart_and_clears_when_krita_restarts(self):
        client = FakeClient(never_finish=True)
        gate.Gate(client, NoFolders(), stuck_file=self.marker).run(batch(op("clear_selection")))
        self.assertTrue(os.path.exists(self.marker))
        restarted = gate.Gate(client, NoFolders(), stuck_file=self.marker)  # a new gate process
        self.assertIsNotNone(restarted.stuck)
        self.assertTrue(restarted.run(batch(op("clear_selection"), job="next-job"))["busy"])
        client.status = lambda: {"instances": [{"instance_id": "inst2", "reachable": True}]}  # Krita restarted
        client.never_finish = False
        self.assertTrue(restarted.run(batch(op("clear_selection"), job="next-job"))["ok"])
        self.assertFalse(os.path.exists(self.marker))

    def test_unreadable_marker_stays_busy_until_a_person_clears_it(self):
        """Rin 的复现：损坏的标记以前会被当成 unknown 实例而自动清掉，然后照常执行。"""
        with open(self.marker, "w") as file:
            file.write("{broken")
        client = FakeClient()
        g = gate.Gate(client, NoFolders(), stuck_file=self.marker)
        self.assertEqual(g.stuck, gate.Gate.CORRUPT)
        for _ in range(2):
            reply = g.run(batch(op("clear_selection")))
            self.assertTrue(reply["busy"])
            self.assertIn("a person must check", reply["running"])
        client.status = lambda: {"instances": [{"instance_id": "inst9", "reachable": True}]}  # even after a Krita restart
        self.assertTrue(g.run(batch(op("clear_selection")))["busy"])
        self.assertEqual(client.calls, [], "nothing was sent to Krita")
        os.unlink(self.marker)  # the documented manual step, after checking Krita
        self.assertTrue(gate.Gate(client, NoFolders(), stuck_file=self.marker).run(batch(op("clear_selection")))["ok"])

    def test_marker_is_written_before_submitting_and_removed_after(self):
        seen = []
        client = FakeClient()
        original = client.execute

        def execute(*args, **kwargs):
            with open(self.marker) as file:
                seen.append(json.load(file)["state"])
            return original(*args, **kwargs)

        client.execute = execute
        g = gate.Gate(client, NoFolders(), stuck_file=self.marker)
        self.assertTrue(g.run(batch(op("clear_selection"), op("clear_selection")))["ok"])
        self.assertEqual(seen, ["in-flight", "in-flight"])
        self.assertFalse(os.path.exists(self.marker))

    def test_crash_after_submit_leaves_the_gate_busy_on_restart(self):
        client = FakeClient()
        g = gate.Gate(client, NoFolders(), stuck_file=self.marker)

        def crash(*args, **kwargs):
            raise SystemExit("gate killed right after submitting")

        client.execute = crash
        with self.assertRaises(SystemExit):
            g.run(batch(op("clear_selection")))
        restarted = gate.Gate(FakeClient(never_finish=True), NoFolders(), stuck_file=self.marker)
        self.assertIsNotNone(restarted.stuck)
        self.assertTrue(restarted.run(batch(op("clear_selection")))["busy"])

    def test_unknown_outcome_marks_stuck(self):
        client = FakeClient(never_finish=True)

        def unknown(*args, **kwargs):
            raise RuntimeError("OUTCOME_UNKNOWN: may have been admitted")

        client.execute = unknown
        g = gate.Gate(client, NoFolders(), stuck_file=self.marker)
        reply = g.run(batch(op("clear_selection"), op("clear_selection")))
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["results"][0]["state"], "unknown")
        self.assertTrue(os.path.exists(self.marker))
        self.assertTrue(g.run(batch(op("clear_selection"), job="next-job"))["busy"])

    def test_failed_cancel_request_still_leaves_the_gate_stuck(self):
        """Rin 第 5 轮：cancel_operation 自己抛例外时，以前内存没有 stuck，下一批照跑。"""
        client = FakeClient(never_finish=True)

        def cancel_fails(instance_id, operation_id):
            client.cancels.append(operation_id)
            raise ConnectionResetError("bridge went away during cancel")

        client.cancel_operation = cancel_fails
        g = gate.Gate(client, NoFolders(), stuck_file=self.marker)
        reply = g.run(batch(op("clear_selection"), op("clear_selection")))
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["results"][0]["state"], "unfinished")
        self.assertEqual(len(client.cancels), 1, "cancel was attempted")
        self.assertIsNotNone(g.stuck, "memory is stuck")
        with open(self.marker) as file:
            self.assertEqual(json.load(file)["state"], "stuck", "disk is stuck")
        self.assertTrue(g.run(batch(op("clear_selection"), job="next-job"))["busy"])
        self.assertEqual(len(client.calls), 1, "nothing new was sent while the first one may still run")
        client.never_finish = False                       # Krita ends it after all
        self.assertTrue(g.run(batch(op("clear_selection"), job="next-job"))["ok"])
        self.assertFalse(os.path.exists(self.marker))

    def test_failed_cancel_without_marker_file_is_stuck_too(self):
        client = FakeClient(never_finish=True)
        client.cancel_operation = lambda *a: (_ for _ in ()).throw(RuntimeError("cancel failed"))
        g = gate.Gate(client, NoFolders())
        g.run(batch(op("clear_selection")))
        self.assertIsNotNone(g.stuck)
        self.assertTrue(g.run(batch(op("clear_selection"), job="next-job"))["busy"])

    def test_memory_is_stuck_whenever_the_in_flight_marker_exists(self):
        """不变式：磁盘上有 in-flight 标记、且 Krita 还没确认结束时，内存一定是 stuck。"""
        client = FakeClient()
        g = gate.Gate(client, NoFolders(), stuck_file=self.marker)
        seen = []
        original = client.execute

        def execute(*args, **kwargs):
            seen.append((os.path.exists(self.marker), g.stuck is not None))
            return original(*args, **kwargs)

        client.execute = execute
        self.assertTrue(g.run(batch(op("clear_selection"), op("clear_selection")))["ok"])
        self.assertEqual(seen, [(True, True), (True, True)])
        self.assertIsNone(g.stuck)

    def test_failed_marker_write_leaves_memory_stuck_and_sends_nothing(self):
        client = FakeClient()
        g = gate.Gate(client, NoFolders(), stuck_file=os.path.join(self.tmp.name, "missing-dir", "stuck.json"))
        with self.assertRaises(OSError):
            g.run(batch(op("clear_selection")))
        self.assertEqual(client.calls, [], "nothing sent when the marker could not be written")
        self.assertIsNotNone(g.stuck)

    def test_failed_marker_removal_keeps_memory_stuck(self):
        from unittest import mock
        client = FakeClient()
        g = gate.Gate(client, NoFolders(), stuck_file=self.marker)
        with mock.patch.object(gate.os, "unlink", side_effect=PermissionError("read-only")):
            with self.assertRaises(PermissionError):
                g.run(batch(op("clear_selection")))
        self.assertTrue(os.path.exists(self.marker))
        self.assertIsNotNone(g.stuck, "disk still says in-flight, so memory must not say idle")


class ReserveTest(unittest.TestCase):
    """Rin 第 5 轮：固定 64 MiB 预留盖不住 8192² PNG（未压缩约 256 MiB）或大的 .kra。"""

    def test_png_worst_case_covers_uncompressed_pixels(self):
        for w, h in ((1, 1), (1200, 800), (4096, 4096), (8192, 8192)):
            self.assertGreater(gate.png_worst_bytes(w, h), w * h * 4 + h)
        self.assertGreater(gate.png_worst_bytes(8192, 8192), 256 * 1024 * 1024)
        self.assertLess(gate.png_worst_bytes(4096, 4096), gate.JOB_QUOTA_BYTES, "a full-size export always fits a job")

    def test_kra_worst_case_counts_every_layer_and_out_of_canvas_pixels(self):
        full = {"bounds": {"x": 0, "y": 0, "width": 4096, "height": 4096}}
        outside = {"bounds": {"x": -4096, "y": -4096, "width": 12288, "height": 12288}}
        one = gate.kra_worst_bytes(4096, 4096, [full])
        self.assertGreater(one, 2 * 4096 * 4096 * 4, "one layer + the merged image")
        self.assertGreater(gate.kra_worst_bytes(4096, 4096, [full] * 3), 4 * 4096 * 4096 * 4)
        self.assertGreater(gate.kra_worst_bytes(4096, 4096, [outside]), 12288 * 12288 * 4)
        empty = {"bounds": {"x": 0, "y": 0, "width": 0, "height": 0}}
        self.assertLess(gate.kra_worst_bytes(1200, 800, [empty] * 10), 8 * 1024 * 1024)

    def test_reserve_comes_from_the_inspected_document(self):
        client = FakeClient()
        client.document = {"width": 4096, "height": 4096,
                           "layers": [{"bounds": {"x": 0, "y": 0, "width": 4096, "height": 4096}}] * 2}
        folders = DirFolders()
        reply = gate.Gate(client, folders).run(batch(op("export_png", {"document_id": "doc1"}, file="a.png"),
                                                     op("save_document", {"document_id": "doc1"}, file="a.kra")))
        self.assertTrue(reply["ok"], reply)
        self.assertEqual([c[0] for c in client.calls],
                         ["inspect_document", "export_png", "inspect_document", "save_document"])
        self.assertEqual(client.calls[0][1], {"document_id": "doc1"})
        reserves = [r for r in folders.reserved if r]
        self.assertEqual(reserves[0], gate.png_worst_bytes(4096, 4096))
        self.assertGreater(reserves[1], 3 * 4096 * 4096 * 4, "two layers + merged image")

    def test_bad_or_oversized_document_description_refuses_the_save(self):
        for document in ({"width": 8192, "height": 8192, "layers": []}, {"width": "1200", "height": 800, "layers": []},
                         {"width": 1200, "height": 800}, {"width": 1200, "height": 800, "layers": [{"bounds": None}]},
                         {"width": 1200, "height": 800, "layers": [{"bounds": {"width": -1, "height": 5}}]}, None):
            client = FakeClient()
            client.document = document
            reply = gate.Gate(client, DirFolders()).run(batch(op("save_document", {"document_id": "doc1"}, file="a.kra")))
            self.assertFalse(reply["ok"], document)
            self.assertEqual(reply["results"][0]["state"], "refused", document)
            self.assertEqual([c[0] for c in client.calls], ["inspect_document"], "the save itself is never sent")

    def test_save_without_document_id_is_refused(self):
        client = FakeClient()
        reply = gate.Gate(client, DirFolders()).run(batch(op("save_document", {"node_id": "n1"}, file="a.kra")))
        self.assertEqual(reply["results"][0]["state"], "refused")
        self.assertEqual(client.calls, [])

    def test_inspect_that_never_ends_refuses_and_leaves_the_gate_stuck(self):
        saved = (gate.OP_WAIT, gate.SETTLE)
        gate.OP_WAIT, gate.SETTLE = 0.05, 0.1
        try:
            client = FakeClient(never_finish=True)
            g = gate.Gate(client, DirFolders())
            reply = g.run(batch(op("save_document", {"document_id": "doc1"}, file="a.kra")))
            self.assertEqual(reply["results"][0]["state"], "refused")
            self.assertEqual([c[0] for c in client.calls], ["inspect_document"])
            self.assertIsNotNone(g.stuck)
        finally:
            gate.OP_WAIT, gate.SETTLE = saved


@unittest.skipUnless(POSIX, "needs POSIX")
class QuotaTest(unittest.TestCase):
    def setUp(self):
        self.saved = (gate.JOB_QUOTA_BYTES, gate.TOTAL_QUOTA_BYTES, gate.MIN_FREE_BYTES)
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.out, self.inp, self.stage = (os.path.join(t, n) for n in ("out", "in", "staging"))
        for d in (self.out, self.inp, self.stage):
            os.mkdir(d)
        self.folders = gate.Folders(self.out, self.inp, self.stage)

    def tearDown(self):
        gate.JOB_QUOTA_BYTES, gate.TOTAL_QUOTA_BYTES, gate.MIN_FREE_BYTES = self.saved
        self.tmp.cleanup()

    def put(self, root, job, name, size, age=0):
        folder = os.path.join(root, job)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name)
        with open(path, "wb") as file:
            file.write(b"x" * size)
        if age:
            os.utime(path, (time.time() - age, time.time() - age))
        return path

    def test_cleanup_removes_only_old_staging_and_previews(self):
        old_stage = self.put(self.stage, "job-a", "a.png", 10, age=2 * 86400)
        new_stage = self.put(self.stage, "job-a", "b.png", 10)
        old_preview = self.put(self.out, "job-a", "preview-1-0.png", 10, age=8 * 86400)
        old_art = self.put(self.out, "job-a", "art.kra", 10, age=30 * 86400)
        usage = self.folders.housekeeping()
        self.assertFalse(os.path.exists(old_stage))
        self.assertFalse(os.path.exists(old_preview))
        self.assertTrue(os.path.exists(new_stage))
        self.assertTrue(os.path.exists(old_art), "saved artwork is never deleted")
        self.assertEqual(usage["removed"], 2)
        self.assertEqual(usage["jobs"]["job-a"], 20)

    def test_cleanup_leaves_other_users_files(self):
        if os.getuid() != 0:
            self.skipTest("needs root to create a file owned by another user")
        path = self.put(self.stage, "job-a", "x.png", 10, age=3 * 86400)
        os.chown(path, 65534, 65534)
        self.folders.housekeeping()
        self.assertTrue(os.path.exists(path))

    def test_quotas_and_free_space_refuse_new_work(self):
        self.put(self.out, "job-a", "art.kra", 1000)
        gate.JOB_QUOTA_BYTES = 500
        with self.assertRaises(gate.Refused):
            self.folders.check_room("job-a")
        self.folders.check_room("job-b")
        gate.TOTAL_QUOTA_BYTES = 500
        with self.assertRaises(gate.Refused):
            self.folders.check_room("job-b")
        gate.JOB_QUOTA_BYTES, gate.TOTAL_QUOTA_BYTES = self.saved[:2]
        gate.MIN_FREE_BYTES = 1 << 60
        with self.assertRaises(gate.Refused):
            self.folders.check_room("job-b")

    def test_quota_is_checked_before_every_import_in_a_batch(self):
        """Rin 的复现：以前只在批次开头检查，50 个 16 MiB 汇入可以一口气写 800 MiB。"""
        size = 1000
        os.mkdir(os.path.join(self.inp, "d1101-a3f9"))
        with open(os.path.join(self.inp, "d1101-a3f9", "w.png"), "wb") as file:
            file.write(gate.PNG_MAGIC + b"x" * (size - len(gate.PNG_MAGIC)))
        gate.JOB_QUOTA_BYTES = 3500
        client = FakeClient()
        reply = gate.Gate(client, self.folders).run(batch(*[op("import_image_layer", {"document_id": "d"}, file="w.png",
                                                              name="w", x=0, y=0)] * 50))
        self.assertFalse(reply["ok"])
        self.assertIn("refused before sending", reply["stopped"])
        self.assertEqual(len(client.calls), 3, "three imports fit in 3500 bytes, the fourth is refused")
        staged = sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fs in os.walk(self.stage) for f in fs)
        self.assertLessEqual(staged, gate.JOB_QUOTA_BYTES)

    def test_total_quota_is_checked_per_operation(self):
        self.put(self.out, "other-job", "art.kra", 1000)
        gate.TOTAL_QUOTA_BYTES = 1000 + gate.PREVIEW_RESERVE - 1
        client = FakeClient()
        reply = gate.Gate(client, self.folders).run(batch(op("clear_selection"), op("get_preview", {"document_id": "d"})))
        self.assertFalse(reply["ok"])
        self.assertEqual([c[0] for c in client.calls], ["clear_selection"], "the preview is refused before it is sent")

    def test_large_save_is_refused_by_the_job_quota_before_it_is_sent(self):
        client = FakeClient()
        client.document = {"width": 4096, "height": 4096,
                           "layers": [{"bounds": {"x": 0, "y": 0, "width": 4096, "height": 4096}}] * 3}
        g = gate.Gate(client, self.folders)
        reply = g.run(batch(op("save_document", {"document_id": "doc1"}, file="a.kra")))
        self.assertFalse(reply["ok"])
        self.assertIn("quota", reply["stopped"])
        self.assertEqual([c[0] for c in client.calls], ["inspect_document"])
        reply = g.run(batch(op("export_png", {"document_id": "doc1"}, file="a.png")))
        self.assertTrue(reply["ok"], "one full-size PNG export fits")

    def test_gate_checks_room_before_file_work(self):
        gate.MIN_FREE_BYTES = 1 << 60
        client = FakeClient()
        g = gate.Gate(client, self.folders)
        reply = g.run(batch(op("save_document", {"document_id": "d"}, file="a.kra")))
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["results"][0]["state"], "refused")
        self.assertEqual(client.calls, [], "nothing sent to Krita")
        self.assertTrue(g.run(batch(op("clear_selection")))["ok"], "painting alone is not blocked")


class NetcheckTest(unittest.TestCase):
    """Only EPERM/EACCES or a timeout may count as blocked; DNS and every other failure is a FAIL."""

    def test_classification(self):
        import errno as e
        import netcheck
        cases = [
            (socket.gaierror(-2, "Name or service not known"), False),
            (socket.gaierror(-3, "Temporary failure in name resolution"), False),
            (OSError(e.ENETUNREACH, "unreachable"), False),
            (OSError(e.EHOSTUNREACH, "no route"), False),
            (ConnectionRefusedError(e.ECONNREFUSED, "refused"), False),
            (ConnectionResetError(e.ECONNRESET, "reset"), False),
            (OSError(e.EADDRNOTAVAIL, "addr"), False),
            (ValueError("weird"), False),
            (PermissionError(e.EPERM, "Operation not permitted"), True),
            (OSError(e.EACCES, "denied"), True),
            (TimeoutError("timed out"), True),
            (socket.timeout("timed out"), True),
        ]
        for error, blocked in cases:
            self.assertEqual(netcheck.classify(error) in netcheck.BLOCKED, blocked, repr(error))

    def test_connect_uses_the_classifier_and_sandbox_fails_on_dns(self):
        import netcheck

        class Sock:
            def __init__(self, *a):
                pass

            def settimeout(self, t):
                pass

            def connect(self, addr):
                raise socket.gaierror(-3, "Temporary failure in name resolution")

            def close(self):
                pass

        self.assertTrue(netcheck.connect("1.1.1.1", 443, make_socket=Sock).startswith("FAIL_DNS"))
        saved = netcheck.connect
        netcheck.connect = lambda host, port, timeout=5: "FAIL_DNS(-3)" if host != "127.0.0.1" else "REFUSED_BY_REMOTE"
        try:
            here = os.path.dirname(os.path.abspath(__file__))
            self.assertFalse(netcheck.main("sandbox", os.path.join(here, "krita.service")))
        finally:
            netcheck.connect = saved

    def test_targets_are_literal_ips(self):
        import ipaddress
        import netcheck
        for host, _ in netcheck.TARGETS:
            ipaddress.ip_address(host)

    def test_hidden_paths_come_from_the_unit_file(self):
        """Rin 第 5 轮：以前写死的清单漏了 /run/dsh、/var/log/dsh。"""
        import netcheck
        here = os.path.dirname(os.path.abspath(__file__))
        paths = netcheck.hidden_paths(os.path.join(here, "krita.service"))
        with open(os.path.join(here, "krita.service"), encoding="utf-8") as file:
            expected = [item.lstrip("-") for line in file if line.startswith("InaccessiblePaths=")
                        for item in line.split("=", 1)[1].split()]
        self.assertEqual(paths, expected)
        for path in ("/srv/dsh", "/var/lib/dsh", "/opt/dsh", "/root", "/etc/dsh", "/var/backups/dsh",
                     "/var/log/dsh", "/run/dsh", "/srv/krita-io/in"):
            self.assertIn(path, paths)

    def test_unit_reader_refuses_what_it_cannot_be_sure_about(self):
        import netcheck
        with tempfile.TemporaryDirectory() as folder:
            unit = os.path.join(folder, "x.service")
            for body in ("[Service]\nUser=krita\n", "[Service]\nInaccessiblePaths=\n",
                         '[Service]\nInaccessiblePaths="/srv/dsh"\n', "[Service]\nInaccessiblePaths=%h/dsh\n",
                         "[Service]\nInaccessiblePaths=/srv/dsh \\\n  /opt/dsh\n", "[Service]\nInaccessiblePaths=srv\n"):
                with open(unit, "w", encoding="utf-8") as file:
                    file.write(body)
                with self.assertRaises(ValueError, msg=body):
                    netcheck.hidden_paths(unit)
            with open(unit, "w", encoding="utf-8") as file:
                file.write("[Unit]\nInaccessiblePaths=/nope\n[Service]\nInaccessiblePaths=/a -/b\nInaccessiblePaths=+/c\n")
            self.assertEqual(netcheck.hidden_paths(unit), ["/a", "/b", "/c"])
            self.assertFalse(netcheck.main("sandbox", os.path.join(folder, "missing.service")), "no unit: FAIL")

    def test_sandbox_fails_when_a_listed_path_is_readable(self):
        import netcheck
        with tempfile.TemporaryDirectory() as folder:
            readable = os.path.join(folder, "visible")
            os.mkdir(readable)
            unit = os.path.join(folder, "x.service")
            with open(unit, "w", encoding="utf-8") as file:
                file.write(f"[Service]\nInaccessiblePaths=-{os.path.join(folder, 'absent')}\n"
                           f"InaccessiblePaths={readable.replace(os.sep, '/')}\n")
            saved = netcheck.connect
            netcheck.connect = lambda host, port, timeout=5: "REFUSED_BY_REMOTE" if host == "127.0.0.1" else "DROPPED"
            try:
                if os.name == "posix":
                    self.assertFalse(netcheck.main("sandbox", unit), "a readable path is a leak")
                    os.rmdir(readable)
                    self.assertTrue(netcheck.main("sandbox", unit), "everything hidden and blocked")
            finally:
                netcheck.connect = saved


class UnitPropsTest(unittest.TestCase):
    def test_probe_sandbox_is_generated_from_the_service(self):
        import unit_props
        here = os.path.dirname(os.path.abspath(__file__))
        unit = os.path.join(here, "krita.service")
        props = unit_props.props(unit)
        keys = {line.split("=", 1)[0] for line in props}
        for required in ("User", "Group", "SupplementaryGroups", "UMask", "BindPaths", "InaccessiblePaths",
                         "LockPersonality", "ProtectClock", "ProtectSystem", "ProtectHome", "PrivateTmp",
                         "PrivateDevices", "ReadWritePaths", "IPAddressDeny", "IPAddressAllow", "MemoryMax",
                         "MemoryHigh", "NoNewPrivileges", "RestrictAddressFamilies", "Environment"):
            self.assertIn(required, keys)
        self.assertIn("InaccessiblePaths=-/srv/krita-io/in", props)
        self.assertNotIn("ExecStart", keys)
        self.assertFalse({"IPAddressDeny", "IPAddressAllow"} & {l.split("=", 1)[0] for l in unit_props.props(unit, ip_filter=False)})
        # Every [Service] line of the unit is either copied or deliberately skipped.
        with open(unit, encoding="utf-8") as file:
            service = [l.strip() for l in file.read().split("[Service]")[1].split("[Install]")[0].splitlines()
                       if l.strip() and not l.strip().startswith("#")]
        self.assertEqual([l for l in service if l not in props and l.split("=", 1)[0] not in unit_props.SKIP], [])

    def test_environment_survives_systemd_unquoting(self):
        """阶段 1 实测：Environment=KRITA6_MCP_INPUT_ROOTS={"in":...} 被 systemd 去掉双引号，桥接拒绝启动；
        另外解开的 AppImage 不设 APPDIR，Krita 内建的 Python 找不到标准库。"""
        import shlex
        here = os.path.dirname(os.path.abspath(__file__))
        env, exec_start = {}, None
        with open(os.path.join(here, "krita.service"), encoding="utf-8") as file:
            for line in file:
                line = line.strip()
                if line.startswith("Environment="):
                    # systemd splits and unquotes Environment= like a shell word list (for these values)
                    for item in shlex.split(line.split("=", 1)[1]):
                        key, _, value = item.partition("=")
                        env[key] = value
                elif line.startswith("ExecStart="):
                    exec_start = line
        for key in ("KRITA6_MCP_INPUT_ROOTS", "KRITA6_MCP_OUTPUT_ROOTS"):
            roots = json.loads(env[key])
            self.assertIsInstance(roots, dict, key)
            self.assertTrue(all(isinstance(v, str) and v.startswith("/") for v in roots.values()), key)
        app_run = next(word for word in shlex.split(exec_start.split("=", 1)[1]) if word.endswith("/AppRun"))
        self.assertEqual(env.get("APPDIR"), os.path.dirname(app_run), "APPDIR must be the AppRun folder")
        self.assertNotIn("current", env["APPDIR"].split("/"), "阶段 2 实测：APPDIR 用 current 连结路径不行，要真实目录")

    def test_krita_output_is_not_group_writable(self):
        """阶段 2 实测：UMask=0007 时 Krita 写出的 .kra/.png 是 660，dsh 经 kritaio 能改写成品。"""
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "krita.service"), encoding="utf-8") as file:
            umask = [l.strip().split("=", 1)[1] for l in file if l.strip().startswith("UMask=")]
        self.assertEqual(umask, ["0027"])


if __name__ == "__main__":
    unittest.main()
