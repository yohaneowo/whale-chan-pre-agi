#!/usr/bin/env python3
"""krita-gate: the only door from the dsh bot to Krita (Linux only).

Krita runs the krita6-mcp bridge plugin as the `krita` user. The bridge's bearer token sits in a
private discovery file (mode 0600, owned by krita; the upstream client refuses any other owner),
so the bot cannot talk to Krita directly. This service, also running as `krita`, reads that
token and listens on a Unix socket that only the `kritaio` group (krita + dsh) may open.

Requests (one JSON object per connection, newline-terminated, at most MAX_REQUEST_BYTES, fully
received within READ_DEADLINE seconds):

    {"job": "d1101-a3f9", "ops": [{"command": "paint_path", "target": {...}, "params": {...}}]}
    {"cancel": "d1101-a3f9"}

Rules:
- Only whitelisted commands; every parameter has an expected type, shape and numeric range.
- File paths never come from the request. save_document / export_png write out/<job>/<file>.
  import_image_layer reads in/<job>/<file> (dsh can write there), so the gate first copies it with
  O_NOFOLLOW into a krita-only staging folder and Krita imports the copy.
- Previews are written by the gate with O_NOFOLLOW / O_EXCL into out/<job>/, a folder it owns.
- One batch at a time; another batch gets {"busy": true} at once. At most MAX_CONNECTIONS
  connections are served at once; more are refused immediately.

Timing and cancelling (what really happens):
- Each operation waits at most min(OP_WAIT, time left of BATCH_DEADLINE). If it is not finished
  by then, the gate asks Krita to cancel it and waits up to SETTLE more seconds for Krita to
  finish or drop it. Krita cannot interrupt a native stroke already running, so cancelling stops
  everything after the current operation, never the stroke in progress.
- The batch lock is held until the current operation has really ended. If Krita still has not
  finished it after SETTLE, the gate remembers it and answers busy until Krita reports it done.
- {"cancel": job} marks the batch cancelled and asks Krita to cancel the operation in progress.
"""

import errno
import json
import math
import os
import re
import socket
import stat
import sys
import threading
import time
import uuid

MAX_OPS = 50
MAX_POINTS_PER_BATCH = 20000
MAX_POINTS_PER_OP = 2048
MAX_SEGMENTS_PER_OP = 256
MAX_REQUEST_BYTES = 1_000_000
MAX_PARAMS_BYTES = 64_000
MAX_STRING = 256
MAX_IMPORT_BYTES = 16 * 1024 * 1024
MAX_CONNECTIONS = 6
READ_DEADLINE = 10.0          # absolute, per connection
OP_WAIT = 30.0
SETTLE = 30.0
BATCH_DEADLINE = 180.0
JOB_QUOTA_BYTES = 200 * 1024 * 1024         # out/<job> + staging/<job>
TOTAL_QUOTA_BYTES = 2 * 1024 * 1024 * 1024  # all of out/ + staging/
MIN_FREE_BYTES = 2 * 1024 * 1024 * 1024     # refuse new work below this much free disk
STAGING_MAX_AGE = 24 * 3600                 # staged copies are only needed during one batch
PREVIEW_MAX_AGE = 7 * 24 * 3600             # previews; saved .kra / exported PNG are kept
# Space reserved before an operation whose output size is only known afterwards. The real size is
# counted again before the next operation, so a batch can never run past a quota by more than one
# reservation.
PREVIEW_RESERVE = 16 * 1024 * 1024          # bridge artifacts are at most 16 MiB
# Saves and exports have no fixed reserve: before each one the gate asks Krita for the document's
# size and every layer's bounds (inspect_document) and reserves the worst case for exactly that
# document (png_worst_bytes / kra_worst_bytes below). The canvas is capped at 4096 x 4096 so that one
# full-size PNG export (about 65 MiB worst case) always fits a job's quota.
MAX_CANVAS_SIDE = 4096
TILE = 64                                   # Krita stores layer pixels in 64 x 64 tiles
TILE_HEADER_BYTES = 64                      # per-tile header line + compression flag in a .kra
NODE_EXTRA_BYTES = 64 * 1024                # per-node XML, metadata, default pixel, masks' small files
FILE_EXTRA_BYTES = 1024 * 1024              # IHDR / ICC profile / text chunks; maindoc.xml, zip directory
COORD = 16384.0
JOB = re.compile(r"[a-z0-9][a-z0-9-]{2,40}\Z")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,80}\.(png|kra)\Z", re.IGNORECASE)
COLOR = re.compile(r"#[0-9A-Fa-f]{6}\Z")
IDENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
PREVIEW_NAME = re.compile(r"preview-[0-9]+-[0-9]+\.png\Z")

