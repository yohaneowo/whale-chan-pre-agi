#!/bin/sh
# 部署新版 krita-guard（只看 dsh 自己的压力、持续 15 秒才停、自动恢复）和 Krita 闲置重启 timer。
# 以 root 执行：sh deploy_guard.sh <放着 deploy/krita 档案的资料夹>
#
# 1. 先把现行的 guard / Krita 相关 unit、drop-in、脚本备份到 /var/backups/krita-guard-<时间>/，并写好
#    同一个资料夹里的 rollback.sh（一键回滚）。
# 2. 装 krita_guard.py、krita_idle_restart.py 到 /opt/krita-gate，三个 unit 到 /etc/systemd/system。
# 3. daemon-reload、重启 guard、启用 timer。不重启 dsh.service，也不重启 krita.service / krita-gate.service。
# 4. 检查：guard active、日志有新版的 guard start、gate socket 在、dsh 的 MainPID / NRestarts 没变。
#    任何一项不对就自动跑 rollback.sh。
set -eu

SRC=${1:?usage: deploy_guard.sh <folder with deploy/krita files>}
G=/opt/krita-gate
U=/etc/systemd/system
TS=$(date -u +%Y%m%d-%H%M%S)
B=/var/backups/krita-guard-$TS
NEW_UNITS="krita-works-guard.service krita-idle-restart.service krita-idle-restart.timer"
NEW_FILES="krita_guard.py krita_idle_restart.py"

fail() { echo "deploy_guard: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || fail "要用 root 执行"
for f in $NEW_FILES $NEW_UNITS; do [ -f "$SRC/$f" ] || fail "缺 $SRC/$f"; done
python3 -m py_compile "$SRC/krita_guard.py" "$SRC/krita_idle_restart.py" || fail "Python 档案不能编译"
for f in $NEW_UNITS; do cp "$SRC/$f" "/tmp/verify-$f"; done
systemd-analyze verify /tmp/verify-krita-works-guard.service /tmp/verify-krita-idle-restart.service >/dev/null 2>&1 ||
	echo "（systemd-analyze verify 有警告，照常继续；装好后会再看 unit 状态）"
rm -f /tmp/verify-krita-*

# ---- 1. 备份 ----
mkdir -p "$B/units" "$B/opt"
chmod 0700 "$B"
for f in krita-works-guard.service krita.service krita-gate.service $NEW_UNITS; do
	if [ -e "$U/$f" ]; then cp -a "$U/$f" "$B/units/"; fi
done
for d in krita.service.d krita-gate.service.d krita-works-guard.service.d; do
	if [ -d "$U/$d" ]; then cp -a "$U/$d" "$B/units/"; fi
done
for f in krita_guard.sh $NEW_FILES; do
	if [ -e "$G/$f" ]; then cp -a "$G/$f" "$B/opt/"; fi
done
if [ -d /var/lib/krita-guard ]; then cp -a /var/lib/krita-guard "$B/state"; fi
for unit in dsh krita krita-gate krita-works-guard krita-idle-restart.timer; do
	echo "$unit: active=$(systemctl is-active "$unit" || true) enabled=$(systemctl is-enabled "$unit" 2>/dev/null || true)"
done > "$B/status-before.txt"
systemctl show dsh -p MainPID -p NRestarts > "$B/dsh-before.txt"
(cd "$B" && find . -type f ! -name SHA256SUMS -exec sha256sum {} + > SHA256SUMS)

cat > "$B/rollback.sh" <<EOF
#!/bin/sh
# 回滚到 $TS 之前的 krita-guard：旧的 guard unit 和 krita_guard.sh，移除闲置重启 timer。
# 不重启 dsh.service；krita / krita-gate 维持现状（停着的话请手动 systemctl start krita-gate.service）。
set -u
B=$B
systemctl disable --now krita-idle-restart.timer 2>/dev/null
systemctl stop krita-idle-restart.service 2>/dev/null
for f in $NEW_UNITS; do
	if [ -e "\$B/units/\$f" ]; then cp -a "\$B/units/\$f" $U/\$f; else rm -f $U/\$f; fi
done
for f in $NEW_FILES; do
	if [ -e "\$B/opt/\$f" ]; then cp -a "\$B/opt/\$f" $G/\$f; else rm -f $G/\$f; fi
done
cp -a "\$B/opt/krita_guard.sh" $G/krita_guard.sh
systemctl daemon-reload
# 旧 guard Requires= krita-gate：gate 停着时 restart 会把 gate 一起带起来，和旧版行为一样。
systemctl restart krita-works-guard.service
systemctl --no-pager status krita-works-guard.service krita-gate.service dsh.service | grep -E '^(●|     Active)'
echo "已回滚到 $TS 之前的版本"
EOF
chmod 0700 "$B/rollback.sh"
echo "备份：$B（回滚：sh $B/rollback.sh）"

# ---- 2. 安装 ----
dsh_before=$(systemctl show dsh -p MainPID -p NRestarts --value | tr '\n' ' ')
install -o root -g root -m 0644 "$SRC/krita_guard.py" "$G/krita_guard.py"
install -o root -g root -m 0644 "$SRC/krita_idle_restart.py" "$G/krita_idle_restart.py"
for f in $NEW_UNITS; do install -o root -g root -m 0644 "$SRC/$f" "$U/$f"; done

# ---- 3. 套用 ----
mark=$(date -u +%Y-%m-%dT%H:%M:%S)
systemctl daemon-reload
systemctl restart krita-works-guard.service
systemctl enable krita-works-guard.service >/dev/null 2>&1
systemctl enable --now krita-idle-restart.timer >/dev/null 2>&1

# ---- 4. 检查 ----
check() {
	sleep 12
	[ "$(systemctl is-active krita-works-guard.service)" = active ] || { echo "guard 不是 active"; return 1; }
	awk -v m="$mark" '$1 >= m' /var/log/krita-guard.log | grep -q "guard start: mode=" || { echo "日志没有新版 guard start"; return 1; }
	[ "$(systemctl is-active dsh.service)" = active ] || { echo "dsh 不是 active"; return 1; }
	[ "$(systemctl show dsh -p MainPID -p NRestarts --value | tr '\n' ' ')" = "$dsh_before" ] || { echo "dsh MainPID/NRestarts 变了"; return 1; }
	if [ "$(systemctl is-active krita-gate.service)" = active ]; then
		[ -S /run/krita-gate/gate.sock ] || { echo "gate 在跑但 socket 不在"; return 1; }
	fi
	[ "$(systemctl is-active krita-idle-restart.timer)" = active ] || { echo "timer 没启用"; return 1; }
	return 0
}
if ! check; then
	echo "检查没过，自动回滚" >&2
	sh "$B/rollback.sh"
	exit 1
fi
echo "部署完成。dsh: $dsh_before（没变）"
tail -n 3 /var/log/krita-guard.log
