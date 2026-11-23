#!/bin/bash
# Safety tests for update_krita_files.sh. Run ONLY in a throwaway systemd container, e.g.:
#   docker run -d --name t --privileged --cgroupns=host -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
#     -v <deploy/krita>:/stage:ro -v <folder with wheelhouse/ and wheelhouse.sha256>:/wheels:ro \
#     jrei/systemd-ubuntu:24.04
#   docker exec t bash /stage/test_update_krita_files.sh
set -u
apt-get update -qq >/dev/null && apt-get install -y -qq python3 >/dev/null 2>&1
getent passwd krita >/dev/null || useradd --system --user-group --home-dir /var/lib/krita --create-home krita
chmod 750 /var/lib/krita
# The source folder, as the maintenance account would stage it: deploy/krita + wheelhouse.
S=/root/src
rm -rf "$S"
cp -r /stage "$S"
cp -r /wheels/wheelhouse /wheels/wheelhouse.sha256 "$S/"
U=/root/update_krita_files.sh
cp "$S/update_krita_files.sh" "$U"

setup() {   # a phase-0 install: old files, no unit_props.py / wheelhouse, no /var/lib/krita/gate
	rm -rf /opt/krita-gate /opt/krita-gate.* /etc/systemd/system/krita.service* /etc/systemd/system/krita-gate.service* \
		/etc/systemd/system/.krita* /var/lib/krita/gate /var/backups/krita-update-*
	mkdir -m 755 /opt/krita-gate
	for f in krita_gate.py measure_krita.py netcheck.py; do echo "# old $f" > /opt/krita-gate/$f; chmod 644 /opt/krita-gate/$f; done
	for f in krita_guard.sh run_probe.sh; do echo "# old $f" > /opt/krita-gate/$f; chmod 755 /opt/krita-gate/$f; done
	for f in krita.service krita-gate.service; do printf '[Service]\nExecStart=/bin/true\n# old\n' > /etc/systemd/system/$f; chmod 644 /etc/systemd/system/$f; done
	systemctl daemon-reload
}

snap() {   # everything the update may touch, including leftovers
	{
		find /opt/krita-gate -exec stat -c '%U %G %a %F %n' {} + | sort
		find /opt/krita-gate -type f -exec sha256sum {} + | sort
		ls -A /etc/systemd/system | grep -i krita
		sha256sum /etc/systemd/system/krita.service /etc/systemd/system/krita-gate.service
		stat -c '%U %G %a' /etc/systemd/system/krita.service /etc/systemd/system/krita-gate.service
		ls -d /var/lib/krita/gate /opt/krita-gate.* 2>/dev/null
	} | sha256sum | cut -c1-16
}

pass=0
failed=0
check() {
	if [ "$1" = ok ]; then echo "PASS $2"; pass=$((pass + 1)); else echo "FAIL $2"; failed=$((failed + 1)); fi
}

refused_unchanged() {   # $1 = description, $2 = text the output must contain; runs the remaining args
	local desc=$1 why=$2; shift 2
	local before; before=$(snap)
	"$@" >/tmp/out 2>&1
	local code=$?
	if [ $code -ne 0 ] && [ "$(snap)" = "$before" ] && grep -qF -- "$why" /tmp/out; then
		check ok "$desc: $(grep -F -- "$why" /tmp/out | tail -1)"
	else
		check no "$desc (exit $code, expected '$why', unchanged: $([ "$(snap)" = "$before" ] && echo yes || echo NO))"; cat /tmp/out
	fi
}