READS = {"list_documents", "inspect_document", "list_brush_presets", "inspect_brush",
         "get_preview", "get_layer_preview", "get_region_preview", "sample_color"}
PAINT = {"paint_path", "paint_line", "paint_bezier_path", "paint_shape", "fill_layer"}
LAYERS = {"create_document", "activate_document", "create_paint_layer", "create_group_layer",
          "set_layer_properties", "move_layer", "copy_layer", "delete_layer", "merge_layer_down",
          "transform_layer", "create_transparency_mask", "set_transparency_mask",
          "set_selection", "clear_selection", "modify_selection", "edit_history"}
FILES = {"import_image_layer": ("in", ".png"), "save_document": ("out", ".kra"),
         "export_png": ("out", ".png")}
ALLOWED = READS | PAINT | LAYERS | set(FILES)
PREVIEWS = {"get_preview", "get_layer_preview", "get_region_preview"}
TERMINAL = {"succeeded", "failed", "cancelled", "expired"}

# Numeric parameters: (low, high, integer?). Anything numeric not listed here is refused.
RANGES = {
    "x": (0, MAX_CANVAS_SIDE, True), "y": (0, MAX_CANVAS_SIDE, True),
    "width": (1, MAX_CANVAS_SIDE, True), "height": (1, MAX_CANVAS_SIDE, True),
    "size_px": (0.1, 1000, False), "opacity": (0, 1, False), "pressure_start": (0, 1, False),
    "pressure_end": (0, 1, False), "radius": (1, 256, True), "max_edge": (32, 1024, True),
    "translate_x": (-COORD, COORD, False), "translate_y": (-COORD, COORD, False),
    "scale_x": (0.01, 16, False), "scale_y": (0.01, 16, False), "rotation_degrees": (-360, 360, False),
    "degrees": (-180, 180, False), "offset": (0, 100000, True), "limit": (1, 100, True),
}
BOOLS = {"fill", "overwrite", "visible", "inherit_alpha", "alpha_locked"}
COLORS = {"color", "end_color", "start_color"}
IDS = {"preset_id", "parent_node_id", "above_node_id", "destination_document_id", "node_id",
       "document_id"}
WORDS = {"kind", "shape", "mode", "action", "direction", "source", "blending_mode", "filter", "axis",
         "query", "name", "type"}
POINT_LISTS = {"points"}             # [[x, y], ...]
PAIRS = {"start", "end", "pivot", "point"}   # [x, y]


class Refused(Exception):
    pass


