"""由 root 在拒绝外网的 systemd 暂时服务中启动本机假 Discord 验收，等它结束，核对结果，并在 root 拥有的位置写下 e2e 证明。

  sudo python3 -B /opt/dsh/run_social_e2e.py [--discord … --social … --presets … --stickers … --memes … --huanqi …]

套件旗标跟 configure_social.py 的一样（不给就是那个脚本这一版的预设）。事前要把同一组套件 plugin add 进 e2e 的家
（/var/lib/dsh/.dsh-e2e），这支程序会逐档核对那里装的跟 /opt/dsh/vendor 里的一模一样，跑完再核对一次。
e2e 单元的结束码是 0、result.json 的 19 项检查名称全部符合，才会写 /opt/dsh/e2e-attest.json；
configure_social.py 升级时会重新核对这份证明（设计与剩下的风险见 deploy/draw/DEPLOY.md 的「e2e 闸门」）。
结束码：0＝通过并写了证明；1＝被拒绝或没通过（不会留下证明）；2＝用法错误。
"""

import argparse
import json
import os
import subprocess
import sys

import configure_social as cs

UNIT = "dsh-social-e2e"
PROPERTIES = (
    "User=dsh",
    "WorkingDirectory=/srv/dsh/verification",
    "Environment=DSH_HOME=/var/lib/dsh/.dsh-e2e",
    "MemoryMax=800M",
    "CPUQuota=60%",
    "NoNewPrivileges=true",
    "CapabilityBoundingSet=",
    "ProtectSystem=strict",
    "ProtectHome=true",
    "PrivateTmp=true",
    "PrivateDevices=true",
    "ReadWritePaths=/var/lib/dsh/.dsh-e2e /srv/dsh/verification",
    "IPAddressDeny=any",
    "IPAddressAllow=localhost",
)
TIMEOUT = 1800


def unit_runner(timeout=TIMEOUT):
    """回传一个函数：启动 e2e 单元、等到它结束，回传结束码（超时就停掉单元，回传 124）。"""
    def run():
        subprocess.run(["systemctl", "stop", UNIT], capture_output=True)
        subprocess.run(["systemctl", "reset-failed", UNIT], capture_output=True)
        args = ["systemd-run", f"--unit={UNIT}", "--collect", "--wait"] + ["--property=" + prop for prop in PROPERTIES]
        try:
            return subprocess.run(args + ["/opt/dsh/node/bin/node", cs.E2E_SCRIPT], timeout=timeout).returncode
        except subprocess.TimeoutExpired:
            subprocess.run(["systemctl", "stop", UNIT], capture_output=True)
            return 124
    return run


def main(argv=None, runner=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for name in ("discord", "social", "presets", "stickers", "memes", "huanqi"):
        parser.add_argument("--" + name)
    args = parser.parse_args(argv)
    if runner is None and os.geteuid() != 0:
        print("必须以 root 执行。", file=sys.stderr)
        return 2
    try:
        bundles = cs.resolve_bundles(args.discord, args.social, args.presets, args.stickers, args.memes, args.huanqi)
        attest = cs.run_and_attest(bundles, runner or unit_runner())
    except (cs.BundleError, cs.UnsafeFile, RuntimeError) as error:
        print(f"拒绝／没通过：{error}", file=sys.stderr)
        return 1
    print(json.dumps({"attested": True, "finished_at": attest["finished_at"], "bundles": list(attest["bundles"]), "file": cs.E2E_ATTEST}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
