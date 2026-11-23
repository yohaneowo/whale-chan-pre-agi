#!/usr/bin/env python3
"""Print the [Service] settings of a unit file as systemd-run properties, one per line.

run_probe.sh uses this so the probe/verify sandbox is generated from krita.service itself and the
two cannot drift apart. Only lines that describe *how* the process runs are skipped (ExecStart,
restart policy, working directory); every sandbox, resource, identity and environment setting is
copied verbatim.

  unit_props.py <unit file>                  all settings
  unit_props.py <unit file> --no-ip-filter   without IPAddressDeny/IPAddressAllow (control / setup)
"""

import sys

# Deliberately not copied, and why (the probe runs a different command, once, in another folder):
#   ExecStart, ExecStartPre, ExecStartPost, ExecStop, ExecReload: the probe has its own command
#   Restart, RestartSec: a probe must run once and report, never be restarted
#   WorkingDirectory: the probe runs in /opt/krita6-mcp/current (run_probe.sh sets it)
#   Type, RemainAfterExit: process-lifecycle settings; systemd-run --wait needs its own defaults
# None of these change what the process may read, write, reach or use. krita.service currently
# sets only ExecStart, Restart, RestartSec and WorkingDirectory from this list.
SKIP = {"ExecStart", "ExecStartPre", "ExecStartPost", "ExecStop", "ExecReload", "Restart", "RestartSec",
        "WorkingDirectory", "Type", "RemainAfterExit"}
IP_FILTER = {"IPAddressDeny", "IPAddressAllow"}


def props(path, ip_filter=True):
    section, out = None, []
    with open(path, encoding="utf-8") as file:
        for raw in file:
            line = raw.strip()
            if not line or line.startswith(("#", ";")):
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1]
                continue
            if section != "Service" or "=" not in line:
                continue
            key = line.split("=", 1)[0].strip()
            if key in SKIP or (not ip_filter and key in IP_FILTER):
                continue
            out.append(line)
    return out


if __name__ == "__main__":
    for item in props(sys.argv[1], ip_filter="--no-ip-filter" not in sys.argv[2:]):
        print(item)