def _no_duplicates(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise Refused(f"duplicate JSON key {key!r}")
        seen[key] = value
    return seen


def parse_request(data):
    """json.loads that refuses duplicate keys, so what is audited is exactly what runs."""
    return json.loads(data, object_pairs_hook=_no_duplicates)


def _finite(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value) and abs(value) <= 1e9
    except OverflowError:
        return False


def _pair(value):
    return (isinstance(value, list) and len(value) == 2
            and all(_finite(v) and -COORD <= v <= COORD for v in value))


def _check_value(index, key, value, nested=False):
    """Return how many points the value holds; raise Refused if it is not what Krita expects."""
    if key in RANGES:
        low, high, integer = RANGES[key]
        if not _finite(value) or (integer and type(value) is not int) or not low <= value <= high:
            raise Refused(f"op {index}: {key} must be {'an integer' if integer else 'a number'} in {low}..{high}")
        return 0
    if key in BOOLS:
        if type(value) is not bool:
            raise Refused(f"op {index}: {key} must be true/false")
        return 0
    if key in COLORS:
        if not isinstance(value, str) or not COLOR.match(value):
            raise Refused(f"op {index}: {key} must be #RRGGBB")
        return 0
    if key in IDS:
        if not isinstance(value, str) or not IDENT.match(value):
            raise Refused(f"op {index}: {key} must be an identifier")
        return 0
    if key in WORDS and isinstance(value, str):
        if len(value) > MAX_STRING or "\x00" in value:
            raise Refused(f"op {index}: {key} is too long")
        return 0
    if key in POINT_LISTS:
        if not isinstance(value, list) or not 2 <= len(value) <= MAX_POINTS_PER_OP or not all(_pair(p) for p in value):
            raise Refused(f"op {index}: {key} must be 2..{MAX_POINTS_PER_OP} [x, y] pairs within ±{COORD:.0f}")
        return len(value)
    if key == "segments":
        if (not isinstance(value, list) or not 1 <= len(value) <= MAX_SEGMENTS_PER_OP
                or not all(isinstance(s, list) and len(s) == 3 and all(_pair(p) for p in s) for s in value)):
            raise Refused(f"op {index}: segments must be 1..{MAX_SEGMENTS_PER_OP} triples of [x, y] pairs")
        return 3 * len(value)
    if key in PAIRS:
        if not _pair(value):
            raise Refused(f"op {index}: {key} must be one [x, y] pair within ±{COORD:.0f}")
        return 1
    if key == "shape" and isinstance(value, dict) and not nested:
        return sum(_check_value(index, inner_key, inner, nested=True) for inner_key, inner in value.items())
    raise Refused(f"op {index}: parameter {key!r} is not allowed here")


def check_batch(request):
    """Validate a batch and rewrite its file parameters. Returns (job, ops) or raises Refused."""
    if not isinstance(request, dict) or set(request) - {"job", "ops"}:
        raise Refused("request must be {job, ops}")
    job = request.get("job")
    if not isinstance(job, str) or not JOB.match(job):
        raise Refused("job must match " + JOB.pattern)
    ops = request.get("ops")
    if not isinstance(ops, list) or not 1 <= len(ops) <= MAX_OPS:
        raise Refused(f"ops must be a list of 1..{MAX_OPS}")
    checked, points = [], 0
    for index, op in enumerate(ops):
        if not isinstance(op, dict) or set(op) - {"command", "target", "params"}:
            raise Refused(f"op {index}: only command, target, params are allowed")
        command = op.get("command")
        if command not in ALLOWED:
            raise Refused(f"op {index}: command {command!r} is not allowed")
        target = op.get("target") or {}
        params = dict(op.get("params") or {})
        if not isinstance(target, dict) or not isinstance(params, dict):
            raise Refused(f"op {index}: target and params must be objects")
        for key, value in target.items():
            if key not in ("document_id", "node_id") or not isinstance(value, str) or not IDENT.match(value):
                raise Refused(f"op {index}: target {key!r} must be a document_id/node_id identifier")
        try:
            size = len(json.dumps(params))
        except (ValueError, OverflowError):
            raise Refused(f"op {index}: params are not plain JSON") from None
        if size > MAX_PARAMS_BYTES:
            raise Refused(f"op {index}: params are larger than {MAX_PARAMS_BYTES} bytes")
        name = None
        if command in FILES:
            name = params.pop("file", None)
            suffix = FILES[command][1]
            if not isinstance(name, str) or not NAME.match(name) or not name.lower().endswith(suffix):
                raise Refused(f"op {index}: {command} needs a plain {suffix} file name")
        for key, value in params.items():
            if key in ("root", "path"):
                raise Refused(f"op {index}: root and path are set by the gate")
            points += _check_value(index, key, value)
        if points > MAX_POINTS_PER_BATCH:
            raise Refused(f"at most {MAX_POINTS_PER_BATCH} points per batch")
        if name is not None:
            params["root"] = FILES[command][0]
            params["path"] = f"{job}/{name}"
        checked.append({"command": command, "target": target, "params": params})
    return job, checked


def deflate_bound(size):
    """zlib's deflateBound() with its 6-byte wrapper: the most deflate can ever turn `size` bytes into."""
    return size + (size >> 12) + (size >> 14) + (size >> 25) + 13 + 6


def png_worst_bytes(width, height):
    """Largest PNG Krita can write for an RGBA 8-bit image: every row is raw pixels plus one filter
    byte, compressed data can grow by deflate_bound, each IDAT chunk (8 KiB, libpng's default) adds
    12 bytes, plus a fixed allowance for the header, ICC profile and text chunks."""
    compressed = deflate_bound(height * (1 + 4 * width))
    return compressed + 12 * (compressed // 8192 + 1) + FILE_EXTRA_BYTES


def kra_worst_bytes(width, height, layers):
    """Largest .kra for a document of this size with these layers. Each node's pixels are counted as
    raw RGBA tiles over its bounds (bounds can reach outside the canvas; unaligned bounds touch one
    extra tile per side), even group layers and masks, so this over-counts rather than under-counts.
    A .kra also holds the merged image and a preview PNG, and the whole zip may be deflated."""
    pixels = 0
    for node in layers:
        w, h = node["bounds"]["width"], node["bounds"]["height"]
        if w > 0 and h > 0:
            pixels += (w // TILE + 2) * (h // TILE + 2) * (TILE * TILE * 4 + TILE_HEADER_BYTES)
        pixels += NODE_EXTRA_BYTES
    return deflate_bound(pixels + png_worst_bytes(width, height) + png_worst_bytes(256, 256)) + FILE_EXTRA_BYTES


def _plain_int(value, low, high):
    return type(value) is int and low <= value <= high


def output_reserve(command, document):
    """Bytes to reserve before save_document / export_png, from Krita's inspect_document result.
    Anything unexpected in that result refuses the save (fail closed)."""
    if not isinstance(document, dict):
        raise Refused("Krita's description of the document is not an object")
    width, height, layers = document.get("width"), document.get("height"), document.get("layers")
    if not _plain_int(width, 1, MAX_CANVAS_SIDE) or not _plain_int(height, 1, MAX_CANVAS_SIDE):
        raise Refused(f"the document is {width!r} x {height!r}; the gate only saves documents up to "
                      f"{MAX_CANVAS_SIDE} x {MAX_CANVAS_SIDE}")
    if command == "export_png":
        return png_worst_bytes(width, height)
    if not isinstance(layers, list):
        raise Refused("Krita did not list the document's layers")
    for node in layers:
        bounds = node.get("bounds") if isinstance(node, dict) else None
        if (not isinstance(bounds, dict) or not _plain_int(bounds.get("width"), 0, 1 << 24)
                or not _plain_int(bounds.get("height"), 0, 1 << 24)):
            raise Refused("Krita reported a layer without usable bounds")
    return kra_worst_bytes(width, height, layers)


class Folders:
    """All file work of the gate, done without following links.

    out_root: krita-owned; job folders and previews are created here (dsh can only read).
    in_root:  dsh-owned; read only, every component opened with O_NOFOLLOW.
    staging:  krita-only (0700); imports are copied here and Krita reads the copy.
    """

    def __init__(self, out_root, in_root=None, staging=None):
        self.out_root, self.in_root, self.staging = out_root, in_root, staging

    @staticmethod
    def _open_dir(path, dir_fd=None, owned=True):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or (owned and info.st_uid != os.getuid()):
            os.close(fd)
            raise Refused(f"{path} is not a folder owned by the gate")
        return fd

    def _job_dir(self, root_path, job):
        if not JOB.match(job):
            raise Refused("bad job")
        root = self._open_dir(root_path)
        try:
            try:
                # No setgid bit in the mode: krita-gate.service has RestrictSUIDSGID=yes, which makes an
                # explicit S_ISGID fail with EPERM (phase 2, 2026-11-02). out/ is setgid kritaio, so the
                # kernel gives the new folder the group and the setgid bit anyway; dsh can read it.
                os.mkdir(job, 0o750, dir_fd=root)
            except FileExistsError:
                pass
            return self._open_dir(job, dir_fd=root)
        finally:
            os.close(root)

    def job_dir(self, job):
        """Open (creating if needed) out/<job>; returns a directory fd the caller must close."""
        return self._job_dir(self.out_root, job)

    @staticmethod
    def _create(folder_fd, name, data):
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o640,
                     dir_fd=folder_fd)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise Refused("not a fresh regular file")
            os.write(fd, data)
        finally:
            os.close(fd)

    def write(self, job, name, data):
        if not NAME.match(name):
            raise Refused("bad file name")
        folder = self.job_dir(job)
        try:
            self._create(folder, name, data)
        finally:
            os.close(folder)
        return f"{job}/{name}"

    def _walk(self, root_path, cutoff=None, pattern=None, now=None):
        """Size of regular files under root_path/<job>/ (no links followed). With cutoff, first delete
        this uid's own regular files older than cutoff whose name matches pattern (any name if
        pattern is None), and job folders it owns that end up empty. Returns (bytes, deleted, per_job)."""
        total, deleted, per_job = 0, 0, {}
        uid = os.getuid()
        now = now if now is not None else time.time()
        try:
            root = self._open_dir(root_path, owned=False)
        except (OSError, Refused):
            return 0, 0, {}
        try:
            for job in os.listdir(root):
                try:
                    folder = os.open(job, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=root)
                except OSError:
                    continue
                try:
                    folder_owned = os.fstat(folder).st_uid == uid
                    for name in os.listdir(folder):
                        info = os.stat(name, dir_fd=folder, follow_symlinks=False)
                        if not stat.S_ISREG(info.st_mode):
                            continue
                        old = cutoff is not None and now - info.st_mtime > cutoff
                        if old and info.st_uid == uid and (pattern is None or pattern.match(name)):
                            os.unlink(name, dir_fd=folder)
                            deleted += 1
                            continue
                        total += info.st_size
                        per_job[job] = per_job.get(job, 0) + info.st_size
                    empty = not os.listdir(folder)
                finally:
                    os.close(folder)
                if folder_owned and cutoff is not None and empty:
                    os.rmdir(job, dir_fd=root)
        finally:
            os.close(root)
        return total, deleted, per_job

    def housekeeping(self, now=None):
        """Delete only what this uid created and no longer needs: staged copies older than a day,
        previews older than a week (saved .kra and exported PNG stay). Returns usage after cleaning."""
        staged, removed_staged, staged_jobs = (self._walk(self.staging, STAGING_MAX_AGE, None, now)
                                               if self.staging else (0, 0, {}))
        out, removed_previews, out_jobs = self._walk(self.out_root, PREVIEW_MAX_AGE, PREVIEW_NAME, now)
        jobs = dict(out_jobs)
        for job, size in staged_jobs.items():
            jobs[job] = jobs.get(job, 0) + size
        return {"bytes": staged + out, "jobs": jobs, "removed": removed_staged + removed_previews}

    @staticmethod
    def _free(path):
        info = os.statvfs(path)
        return info.f_bavail * info.f_frsize

    def check_room(self, job, adding=0, now=None):
        """Refuse a file-producing operation unless current usage plus `adding` bytes stays within
        the job quota, the total quota and the free-disk floor. Called before every such operation,
        with usage measured again each time."""
        usage = self.housekeeping(now)
        free = min(self._free(path) for path in (self.out_root, self.staging) if path)
        if free - adding < MIN_FREE_BYTES:
            raise Refused(f"disk almost full ({free // 1048576} MB free); not writing {adding // 1048576} MB more")
        if usage["bytes"] + adding > TOTAL_QUOTA_BYTES:
            raise Refused(f"Krita folders use {usage['bytes'] // 1048576} MB; {adding // 1048576} MB more would pass "
                          f"the {TOTAL_QUOTA_BYTES // 1048576} MB quota")
        if usage["jobs"].get(job, 0) + adding > JOB_QUOTA_BYTES:
            raise Refused(f"job {job} uses {usage['jobs'].get(job, 0) // 1048576} MB; {adding // 1048576} MB more "
                          f"would pass its {JOB_QUOTA_BYTES // 1048576} MB quota")
        return usage

    def stage_import(self, job, name):
        """Copy in/<job>/<name> into staging/<job>/<unique>.png; return the relative staging path."""
        if not NAME.match(name) or not name.lower().endswith(".png"):
            raise Refused("bad import name")
        root = self._open_dir(self.in_root, owned=False)
        try:
            folder = self._open_dir(job, dir_fd=root, owned=False)
        finally:
            os.close(root)
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=folder)
        finally:
            os.close(folder)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_IMPORT_BYTES:
                raise Refused("import must be a regular file up to 16 MB")
            data = os.read(fd, MAX_IMPORT_BYTES + 1)
        finally:
            os.close(fd)
        if len(data) > MAX_IMPORT_BYTES or not data.startswith(PNG_MAGIC):
            raise Refused("import must be a PNG up to 16 MB")
        self.check_room(job, adding=len(data))
        staged = f"{uuid.uuid4().hex}.png"
        target = self._job_dir(self.staging, job)
        try:
            self._create(target, staged, data)
        finally:
            os.close(target)
        return f"{job}/{staged}"


class Gate:
    def __init__(self, client, folders, clock=time.monotonic, sleep=time.sleep, stuck_file=None):
        self.client, self.folders, self.clock, self.sleep = client, folders, clock, sleep
        self.lock = threading.Lock()
        self.running = None              # job id of the batch in progress
        self.current = None              # (instance, operation id) in progress
        self.cancelled = set()
        # One marker file covers both cases, so a crash at any moment leaves a trace:
        #   in-flight: written BEFORE an operation is submitted, removed once Krita reports it ended;
        #   stuck:     Krita did not end it in time.
        # A leftover marker at start means "Krita may still be running something": busy until cleared.
        self.stuck_file = stuck_file
        self.stuck = self._load_stuck()  # (instance, operation id) or CORRUPT

    CORRUPT = ("corrupt", "corrupt")

    def _load_stuck(self):
        if not self.stuck_file:
            return None
        try:
            with open(self.stuck_file, encoding="utf-8") as file:
                data = json.load(file)
            return (str(data["instance"]), str(data["operation"]))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError):
            # Unreadable marker: stay busy until a person has checked Krita and removed it (KRITA.md).
            return self.CORRUPT

    def _write_marker(self, value, state):
        # Memory goes busy FIRST, for both states: from the moment an operation may reach Krita until
        # Krita confirms it ended, the gate is stuck in memory as well as on disk (fail closed).
        self.stuck = value
        if not self.stuck_file:
            return
        temporary = self.stuck_file + ".part"
        with open(temporary, "w", encoding="utf-8") as file:
            json.dump({"instance": value[0], "operation": value[1], "state": state, "since": time.time()}, file)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, self.stuck_file)

    def _set_stuck(self, value):
        if value is not None:
            self._write_marker(value, "stuck")
            return
        # Remove the marker before clearing memory: if the unlink fails, memory stays busy too.
        if self.stuck_file:
            try:
                os.unlink(self.stuck_file)
            except FileNotFoundError:
                pass
        self.stuck = None

    def _stuck_cleared(self):
        """True once it is safe to run again: the marked operation has ended, or the Krita instance
        that ran it is gone. A corrupt marker never clears by itself."""
        if self.stuck == self.CORRUPT:
            return False
        instance, operation = self.stuck
        try:
            alive = [i["instance_id"] for i in self.client.status().get("instances") or [] if i.get("reachable")]
        except Exception:  # noqa: BLE001
            return False
        if instance not in alive:
            return True
        return self._settled(instance, operation)

    def instance(self):
        status = self.client.status()
        instances = [i for i in status.get("instances") or [] if i.get("reachable")]
        if len(instances) != 1:
            raise Refused(f"expected exactly one running Krita, found {len(instances)}")
        return instances[0]["instance_id"]

    def cancel(self, job):
        if self.running != job:
            return {"ok": False, "cancel": job, "running": self.running}
        self.cancelled.add(job)
        current = self.current
        if current:
            try:
                self.client.cancel_operation(*current)
            except Exception:  # noqa: BLE001 - best effort; the batch still stops after this op
                pass
        return {"ok": True, "cancel": job, "note": "the operation in progress finishes or is dropped by Krita; nothing after it runs"}

    def _settled(self, instance, operation):
        """Wait up to SETTLE for Krita to end an operation. True when it has really ended."""
        deadline = self.clock() + SETTLE
        while True:
            try:
                state = self.client.get_operation(instance, operation).get("state")
            except Exception:  # noqa: BLE001
                state = None
            if state in TERMINAL:
                return True
            if self.clock() >= deadline:
                return False
            self.sleep(0.1)

    def run(self, request):
        job, ops = check_batch(request)
        if not self.lock.acquire(blocking=False):
            return {"ok": False, "busy": True, "running": self.running}
        try:
            if self.stuck:
                if self._stuck_cleared():
                    self._set_stuck(None)
                elif self.stuck == self.CORRUPT:
                    return {"ok": False, "busy": True,
                            "running": "the gate's marker file is unreadable; a person must check Krita and clear it (KRITA.md)"}
                else:
                    return {"ok": False, "busy": True, "running": "an earlier operation Krita has not finished"}
            self.running = job
            self.cancelled.discard(job)
            return self._run(job, ops)
        finally:
            self.running = None
            self.current = None
            self.cancelled.discard(job)
            self.lock.release()

    def _submit(self, instance, job, command, target, params, left):
        """Send one operation and wait for its end. Returns (snapshot, None) when Krita reported that
        it ended, (exception, "unknown") when the request failed, (snapshot, "unfinished") when it did
        not end in time.

        Fail closed: the in-flight marker (disk) and self.stuck (memory) are both set before
        submitting, and both are cleared only after Krita confirms the end. Any exception on the way,
        including one from cancel_operation or from writing the marker, leaves the gate busy."""
        operation = f"{job}-{uuid.uuid4().hex[:16]}"
        self.current = (instance, operation)
        self._write_marker((instance, operation), "in-flight")   # before submitting: survives a crash
        try:
            snapshot = self.client.execute(command, instance_id=instance, operation_id=operation,
                                           target=target, params=params,
                                           wait_timeout=max(0.05, min(OP_WAIT, left)))
        except Exception as error:  # noqa: BLE001 - e.g. OUTCOME_UNKNOWN: it may have been admitted
            self._set_stuck((instance, operation))
            return error, "unknown"
        if not isinstance(snapshot, dict) or snapshot.get("state") not in TERMINAL:
            # Not finished in time: ask Krita to cancel, and do not move on (or release the lock)
            # until Krita says the operation has ended. A failed cancel request changes nothing:
            # only a confirmed end clears the busy state.
            try:
                self.client.cancel_operation(instance, operation)
            except Exception:  # noqa: BLE001
                pass
            if self._settled(instance, operation):
                self._set_stuck(None)
            else:
                self._set_stuck((instance, operation))
            return (snapshot if isinstance(snapshot, dict) else {}), "unfinished"
        self._set_stuck(None)                                    # Krita reported it ended
        self.current = None
        return snapshot, None

    def _output_reserve(self, instance, job, op, left):
        """Ask Krita for the document's size and layers and return the worst-case output size."""
        document_id = op["target"].get("document_id")
        if not document_id:
            raise Refused(f"{op['command']} needs target.document_id")
        snapshot, problem = self._submit(instance, job, "inspect_document", {"document_id": document_id}, {}, left)
        if problem or snapshot.get("state") != "succeeded":
            raise Refused(f"could not measure the document before {op['command']} "
                          f"({problem or snapshot.get('state')}); nothing was written")
        return output_reserve(op["command"], snapshot.get("result"))

    def _run(self, job, ops):
        started = self.clock()
        instance = self.instance()
        if any(op["command"] in ("save_document", "export_png") for op in ops):
            os.close(self.folders.job_dir(job))
        results, stop = [], None
        for index, op in enumerate(ops):
            left = BATCH_DEADLINE - (self.clock() - started)
            if job in self.cancelled:
                stop = "cancelled"
                break
            if left <= 0:
                stop = f"batch deadline {BATCH_DEADLINE:.0f}s reached"
                break
            params = op["params"]
            try:
                # Quota is checked before EVERY file-producing operation, against what is on disk now.
                if op["command"] == "import_image_layer":
                    params = {**params, "path": self.folders.stage_import(job, params["path"].split("/", 1)[1])}
                elif op["command"] in PREVIEWS:
                    self.folders.check_room(job, adding=PREVIEW_RESERVE)
                elif op["command"] in ("save_document", "export_png"):
                    self.folders.check_room(job, adding=0)          # no room at all: do not even ask Krita
                    reserve = self._output_reserve(instance, job, op, left)
                    self.folders.check_room(job, adding=reserve)
            except Refused as error:
                results.append({"index": index, "command": op["command"], "state": "refused",
                                "error": {"code": "QUOTA_OR_FILE", "message": str(error)}})
                stop = "refused before sending: " + str(error)
                break
            op_started = self.clock()
            snapshot, problem = self._submit(instance, job, op["command"], op["target"], params, left)
            if problem == "unknown":
                results.append({"index": index, "command": op["command"], "state": "unknown",
                                "error": {"code": type(snapshot).__name__, "message": str(snapshot)[:300]}})
                stop = "could not tell whether Krita received the operation; busy until it is confirmed ended"
                break
            if problem == "unfinished":
                stop = "cancelled" if job in self.cancelled else "operation did not finish in time"
                results.append({"index": index, "command": op["command"], "state": "unfinished",
                                "ms": round((self.clock() - op_started) * 1000)})
                break
            entry = {"index": index, "command": op["command"], "state": snapshot.get("state"),
                     "ms": round((self.clock() - op_started) * 1000)}
            result = snapshot.get("result") or {}
            if snapshot.get("state") != "succeeded":
                entry["error"] = snapshot.get("error") or {"code": snapshot.get("state")}
                results.append(entry)
                break
            if op["command"] in PREVIEWS and result.get("artifact_id"):
                data = self.client.get_artifact(instance, result["artifact_id"])
                entry["preview"] = self.folders.write(job, f"preview-{int(time.time())}-{index}.png", data)
            else:
                entry["result"] = {k: v for k, v in result.items() if k not in ("artifact", "png_base64")}
            results.append(entry)
            if self.clock() - started > BATCH_DEADLINE:
                stop = f"batch deadline {BATCH_DEADLINE:.0f}s reached"
                break
        if stop is None and job in self.cancelled:
            # The cancel arrived while the last operation was running, and that operation finished.
            stop = "cancel arrived too late: every operation had already been sent and the last one completed; nothing was skipped"
        ok = stop is None and len(results) == len(ops) and all(r["state"] == "succeeded" for r in results)
        reply = {"ok": ok, "job": job, "done": len(results), "of": len(ops),
                 "ms": round((self.clock() - started) * 1000), "results": results}
        if stop:
            reply["stopped"] = stop
        return reply


