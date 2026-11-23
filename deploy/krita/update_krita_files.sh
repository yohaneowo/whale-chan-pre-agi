#!/bin/sh
# Update the gate/tool files, the probe wheelhouse and the two unit files of an existing Krita install
# (phase 0 done), without touching Krita, krita6-mcp, accounts, or the shared folders. Run with sudo:
#   sudo sh update_krita_files.sh <folder with the deploy/krita files, wheelhouse/ and wheelhouse.sha256>
#
# 1. Preflight on the live system (fail closed): every target's owner / group / exact mode / type,
#    no links, no unexpected files, no drop-ins, nothing running.
# 2. Freeze: copy the sources (without following links) into a root-only folder; everything after is
#    checked and installed from that frozen copy, so a source cannot be swapped after it was checked.
#    Checks: complete, parseable, every wheel listed in wheelhouse.sha256 AND pinned by sha256 in
#    probe-requirements.txt.
# 3. Back up, build the new folder next to the old one, swap by rename, replace each unit by rename,
#    verify. The rollback state is recorded BEFORE each step, so an interruption at any point
#    (error or signal) is undone; a failure before anything was replaced changes nothing.
# 4. The rollback runs every restore step even when one fails, and reports each failure.
set -eu

SRC=${1:?usage: update_krita_files.sh <folder with deploy/krita files>}
G=/opt/krita-gate
UNITDIR=/etc/systemd/system
PY644="krita_gate.py measure_krita.py netcheck.py unit_props.py"
SH755="krita_guard.sh run_probe.sh"
DATA644="probe-requirements.txt"
UNITS="krita.service krita-gate.service"
TS=$(date +%Y%m%d-%H%M%S)
BACKUP=/var/backups/krita-update-$TS
FROZEN=$BACKUP/source
NEW=$G.new-$TS
OLD=$G.old-$TS

fail() { echo "refusing: $*" >&2; exit 1; }

# Test hooks (used only by test_update_krita_files.sh in a throwaway container):
#   KRITA_UPDATE_TEST_FAIL=<point>        fail at that point (with KRITA_UPDATE_TEST_SIGNAL=TERM: be killed)
#   KRITA_UPDATE_TEST_ROLLBACK_FAIL=<id>  make that rollback step fail
testpoint() {
	[ "${KRITA_UPDATE_TEST_FAIL:-}" = "$1" ] || return 0
	echo "test: forced failure at $1" >&2
	if [ -n "${KRITA_UPDATE_TEST_SIGNAL:-}" ]; then kill -s "$KRITA_UPDATE_TEST_SIGNAL" $$; sleep 2; fi
	exit 1
}

# expect <path> <owner> <group> <octal mode> <type: d|f>
expect() {
	[ -e "$1" ] && [ ! -L "$1" ] || fail "$1 missing or a link"
	case $5 in d) [ -d "$1" ] || fail "$1 is not a folder" ;; f) [ -f "$1" ] || fail "$1 is not a regular file" ;; esac
	actual=$(stat -c '%U %G %a' "$1")
	[ "$actual" = "$2 $3 $4" ] || fail "$1 is '$actual', expected '$2 $3 $4'"
}

# ---------- 1. preflight on the live system (nothing changed yet) ----------
[ "$(id -u)" = 0 ] || fail "run with sudo"
for unit in krita.service krita-gate.service; do
	if systemctl is-active --quiet "$unit"; then fail "$unit is running; stop it first"; fi
done
if systemctl list-units --no-legend 'krita-probe-*.service' | grep -q .; then fail "a krita probe is running"; fi
expect /opt root root 755 d
expect $UNITDIR root root 755 d
expect $G root root 755 d
for name in $PY644 $DATA644; do [ ! -e "$G/$name" ] && [ ! -L "$G/$name" ] || expect "$G/$name" root root 644 f; done
for name in $SH755; do [ ! -e "$G/$name" ] && [ ! -L "$G/$name" ] || expect "$G/$name" root root 755 f; done
if [ -e "$G/wheelhouse" ] || [ -L "$G/wheelhouse" ]; then
	expect "$G/wheelhouse" root root 755 d
	for whl in "$G"/wheelhouse/* "$G"/wheelhouse/.[!.]*; do
		[ -e "$whl" ] || [ -L "$whl" ] || continue
		case ${whl##*/} in *.whl) expect "$whl" root root 644 f ;; *) fail "unexpected file in $G/wheelhouse: ${whl##*/}" ;; esac
	done
fi
extra=$(ls -A "$G" | grep -vxF -e krita_gate.py -e measure_krita.py -e netcheck.py -e unit_props.py \
	-e krita_guard.sh -e run_probe.sh -e probe-requirements.txt -e wheelhouse || true)
