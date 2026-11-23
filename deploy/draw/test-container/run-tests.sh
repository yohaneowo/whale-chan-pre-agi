#!/bin/sh
# 映像里执行：把 /stage（deploy/draw，唯读挂载）复制一份，先以 root、再以 dsh 各跑一次测试。
# 结束码：两个身分都通过才是 0；任何一个失败（有测试失败、出错、或根本没跑起来）就是 1。
#   TESTS      要跑的测试（预设 test_whaledraw；反证用的 selftest_runner.sh 会指到失败夹具）
#   /samples   选配：有 whale_sheet.png 才会跑真实样本测试（没有就跳过，输出里会写「未跑」）
# 另外：/stage/../social/persona-group.md 不在容器里，对应的那一项会跳过（本机完整 repo 里会跑）。
set -u
[ -f /stage/test_whaledraw.py ] || { echo "要把 deploy/draw 挂载到 /stage" >&2; exit 2; }
TESTS=${TESTS:-test_whaledraw}
rm -rf /root/s /tmp/sd
cp -r /stage /root/s
cp -r /stage /tmp/sd && chown -R dsh /tmp/sd
SAMPLES_ENV=
if [ -f /samples/whale_sheet.png ]; then SAMPLES_ENV="WHALEDRAW_SAMPLES=/samples"; else echo "（没有挂载样本：真实样本测试会跳过）"; fi
E="PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/opt/whaledraw-deps-lib:. $SAMPLES_ENV"

# 每个身分：测试的输出先存档，取 python 自己的结束码，再从档案里挑重点印出来。
# （不能直接 `python | grep`：那样拿到的是 grep 的结束码，测试失败也会被吞掉。）
run_as() {
	who=$1
	shift
	"$@" > "/tmp/out-$who.txt" 2>&1
	status=$?
	grep -E "^(ERROR|FAIL):|^Ran|^OK|^FAILED|skipped|Traceback|ModuleNotFoundError|ImportError" "/tmp/out-$who.txt"
	if [ "$status" -ne 0 ] && ! grep -qE "^(FAILED|OK)" "/tmp/out-$who.txt"; then
		tail -20 "/tmp/out-$who.txt"      # 没跑起来（例如 import 就出错）：把尾巴印出来
	fi
	return $status
}

echo "== root"
run_as root sh -c "cd /root/s && env $E python3 -m unittest $TESTS"
root_status=$?
echo "== dsh"
run_as dsh su dsh -s /bin/sh -c "cd /tmp/sd && env $E python3 -m unittest $TESTS"
dsh_status=$?
echo "== 结果：root 结束码 $root_status，dsh 结束码 $dsh_status"
if [ "$root_status" -ne 0 ] || [ "$dsh_status" -ne 0 ]; then
	echo "测试没有全部通过" >&2
	exit 1
fi
exit 0