def read_request(conn, deadline=None):
    """Read one newline-terminated request with an absolute deadline and a size cap."""
    deadline = deadline if deadline is not None else time.monotonic() + READ_DEADLINE
    data = b""
    while not data.endswith(b"\n"):
        left = deadline - time.monotonic()
        if left <= 0:
            raise Refused(f"request not received within {READ_DEADLINE:.0f}s")
        conn.settimeout(left)
        try:
            chunk = conn.recv(65536)
        except socket.timeout:
            raise Refused(f"request not received within {READ_DEADLINE:.0f}s") from None
        if not chunk:
            break
        data += chunk
        if len(data) > MAX_REQUEST_BYTES:
            raise Refused(f"request larger than {MAX_REQUEST_BYTES} bytes")
    return data


def _reply(conn, reply):
    try:
        conn.settimeout(5)
        conn.sendall((json.dumps(reply, ensure_ascii=False) + "\n").encode())
    except OSError:
        pass


def handle(gate, conn, slots):
    try:
        with conn:
            try:
                request = parse_request(read_request(conn))
                if isinstance(request, dict) and set(request) == {"cancel"}:
                    reply = gate.cancel(str(request["cancel"])[:64])
                else:
                    reply = gate.run(request)
            except Refused as error:
                reply = {"ok": False, "refused": str(error)}
            except (ValueError, OverflowError, RecursionError) as error:
                reply = {"ok": False, "refused": f"bad request: {type(error).__name__}"}
            except Exception as error:  # noqa: BLE001 - report, keep serving
                reply = {"ok": False, "error": f"{type(error).__name__}: {error}"}
            _reply(conn, reply)
    finally:
        slots.release()