[ -z "$extra" ] || fail "unexpected files in $G: $extra"
for name in $UNITS; do
	expect "$UNITDIR/$name" root root 644 f
	[ ! -e "$UNITDIR/$name.d" ] && [ ! -e "/run/systemd/system/$name.d" ] || fail "a drop-in folder exists for $name"
done
expect /var/lib/krita krita krita 750 d
[ ! -e /var/lib/krita/gate ] || expect /var/lib/krita/gate krita krita 700 d
for path in "$NEW" "$OLD" "$BACKUP"; do [ ! -e "$path" ] && [ ! -L "$path" ] || fail "$path already exists"; done

# ---------- rollback (armed before anything is created) ----------
# Each flag is set BEFORE the step it covers, and every restore step is safe when its step never ran.
made_backup=no     # set after $BACKUP was created by this run
gate_moved=no      # set before "mv $G $OLD"
units_touched=""   # a unit name is added before its replacement starts
made_gate_dir=no   # set before /var/lib/krita/gate is created

problems=0
rb() {   # rb <step id> <command...>: run one rollback step; on failure report it and go on
	step=$1
	shift
	if [ "${KRITA_UPDATE_TEST_ROLLBACK_FAIL:-}" = "$step" ]; then
		echo "rollback: step $step failed (test)" >&2
		problems=$((problems + 1))
	elif ! "$@"; then
		echo "rollback: step $step failed: $*" >&2
		problems=$((problems + 1))
	fi
}
restore_unit() {
	cp -a "$BACKUP/$1" "$UNITDIR/.$1.restore-$TS" && mv -f "$UNITDIR/.$1.restore-$TS" "$UNITDIR/$1"
}

rollback() {
	status=$?
	[ "$status" -ne 0 ] || exit 0
	set +e
	trap '' INT TERM HUP   # finish the rollback even if signalled again
	if [ "$gate_moved" = no ] && [ -z "$units_touched" ]; then
		rm -rf -- "$NEW"
		[ "$made_backup" = no ] || rm -rf -- "$BACKUP"
		echo "update failed (exit $status) before anything was replaced; nothing was changed" >&2
		exit "$status"
	fi
	echo "update failed (exit $status); rolling back from $BACKUP" >&2
	if [ "$gate_moved" = yes ] && [ -d "$OLD" ]; then
		if [ -e "$G" ] || [ -L "$G" ]; then rb remove-new-gate rm -rf -- "$G"; fi
		if [ -e "$G" ] || [ -L "$G" ]; then
			echo "rollback: $G is still there; the previous version is left in $OLD" >&2
			problems=$((problems + 1))
		else
			rb restore-gate mv -T "$OLD" "$G"
		fi
	elif [ "$gate_moved" = yes ] && [ ! -d "$G" ]; then
		rb restore-gate-from-backup cp -a "$BACKUP/krita-gate" "$G"
	fi
	if [ -e "$NEW" ] || [ -L "$NEW" ]; then rb remove-new-folder rm -rf -- "$NEW"; fi
	for name in $units_touched; do
		rb "unit-$name" restore_unit "$name"
		rm -f -- "$UNITDIR/.$name.new-$TS" "$UNITDIR/.$name.restore-$TS"
	done
	if [ "$made_gate_dir" = yes ] && [ -d /var/lib/krita/gate ]; then rb gate-dir rmdir /var/lib/krita/gate; fi
	rb daemon-reload systemctl daemon-reload
	if [ "$problems" -eq 0 ]; then
		echo "rolled back; backup kept in $BACKUP" >&2
	else
		echo "ROLLBACK INCOMPLETE: $problems step(s) failed (see above); restore by hand from $BACKUP" >&2
	fi
	exit "$status"
}
trap rollback EXIT
trap 'exit 130' INT TERM HUP

# ---------- 2. freeze the sources (root-only, links are not followed), then check the frozen copy ----------
mkdir -m 0755 -p /var/backups
mkdir -m 0700 "$BACKUP"
made_backup=yes
mkdir -m 0700 "$FROZEN" "$FROZEN/wheelhouse"
for name in $PY644 $SH755 $DATA644 $UNITS wheelhouse.sha256; do
	[ -f "$SRC/$name" ] && [ ! -L "$SRC/$name" ] || fail "source $SRC/$name missing, a link or not a file"
	cp -P "$SRC/$name" "$FROZEN/$name"   # checked again on the copy: the source may change in between
	[ -f "$FROZEN/$name" ] && [ ! -L "$FROZEN/$name" ] && [ -s "$FROZEN/$name" ] || fail "source $SRC/$name is empty, a link or not a file"