echo "== preflight and source checks: refused, nothing changed"
setup; chmod 777 /opt/krita-gate;                      refused_unchanged "group/other-writable /opt/krita-gate" "/opt/krita-gate is 'root root 777'" sh "$U" "$S"
setup; chmod 775 /opt/krita-gate;                      refused_unchanged "group-writable /opt/krita-gate" "/opt/krita-gate is 'root root 775'" sh "$U" "$S"
setup; chmod 664 /opt/krita-gate/krita_gate.py;        refused_unchanged "group-writable file" "krita_gate.py is 'root root 664'" sh "$U" "$S"
setup; chown nobody /opt/krita-gate/run_probe.sh;      refused_unchanged "file not owned by root" "run_probe.sh is 'nobody root 755'" sh "$U" "$S"
setup; touch /opt/krita-gate/evil;                     refused_unchanged "unexpected file in /opt/krita-gate" "unexpected files in /opt/krita-gate: evil" sh "$U" "$S"
setup; ln -s /tmp /opt/krita-gate/unit_props.py;       refused_unchanged "link where a file goes" "unit_props.py missing or a link" sh "$U" "$S"
setup; mkdir -m 755 /opt/krita-gate/wheelhouse; touch /opt/krita-gate/wheelhouse/x.sh
refused_unchanged "non-wheel file in the installed wheelhouse" "unexpected file in /opt/krita-gate/wheelhouse: x.sh" sh "$U" "$S"
setup; chmod 664 /etc/systemd/system/krita.service;    refused_unchanged "group-writable unit file" "krita.service is 'root root 664'" sh "$U" "$S"
setup; mkdir /etc/systemd/system/krita.service.d;      refused_unchanged "systemd drop-in present" "a drop-in folder exists for krita.service" sh "$U" "$S"
setup; mkdir -m 777 /var/lib/krita/gate;               refused_unchanged "wrong mode on /var/lib/krita/gate" "/var/lib/krita/gate is 'root root 777'" sh "$U" "$S"
fresh_src() { rm -rf /tmp/src; cp -r "$S" /tmp/src; }
setup; fresh_src; rm /tmp/src/netcheck.py
refused_unchanged "missing source file" "source /tmp/src/netcheck.py missing" sh "$U" /tmp/src
setup; fresh_src; ln -sf /etc/shadow /tmp/src/unit_props.py
refused_unchanged "source file is a link" "source /tmp/src/unit_props.py missing, a link" sh "$U" /tmp/src
setup; fresh_src; echo 'def (' >> /tmp/src/krita_gate.py
refused_unchanged "source that does not parse" "source krita_gate.py does not parse" sh "$U" /tmp/src
setup; fresh_src; w=$(ls /tmp/src/wheelhouse | head -1); printf 'x' >> "/tmp/src/wheelhouse/$w"
refused_unchanged "tampered wheel" "a wheel does not match wheelhouse.sha256" sh "$U" /tmp/src
leftover_backups=$(ls -d /var/backups/krita-update-* 2>/dev/null | wc -l)
[ "$leftover_backups" = 0 ] && check ok "a refused source leaves no backup / frozen copy behind" || check no "refusal left $leftover_backups backup folder(s)"
setup; fresh_src; cp /tmp/src/wheelhouse/$w /tmp/src/wheelhouse/extra-0-py3-none-any.whl
refused_unchanged "extra wheel" "wheelhouse and wheelhouse.sha256 list different files" sh "$U" /tmp/src
setup; fresh_src; sum=$(head -1 /tmp/src/wheelhouse.sha256 | cut -d' ' -f1); sed -i "s/$sum/$(printf '0%.0s' $(seq 64))/" /tmp/src/probe-requirements.txt
refused_unchanged "wheel not pinned in probe-requirements.txt" "is not pinned in probe-requirements.txt" sh "$U" /tmp/src
setup; systemd-run --unit=krita-probe-busy --quiet /bin/sleep 30; sleep 1
refused_unchanged "probe running" "a krita probe is running" sh "$U" "$S"
systemctl stop krita-probe-busy

echo "== a failure or a kill between every two steps: everything is back as before"
POINTS="after-freeze after-backup after-build mid-gate-swap after-gate-swap mid-unit-krita.service after-unit-krita.service
mid-unit-krita-gate.service after-unit-krita-gate.service after-gate-dir after-reload after-verify"
for signal in "" TERM; do
	for point in $POINTS; do
		case $point in after-freeze | after-backup | after-build) why="nothing was changed" ;; *) why="rolled back; backup kept" ;; esac
		setup
		refused_unchanged "${signal:-error} at $point" "$why" env KRITA_UPDATE_TEST_FAIL=$point KRITA_UPDATE_TEST_SIGNAL=$signal sh "$U" "$S"
	done
