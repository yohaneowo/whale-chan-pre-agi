#!/bin/sh
# Safety tests for install_whaledraw.sh. Run as root in a throwaway Ubuntu 24.04 container only:
#   docker run --rm -v <deploy/draw>:/stage:ro ubuntu:24.04 sh /stage/test_install_whaledraw.sh
set -u
apt-get update -qq >/dev/null && apt-get install -y -qq python3 curl ca-certificates >/dev/null 2>&1
S=/stage/install_whaledraw.sh
fail=0
sentinels() {
	mkdir -p /var/tmp/evil /var/tmp/evil.part /opt/dsh/keep /opt/evil.part /tmp/x.part
	for d in /var/tmp/evil /var/tmp/evil.part /opt/dsh/keep /opt/evil.part /tmp/x.part; do touch "$d/keep"; done
}
check_sentinels() {
	for d in /var/tmp/evil /var/tmp/evil.part /opt/dsh/keep /opt/evil.part /tmp/x.part; do
		[ -f "$d/keep" ] || { echo "  DELETED $d"; return 1; }
	done
}
sentinels
for v in "" "." ".." "../x" "/../../../var/tmp/evil" ".hidden" "a/b" "1.0/../../../var/tmp/evil" "../../evil" "0.1 0" 'x;rm' '$(id)' "0.1.0/"; do
	sh "$S" /stage "$v" >/tmp/out 2>&1; code=$?
	if [ "$code" -eq 2 ] && check_sentinels && [ -z "$(ls /opt/dsh | grep -v '^keep$')" ]; then
		echo "PASS refused '$v' (exit $code): $(head -1 /tmp/out)"
	else
		echo "FAIL '$v' exit=$code: $(head -2 /tmp/out | tr '\n' ' ')"; fail=1
	fi
done
echo "== valid install"
sh "$S" /stage 0.1.0 >/tmp/out 2>&1 && grep -q "installed /opt/dsh/whaledraw-0.1.0" /tmp/out && check_sentinels && echo "PASS valid version installed" || { echo "FAIL valid"; cat /tmp/out; fail=1; }
echo "== failure cleans only its own .part"
mkdir -p /tmp/bad && cp -r /stage/. /tmp/bad/ && sed -i '0,/^42d6/s/^42d6/0000/' /tmp/bad/wheels-vm.txt
sh /tmp/bad/install_whaledraw.sh /tmp/bad 0.2.0 >/tmp/out 2>&1; code=$?
if [ "$code" -ne 0 ] && [ ! -e /opt/dsh/whaledraw-0.2.0.part ] && [ ! -e /opt/dsh/whaledraw-0.2.0 ] && check_sentinels && [ -d /opt/dsh/whaledraw-0.1.0 ]; then
	echo "PASS bad hash: exit $code, .part removed, other installs and sentinels untouched ($(grep -o 'removed .*' /tmp/out))"
else
	echo "FAIL bad hash cleanup exit=$code"; ls /opt/dsh; fail=1
fi
echo "== result: $([ $fail = 0 ] && echo ALL PASS || echo FAILURES)"
exit $fail
