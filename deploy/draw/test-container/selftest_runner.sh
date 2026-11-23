#!/bin/sh
# 测试 runner 自己的反证：故意放一个会失败的测试夹具，runner 必须回非 0；夹具不失败时必须回 0。
# 五种情况：只有 root 失败、只有 dsh 失败、两个都失败、测试根本没跑起来（找不到模块）→ 都要非 0；都不失败 → 要 0。
# 只跑夹具（每种情况几秒钟），不跑整套。
#   sh deploy/draw/test-container/selftest_runner.sh
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
DRAW=$(dirname "$HERE")
FAILED=0

# run_case <名称> <要放的 marker 档名（空白分隔，可为空）> <预期：zero 或 nonzero> [TESTS 的值]
run_case() {
	name=$1
	markers=$2
	expect=$3
	tests=${4:-test_runner_fixture}
	copy=$(mktemp -d)
	cp -r "$DRAW"/. "$copy"/
	rm -rf "$copy/__pycache__" "$copy/whaledraw/__pycache__"
	cat > "$copy/test_runner_fixture.py" <<'PY'
"""runner 的反证夹具：stage 资料夹里有 fail_as_root / fail_as_dsh 这个档，对应的身分就失败。"""
import os
import unittest

STAGE = os.path.dirname(os.path.abspath(__file__))


class RunnerFixture(unittest.TestCase):
    def test_this_identity_is_allowed_to_pass(self):
        who = "root" if os.geteuid() == 0 else "dsh"
        self.assertFalse(os.path.exists(os.path.join(STAGE, "fail_as_" + who)), "故意失败：" + who)
PY
	for marker in $markers; do : > "$copy/$marker"; done
	TESTS=$tests STAGE_DIR="$copy" sh "$HERE/run_linux_tests.sh" > "$copy.out" 2>&1
	code=$?
	rm -rf "$copy"
	if { [ "$expect" = zero ] && [ "$code" -eq 0 ]; } || { [ "$expect" = nonzero ] && [ "$code" -ne 0 ]; }; then
		echo "PASS  $name -> 结束码 $code（预期 $expect）"
	else
		echo "FAIL  $name -> 结束码 $code（预期 $expect）"
		cat "$copy.out"
		FAILED=1
	fi
	rm -f "$copy.out"
}

run_case "夹具都通过（正常）" "" zero
run_case "只有 root 的测试失败" "fail_as_root" nonzero
run_case "只有 dsh 的测试失败" "fail_as_dsh" nonzero
run_case "root 和 dsh 都失败" "fail_as_root fail_as_dsh" nonzero
run_case "测试根本没跑起来（模块不存在）" "" nonzero test_module_that_does_not_exist
[ "$FAILED" -eq 0 ] && echo "selftest 全部通过：runner 不会吞掉失败" || echo "selftest 有失败" >&2
exit $FAILED
