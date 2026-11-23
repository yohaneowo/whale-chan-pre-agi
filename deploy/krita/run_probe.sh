#!/bin/bash
# Run the krita6-mcp upstream checks on VM 102 inside the SAME sandbox as krita.service.
# The systemd-run properties are generated from /etc/systemd/system/krita.service by
# unit_props.py (everything except ExecStart / restart policy / working directory), so the probe
# sandbox and the service sandbox cannot drift apart. Run as the maintenance account:
#   sudo bash /opt/krita-gate/run_probe.sh props               # print the generated properties
#   sudo bash /opt/krita-gate/run_probe.sh verify              # isolation check, read-only
#   sudo bash /opt/krita-gate/run_probe.sh setup               # build the probe venv OFFLINE (IP filter on)
#   sudo bash /opt/krita-gate/run_probe.sh probe_plugin_import | probe_krita | smoke_krita
# krita_guard.sh stops these units ('krita-probe-*.service') if the bot is affected.
set -euo pipefail

UNIT=/etc/systemd/system/krita.service
UV=/opt/krita-tools/uv-0.13.6/uv
VENV=/var/lib/krita/probe-venv
PROJECT=/opt/krita6-mcp/current
PYTHON=/usr/bin/python3

# args_from_unit [--no-ip-filter]: fills the array ARGS with "-p" "KEY=VALUE" pairs.
args_from_unit() {
	# The properties are read from the unit FILE, so a drop-in (which would change the running service
	# but not this file) would make the probe sandbox differ from the service. Refuse if there is one.
	local dropins
	dropins=$(systemctl show krita.service -p DropInPaths --value)
	if [ -n "$dropins" ] || [ -e /etc/systemd/system/krita.service.d ] || [ -e /run/systemd/system/krita.service.d ]; then
		echo "refusing: krita.service has drop-ins ($dropins); the probe sandbox would not match the service" >&2
		exit 1
	fi
	[ "$(systemctl show krita.service -p FragmentPath --value)" = "$UNIT" ] || { echo "refusing: krita.service is not loaded from $UNIT" >&2; exit 1; }
	ARGS=()
	local line
	while IFS= read -r line; do
		ARGS+=(-p "$line")
	done < <($PYTHON -I /opt/krita-gate/unit_props.py "$UNIT" "$@")
	[ "${#ARGS[@]}" -gt 20 ] || { echo "too few properties read from $UNIT" >&2; exit 1; }
}

run_unit() {
	# $1 = unit name suffix; the rest is the command. Uses the current ARGS.
	local name=$1
	shift
	systemd-run --wait --pipe --collect --quiet --unit="krita-probe-$name" \
		"${ARGS[@]}" -p WorkingDirectory=$PROJECT "$@"
}

case ${1:-} in
props)
	args_from_unit
	printf '%s\n' "${ARGS[@]}" | grep -v '^-p$'
	;;
verify)
	# 1) Control: the identical sandbox minus the IP filter, same user, literal IPs (no DNS): every
	#    target must connect. 2) The full sandbox, seconds later: dsh paths hidden, every outbound
	#    connect blocked (EPERM or dropped -> timeout), loopback works. Anything else fails.
	args_from_unit --no-ip-filter
	run_unit netcontrol $PYTHON -I /opt/krita-gate/netcheck.py control || { echo "control failed: verify is not valid" >&2; exit 1; }
	args_from_unit
	run_unit verify $PYTHON -I /opt/krita-gate/netcheck.py sandbox "$UNIT"
	;;
setup)
	# Offline, in the FULL sandbox (IP filter on, same as krita.service): nothing is downloaded and
	# the krita account never gets outbound access. The wheels come from the root-owned wheelhouse
	# that install_krita.sh / update_krita_files.sh put in /opt/krita-gate; uv checks every wheel
	# against the sha256 pinned in probe-requirements.txt (exported from krita6-mcp's uv.lock at the
	# pinned commit), refuses anything not in that list, and never builds from source.
	# krita6-mcp itself is not installed: a .pth file puts its src/ and plugin/ (root-owned, read-only)
	# on the venv's path. Safe to run again: it refuses if the venv exists (remove it to rebuild).
	[ ! -e $VENV ] && [ ! -L $VENV ] || { echo "refusing: $VENV already exists; remove it to rebuild" >&2; exit 1; }
	args_from_unit
	OFFLINE=(-p Environment=UV_CACHE_DIR=/var/lib/krita/.cache/uv -p Environment=UV_PYTHON_DOWNLOADS=never
		-p Environment=UV_NO_CONFIG=1 -p Environment=UV_OFFLINE=1)
	run_unit setup-venv "${OFFLINE[@]}" $UV venv --offline --no-config --python $PYTHON $VENV
	run_unit setup-install "${OFFLINE[@]}" $UV pip install --offline --no-config --no-index --no-build \
		--require-hashes --find-links /opt/krita-gate/wheelhouse --python $VENV/bin/python \
		-r /opt/krita-gate/probe-requirements.txt
	run_unit setup-pth $VENV/bin/python -I -c 'import sysconfig
with open(sysconfig.get_paths()["purelib"] + "/krita6_mcp_source.pth", "x", encoding="utf-8") as f:
    f.write("/opt/krita6-mcp/current/src\n/opt/krita6-mcp/current/plugin\n")'
	run_unit setup-check $VENV/bin/python -I -c 'import mcp, krita6_mcp.cli, krita6_bridge.discovery; print("probe venv ok:", krita6_mcp.cli.__file__)'
	;;
probe_plugin_import)
	# The upstream tool builds its ZIP into <project>/dist, which is read-only here; build it into a
	# fresh report folder instead (same tool, same importer, only the ZIP location differs).
	install -d -o krita -g krita -m 0750 /var/lib/krita/probe-reports
	args_from_unit
	OUT=/var/lib/krita/probe-reports/plugin_import-$(date +%Y%m%d-%H%M%S)
	run_unit probe_plugin_import $VENV/bin/python -c 'import sys
sys.path.insert(0, "tools")
from pathlib import Path
import build_plugin, probe_plugin_import
out, importer = Path(sys.argv[1]), sys.argv[2]
out.mkdir(mode=0o750)
real = build_plugin.build
probe_plugin_import.build = lambda: real(out / "krita6-bridge.zip")
sys.argv = ["probe_plugin_import.py", "--importer", importer]
probe_plugin_import.main()' "$OUT" /opt/krita/current/usr/share/krita/pykrita/plugin_importer/plugin_importer.py
	;;
probe_krita | smoke_krita)
	install -d -o krita -g krita -m 0750 /var/lib/krita/probe-reports
	args_from_unit
	run_unit "$1" $VENV/bin/python tools/"$1".py --krita /opt/krita/current/AppRun \
		--output /var/lib/krita/probe-reports/"$1-$(date +%Y%m%d-%H%M%S)"
	;;
*)
	echo "usage: run_probe.sh props | verify | setup | probe_plugin_import | probe_krita | smoke_krita" >&2
	exit 2
	;;
esac