def serve(gate, path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    os.chmod(path, 0o660)
    server.listen(MAX_CONNECTIONS)
    slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
    print(f"krita-gate listening on {path}", flush=True)
    while True:
        conn, _ = server.accept()
        if not slots.acquire(blocking=False):
            with conn:
                _reply(conn, {"ok": False, "busy": True, "refused": "too many connections"})
            continue
        threading.Thread(target=handle, args=(gate, conn, slots), daemon=True).start()


def main():
    sys.path[:0] = [os.environ["KRITA6_MCP_SRC"], os.environ["KRITA6_MCP_PLUGIN"]]
    from krita6_mcp.bridge_client import BridgeClient  # noqa: PLC0415

    client = BridgeClient(state_dir=os.environ.get("KRITA6_MCP_STATE_DIR"), timeout=10.0)
    folders = Folders(os.environ["KRITA_GATE_OUT"], os.environ["KRITA_GATE_IN"], os.environ["KRITA_GATE_STAGING"])
    print(f"krita-gate housekeeping: {folders.housekeeping()}", flush=True)
    gate = Gate(client, folders, stuck_file=os.environ.get("KRITA_GATE_STUCK", "/var/lib/krita/gate/stuck.json"))
    if gate.stuck:
        print(f"krita-gate: an operation was left unfinished before this start: {gate.stuck}; busy until cleared",
              flush=True)
    serve(gate, os.environ.get("KRITA_GATE_SOCKET", "/run/krita-gate/gate.sock"))


if __name__ == "__main__":
    try:
        main()
    except OSError as error:
        if error.errno == errno.EADDRINUSE:
            sys.exit("socket in use")
        raise
