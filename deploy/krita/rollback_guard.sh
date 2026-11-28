#!/bin/sh
# 把 krita-guard 回滚到「这个备份资料夹」里的版本。deploy_guard.sh 部署前把这个档复制成备份资料夹里的
# rollback.sh；以 root 执行：sh /var/backups/krita-guard-<时间>/rollback.sh
#
# - 每一步都印「OK」或「失败」；全部成功才印「回滚完成」，有任何一步失败就印「回滚没完成」、列出失败的
#   步骤，并以 1 结束。
# - 不重启 dsh.service，不启动 krita.service / krita-gate.service：回滚前停着的，回滚后还是停着（最后会检查）。
#   备份里的 guard 如果 Requires=/BindsTo= krita-gate（2026-11-28 以前的版本），gate 停着时就不启动 guard
#   （启动它会把 gate 和 Krita 一起拉起来），改印出要手动执行的指令。
# - guard 的开机启动、闲置重启 timer 回到部署前的状态（status-before.txt）。
# 测试可以用 KRITA_ROLLBACK_UNIT_DIR / KRITA_ROLLBACK_OPT_DIR / KRITA_ROLLBACK_SYSTEMCTL 换掉路径和 systemctl。
set -u
B=$(cd "$(dirname "$0")" && pwd)
U=${KRITA_ROLLBACK_UNIT_DIR:-/etc/systemd/system}
G=${KRITA_ROLLBACK_OPT_DIR:-/opt/krita-gate}
SYSTEMCTL=${KRITA_ROLLBACK_SYSTEMCTL:-systemctl}
GUARD=krita-works-guard.service
TIMER=krita-idle-restart.timer
UNITS="$GUARD krita-idle-restart.service $TIMER"
FILES="krita_guard.py krita_idle_restart.py krita_guard.sh"
failed=""

ok() { echo "OK    $1"; }
bad() {
	echo "失败  $1${2:+：$2}"
	failed="$failed
  - $1"
}
step() {
	name=$1
	shift
	if out=$("$@" 2>&1); then ok "$name"; else bad "$name" "$(printf '%s' "$out" | tail -n 3 | tr '\n' ' ')"; fi
}
check() {
	name=$1
	shift
	if "$@"; then ok "$name"; else bad "$name"; fi
}
is_active() { [ "$($SYSTEMCTL is-active "$1" 2>/dev/null)" = active ]; }
# status-before.txt 的一行是「<名字>: active=<值> enabled=<值>」；before <名字> active|enabled 印出那个值。
before() {
	line=$(awk -v n="$1:" '$1 == n { print; exit }' "$B/status-before.txt" 2>/dev/null)
	[ -n "$line" ] || return 0
	case "$2" in
	active) v=${line#*active=} && echo "${v%% *}" ;;
	enabled) echo "${line#*enabled=}" ;;
	esac
}
dsh_now() { $SYSTEMCTL show dsh.service -p MainPID -p NRestarts --value 2>/dev/null | tr '\n' ' '; }

if [ ! -d "$B/units" ] || [ ! -d "$B/opt" ] || [ ! -f "$B/status-before.txt" ]; then
	echo "失败  $B 不是 deploy_guard.sh 的备份资料夹（要有 units/、opt/、status-before.txt）"
	echo "回滚没完成"
	exit 1
fi

gate0=inactive; is_active krita-gate.service && gate0=active
krita0=inactive; is_active krita.service && krita0=active
dsh0=$(dsh_now)
echo "回滚到 $B 备份的版本。回滚前：krita=$krita0 krita-gate=$gate0 dsh(MainPID NRestarts)=$dsh0"

# 1. 先停 timer 和 guard，免得还原到一半时它们去动 Krita。
if [ -e "$U/$TIMER" ]; then
	step "停用闲置重启 timer" $SYSTEMCTL disable --now $TIMER
	step "停闲置重启 service" $SYSTEMCTL stop krita-idle-restart.service
fi
step "停 guard" $SYSTEMCTL stop $GUARD

# 2. 还原档案：备份里有的复制回去，部署前没有的移除。
for f in $UNITS; do
	if [ -e "$B/units/$f" ]; then
		step "还原 $U/$f" cp -a "$B/units/$f" "$U/$f"
	elif [ -e "$U/$f" ]; then
		step "移除 $U/$f（部署前没有）" rm -f "$U/$f"
	fi
done
for f in $FILES; do
	if [ -e "$B/opt/$f" ]; then
		step "还原 $G/$f" cp -a "$B/opt/$f" "$G/$f"
	elif [ -e "$G/$f" ]; then
		step "移除 $G/$f（部署前没有）" rm -f "$G/$f"
	fi
done
step "daemon-reload" $SYSTEMCTL daemon-reload

# 3. guard：开机启动照部署前；部署前在跑才启动，而且不能因此把停着的 gate 拉起来。
case "$(before krita-works-guard enabled)" in
enabled) step "guard 设为开机启动（照部署前）" $SYSTEMCTL enable $GUARD ;;
disabled) step "guard 取消开机启动（照部署前）" $SYSTEMCTL disable $GUARD ;;
esac
guard_started=no
if [ "$(before krita-works-guard active)" != active ]; then
	echo "略过  启动 guard（部署前它没在跑）"
elif [ "$gate0" != active ] && $SYSTEMCTL show $GUARD -p Requires -p BindsTo --value 2>/dev/null | tr ' ' '\n' | grep -qx krita-gate.service; then
	echo "略过  启动 guard：备份里的 guard Requires= krita-gate，现在 krita-gate 停着，启动 guard 会把它和 Krita 一起拉起来。"
	echo "      要开回 Krita 时手动执行：systemctl start krita-gate.service $GUARD"
else
	step "启动 guard" $SYSTEMCTL start $GUARD
	guard_started=yes
fi

# 4. timer 照部署前。
if [ -e "$U/$TIMER" ]; then
	if [ "$(before $TIMER enabled)" = enabled ]; then step "启用闲置重启 timer（照部署前）" $SYSTEMCTL enable $TIMER; fi
	if [ "$(before $TIMER active)" = active ]; then step "启动闲置重启 timer（照部署前）" $SYSTEMCTL start $TIMER; fi
fi

# 5. 检查结果。
gate1=inactive; is_active krita-gate.service && gate1=active
krita1=inactive; is_active krita.service && krita1=active
check "krita-gate 维持原状（$gate0 → $gate1）" [ "$gate1" = "$gate0" ]
check "krita 维持原状（$krita0 → $krita1）" [ "$krita1" = "$krita0" ]
if [ "$guard_started" = yes ]; then check "guard 是 active" is_active $GUARD; fi
if [ -e "$U/$TIMER" ] && [ "$(before $TIMER active)" = active ]; then check "闲置重启 timer 是 active" is_active $TIMER; fi
dsh1=$(dsh_now)
check "dsh 的 MainPID / NRestarts 没变（$dsh1）" [ "$dsh1" = "$dsh0" ]

if [ -n "$failed" ]; then
	echo "回滚没完成，失败的步骤：$failed"
	echo "备份还在 $B，请照上面的错误手动处理。"
	exit 1
fi
echo "回滚完成（还原到 $B 备份的版本）。"