done

echo "== a rollback step that fails does not stop the others"
setup
before_units=$(sha256sum /etc/systemd/system/krita.service /etc/systemd/system/krita-gate.service)
env KRITA_UPDATE_TEST_FAIL=after-reload KRITA_UPDATE_TEST_ROLLBACK_FAIL=remove-new-gate sh "$U" "$S" >/tmp/out 2>&1
code=$?
old=$(ls -d /opt/krita-gate.old-* 2>/dev/null | head -1)
if [ $code -ne 0 ] && grep -q "rollback: step remove-new-gate failed" /tmp/out && grep -q "ROLLBACK INCOMPLETE: 2 step(s)" /tmp/out \
	&& [ "$(sha256sum /etc/systemd/system/krita.service /etc/systemd/system/krita-gate.service)" = "$before_units" ] \
	&& [ ! -e /var/lib/krita/gate ] && [ -n "$old" ] && grep -q "# old" "$old/krita_gate.py"; then
	check ok "gate step failed, units and gate folder still restored, old version kept in $old: $(tail -1 /tmp/out)"
else
	check no "failed rollback step (exit $code)"; cat /tmp/out
fi
setup
env KRITA_UPDATE_TEST_FAIL=after-reload KRITA_UPDATE_TEST_ROLLBACK_FAIL=unit-krita.service sh "$U" "$S" >/tmp/out 2>&1
code=$?
if [ $code -ne 0 ] && grep -q "rollback: step unit-krita.service failed" /tmp/out && grep -q "ROLLBACK INCOMPLETE: 1 step(s)" /tmp/out \
	&& grep -q "# old" /opt/krita-gate/krita_gate.py && grep -q "# old" /etc/systemd/system/krita-gate.service \
	&& [ ! -e /var/lib/krita/gate ]; then
	check ok "unit step failed, gate folder and the other unit still restored: $(tail -1 /tmp/out)"
else
	check no "failed unit rollback step (exit $code)"; cat /tmp/out
fi

echo "== normal update"
setup
sh "$U" "$S" >/tmp/out 2>&1
code=$?
if [ $code -eq 0 ] \
	&& cmp -s "$S/krita_gate.py" /opt/krita-gate/krita_gate.py \
	&& cmp -s "$S/unit_props.py" /opt/krita-gate/unit_props.py \
	&& cmp -s "$S/probe-requirements.txt" /opt/krita-gate/probe-requirements.txt \
	&& cmp -s "$S/krita.service" /etc/systemd/system/krita.service \
	&& ( cd /opt/krita-gate/wheelhouse && sha256sum -c --quiet "$S/wheelhouse.sha256" ) \
	&& [ "$(stat -c '%U %G %a' /opt/krita-gate/unit_props.py)" = "root root 644" ] \
	&& [ "$(stat -c '%U %G %a' /opt/krita-gate/run_probe.sh)" = "root root 755" ] \
	&& [ "$(stat -c '%U %G %a' /opt/krita-gate/wheelhouse)" = "root root 755" ] \
	&& [ -z "$(find /opt/krita-gate/wheelhouse -type f ! -perm 644)" ] \
	&& [ "$(stat -c '%U %G %a' /var/lib/krita/gate)" = "krita krita 700" ] \
	&& ls /var/backups/krita-update-*/krita-gate/krita_gate.py >/dev/null 2>&1 \
	&& [ "$(stat -c '%U %a' /var/backups/krita-update-*/source)" = "root 700" ] \
	&& ! ls -d /opt/krita-gate.* >/dev/null 2>&1 \
	&& [ -z "$(ls -A /etc/systemd/system | grep '^\.krita')" ]; then
	check ok "normal update: files match source, wheelhouse checked, gate folder made, frozen sources root-only: $(tail -1 /tmp/out)"
else
	check no "normal update (exit $code)"; cat /tmp/out
fi
sleep 1   # a new timestamp for the second run
sh "$U" "$S" >/tmp/out 2>&1 && check ok "second update over the first (wheelhouse present) works" || { check no "second update"; cat /tmp/out; }
echo "== $pass passed, $failed failed"
[ $failed -eq 0 ]