done
[ -d "$SRC/wheelhouse" ] && [ ! -L "$SRC/wheelhouse" ] || fail "source $SRC/wheelhouse missing or a link"
for whl in "$SRC"/wheelhouse/*.whl; do
	[ -f "$whl" ] && [ ! -L "$whl" ] || fail "source wheel $whl missing, a link or not a file"
	cp -P "$whl" "$FROZEN/wheelhouse/"
	[ -f "$FROZEN/wheelhouse/${whl##*/}" ] && [ ! -L "$FROZEN/wheelhouse/${whl##*/}" ] || fail "source wheel $whl is a link or not a file"
done
chmod -R go-rwx "$FROZEN"
testpoint after-freeze
F=$FROZEN
for name in $PY644; do python3 -I -c 'import ast,sys; ast.parse(open(sys.argv[1], encoding="utf-8").read())' "$F/$name" || fail "source $name does not parse"; done
sh -n "$F/krita_guard.sh" || fail "source krita_guard.sh does not parse"
bash -n "$F/run_probe.sh" || fail "source run_probe.sh does not parse"
for name in $UNITS; do grep -q '^\[Service\]$' "$F/$name" || fail "source $name has no [Service] section"; done
( cd "$F/wheelhouse" && sha256sum -c --quiet ../wheelhouse.sha256 ) || fail "a wheel does not match wheelhouse.sha256"
[ "$(ls -A "$F/wheelhouse" | wc -l)" = "$(grep -c '\.whl$' "$F/wheelhouse.sha256")" ] || fail "wheelhouse and wheelhouse.sha256 list different files"
while read -r sum file; do
	grep -qF -- "--hash=sha256:$sum" "$F/probe-requirements.txt" || fail "wheel $file is not pinned in probe-requirements.txt"
done < "$F/wheelhouse.sha256"

# ---------- 3. backup, build, swap, verify ----------
cp -a "$G" "$BACKUP/krita-gate"
for name in $UNITS; do cp -a "$UNITDIR/$name" "$BACKUP/$name"; done
testpoint after-backup

mkdir -m 0755 "$NEW" "$NEW/wheelhouse"
for name in $PY644 $DATA644; do install -o root -g root -m 0644 "$F/$name" "$NEW/$name"; done
for name in $SH755; do install -o root -g root -m 0755 "$F/$name" "$NEW/$name"; done
for whl in "$F"/wheelhouse/*.whl; do install -o root -g root -m 0644 "$whl" "$NEW/wheelhouse/"; done
chown root:root "$NEW" "$NEW/wheelhouse"
testpoint after-build

gate_moved=yes
mv -T "$G" "$OLD"
testpoint mid-gate-swap
mv -T "$NEW" "$G"
testpoint after-gate-swap
for name in $UNITS; do
	units_touched="$units_touched $name"
	install -o root -g root -m 0644 "$F/$name" "$UNITDIR/.$name.new-$TS"
	testpoint "mid-unit-$name"
	mv -f "$UNITDIR/.$name.new-$TS" "$UNITDIR/$name"
	testpoint "after-unit-$name"
done
if [ ! -e /var/lib/krita/gate ]; then
	made_gate_dir=yes
	install -d -o krita -g krita -m 0700 /var/lib/krita/gate
fi
testpoint after-gate-dir
systemctl daemon-reload
testpoint after-reload

for name in $PY644 $DATA644; do expect "$G/$name" root root 644 f; cmp -s "$F/$name" "$G/$name" || fail "$G/$name differs from source"; done
for name in $SH755; do expect "$G/$name" root root 755 f; cmp -s "$F/$name" "$G/$name" || fail "$G/$name differs from source"; done
expect "$G/wheelhouse" root root 755 d
( cd "$G/wheelhouse" && sha256sum -c --quiet "$F/wheelhouse.sha256" ) || fail "installed wheelhouse does not match"
[ "$(ls -A "$G/wheelhouse" | wc -l)" = "$(grep -c '\.whl$' "$F/wheelhouse.sha256")" ] || fail "installed wheelhouse has extra files"
for name in $UNITS; do expect "$UNITDIR/$name" root root 644 f; cmp -s "$F/$name" "$UNITDIR/$name" || fail "$UNITDIR/$name differs from source"; done
expect /var/lib/krita/gate krita krita 700 d
testpoint after-verify

# Committed: from here on nothing is rolled back; the old folder is only garbage.
trap - EXIT
trap - INT TERM HUP
rm -rf -- "$OLD"
echo "updated $G and the unit files; nothing started; previous version and frozen sources in $BACKUP"
