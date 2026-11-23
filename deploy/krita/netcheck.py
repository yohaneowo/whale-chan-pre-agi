#!/usr/bin/env python3
"""Isolation check for the Krita sandbox (read-only; prints one line per check).

  netcheck.py control   the same sandbox WITHOUT the IP filter: every target must connect at once,
                        proving the VM can reach them right now
  netcheck.py sandbox [unit file]
                        the full sandbox, right after a passing control: every InaccessiblePaths=
                        entry of the unit file (default /etc/systemd/system/krita.service) must be
                        hidden, and every target must be blocked

Only two outcomes count as "blocked by the filter":
  BLOCKED_BY_FILTER  connect() failed with EPERM/EACCES (what a cgroup BPF verdict can return)
  DROPPED            connect() timed out (systemd's IPAddressDeny drops packets silently)
Everything else is a FAIL: DNS errors, network/host unreachable, refused, reset, any other error.
All targets are literal IP addresses (checked at start), so no DNS lookup is ever made.

The evidence is the pair: same user, same sandbox, same IPs, seconds apart; only the IP filter
differs. run_probe.sh runs sandbox only if control passed. Exit 0 only if every check passed.
"""

import errno
import ipaddress
import os
import socket
import sys

TARGETS = [("1.1.1.1", 443), ("9.9.9.9", 443), ("8.8.8.8", 53)]
UNIT = "/etc/systemd/system/krita.service"
BLOCKED = ("BLOCKED_BY_FILTER", "DROPPED")


def hidden_paths(unit_path):
    """Every InaccessiblePaths= entry of the unit's [Service] section, without the optional '-' / '+'
    prefixes. Read from the unit file itself, so the check covers exactly what the service hides.
    Fails (ValueError) on anything this simple reader cannot be sure about."""
    paths, section = [], None
    with open(unit_path, encoding="utf-8") as file:
        for raw in file:
            line = raw.strip()
            if not line or line.startswith(("#", ";")):
                continue
            if line.startswith("["):
                section = line
                continue
            if line.endswith("\\"):
                raise ValueError(f"{unit_path}: continued lines are not supported")
            key, _, value = line.partition("=")
            if section != "[Service]" or key.strip() != "InaccessiblePaths":
                continue
            if any(c in value for c in "\"'%$"):
                raise ValueError(f"{unit_path}: quotes or specifiers in InaccessiblePaths are not supported")
            if not value.strip():
                raise ValueError(f"{unit_path}: an empty InaccessiblePaths= would reset the list")
            for item in value.split():
                path = item.lstrip("-+")
                if not path.startswith("/"):
                    raise ValueError(f"{unit_path}: {item!r} is not an absolute path")
                paths.append(path)
    if not paths:
        raise ValueError(f"{unit_path}: no InaccessiblePaths found")
    return paths


def probe_path(path):
    """LEAK if the sandboxed user can list (folder) or open (file) the path, else DENIED."""
    try:
        if os.path.isdir(path):
            os.listdir(path)
        else:
            with open(path, "rb"):
                pass
        return f"LEAK can read {path}"
    except FileNotFoundError:
        return f"DENIED {path} (not present or hidden)"
    except PermissionError:
        return f"DENIED {path}"
    except OSError as error:
        return f"FAIL {path} {type(error).__name__}"

for _host, _port in TARGETS:
    ipaddress.ip_address(_host)  # raises if a target is ever changed to a name


def classify(error):
    """Map a connect() exception to an outcome. Only EPERM/EACCES and a plain timeout are blocks."""
    if isinstance(error, socket.gaierror):
        return f"FAIL_DNS({error.errno})"
    if isinstance(error, (socket.timeout, TimeoutError)):
        return "DROPPED"
    if isinstance(error, OSError) and error.errno in (errno.EPERM, errno.EACCES):
        return "BLOCKED_BY_FILTER"
    if isinstance(error, OSError) and error.errno == errno.ECONNREFUSED:
        return "REFUSED_BY_REMOTE"
    name = errno.errorcode.get(getattr(error, "errno", None) or -1, type(error).__name__)
    return f"FAIL_{name}"


def connect(host, port, timeout=5, make_socket=socket.socket):
    sock = make_socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return "OPEN"
    except Exception as error:  # noqa: BLE001 - every failure is classified, unknown ones FAIL
        return classify(error)
    finally:
        sock.close()


def main(mode, unit_path=UNIT):
    ok = True
    if mode == "control":
        results = [connect(h, p) for h, p in TARGETS]
        for (host, port), result in zip(TARGETS, results):
            print(f"control {host}:{port} {result}")
        if any(r != "OPEN" for r in results):
            print("FAIL control: not every target is reachable without the filter, so a sandbox result would prove nothing")
            ok = False
        return ok
    try:
        paths = hidden_paths(unit_path)
    except (OSError, ValueError) as error:
        print(f"FAIL cannot read the hidden paths from the unit: {error}")
        return False
    for path in paths:
        result = probe_path(path)
        print(result)
        ok = ok and result.startswith("DENIED")
    for host, port in TARGETS:
        result = connect(host, port, timeout=4)
        blocked = result in BLOCKED
        print(f"{'DENIED' if blocked else 'FAIL'} internet {host}:{port} {result}")
        ok = ok and blocked
    loop = connect("127.0.0.1", 9, timeout=3)
    print(f"{'LOOPBACK OK' if loop in ('OPEN', 'REFUSED_BY_REMOTE') else 'FAIL loopback'} {loop}")
    ok = ok and loop in ("OPEN", "REFUSED_BY_REMOTE")
    return ok


if __name__ == "__main__":
    sys.exit(0 if main(sys.argv[1] if len(sys.argv) > 1 else "sandbox",
                       sys.argv[2] if len(sys.argv) > 2 else UNIT) else 1)
