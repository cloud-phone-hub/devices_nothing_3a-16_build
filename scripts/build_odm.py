#!/usr/bin/env python3
"""build_odm.py：从官方 gold vendor_a.img(erofs) 提取 /odm/etc/build.prop，
把 cpu 的 abilist 声明改为纯 64 位，产出 out/dist/odm/etc/build.prop。

背景：system_server 的 ZygoteProcess 按 ro.product.cpu.abilist32 是否非空决定是否
连接 zygote_secondary。官方 odm build.prop 声明 32+64 混合（armeabi-v7a,armeabi），
而底座无 32 位 zygote -> android.display FATAL。这里把 odm 侧声明锁成纯 64 位
（ro.* 由 init 加载 build.prop 时一次设置即锁定，比 vendor_init 更早/更稳）。

流程（按设备构建约定）：
  1. fsck.erofs --extract 解包官方 vendor_a.img（erofs）到 out/tmp/odm（缓存）；
  2. 从 /odm/etc/build.prop（官方原始，1202B，键 ro.odm.product.cpu.abilist*）改：
       abilist   = arm64-v8a
       abilist32 =（清空）
       abilist64 = arm64-v8a
  3. 写出 out/dist/odm/etc/build.prop（mode 0644/0/0，供 build-patch.py 收编进 patch.img 的
     /odm 子树；运行时靠内核重定向 /odm/etc/build.prop -> /mnt/odm/etc/build.prop 生效）。

用法：在三层任一上线目录执行：
  python3 build/scripts/build_odm.py
  python3 build/scripts/build_odm.py --force   # 强制重新解包官方 vendor_a
"""

import argparse
import os
import re
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from buildenv import DIST_DIR, ROM_ROOT_DIR, TMP_DIR  # noqa: E402

VENDOR_GOLD = os.path.join(ROM_ROOT_DIR, "vendor_a.img")   # 官方 erofs vendor 分区镜像
EXTRACT_DIR = os.path.join(TMP_DIR, "odm")                 # fsck.erofs 解包缓存 out/tmp/odm
OUT_ODM_DIR = os.path.join(DIST_DIR, "odm")                # 产物 out/dist/odm/etc/build.prop

SRC_PROP = os.path.join(EXTRACT_DIR, "odm", "etc", "build.prop")
DST_PROP = os.path.join(OUT_ODM_DIR, "etc", "build.prop")

# 纯 64 位 ABI 覆盖映射：键 -> 值（None 表示整行按原键保留但清空）
ABILIST_OVERRIDES = {
    "ro.odm.product.cpu.abilist": "arm64-v8a",
    "ro.odm.product.cpu.abilist32": "",
    "ro.odm.product.cpu.abilist64": "arm64-v8a",
}


def _erofs_tool(name):
    t = shutil.which(name)
    if not t:
        print(f"[err] 缺少命令: {name}（brew install erofs-utils）", file=sys.stderr)
        sys.exit(1)
    return t


def _extract_official_vendor(force=False):
    """fsck.erofs 解包官方 vendor_a.img 到 EXTRACT_DIR（已存在且非 force 则复用）。"""
    marker = os.path.join(EXTRACT_DIR, ".ok")
    if os.path.isfile(marker) and not force:
        return
    if os.path.isdir(EXTRACT_DIR):
        shutil.rmtree(EXTRACT_DIR)
    os.makedirs(EXTRACT_DIR, exist_ok=True)
    if not os.path.isfile(VENDOR_GOLD):
        print(f"[err] 官方 vendor_a.img 缺失: {VENDOR_GOLD}", file=sys.stderr)
        sys.exit(1)
    fsck = _erofs_tool("fsck.erofs")
    print(f"[..] fsck.erofs --extract={EXTRACT_DIR} {VENDOR_GOLD}")
    subprocess.run([fsck, f"--extract={EXTRACT_DIR}", VENDOR_GOLD], check=True)
    if not os.path.isfile(SRC_PROP):
        print(f"[err] 官方 /odm/etc/build.prop 未解出: {SRC_PROP}", file=sys.stderr)
        sys.exit(1)
    with open(marker, "w") as f:
        f.write("ok\n")
    print(f"[ok] 官方 vendor_a 解包就绪（{os.path.getsize(VENDOR_GOLD)//2**20} MiB）")


def _patch_abilist(text):
    """把 ro.odm.product.cpu.abilist* 改为纯 64；其余行原样保留。"""
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        m = re.match(r"^[ \t]*(ro\.odm\.product\.cpu\.abilist(?:32|64)?)=", stripped)
        if m:
            key = m.group(1)
            val = ABILIST_OVERRIDES.get(key)
            if val is not None:
                out.append(f"{key}={val}")
                continue
        out.append(line)
    return "\n".join(out) + "\n"


def build_odm(force=False, dry_run=False):
    if dry_run:
        print(f"[..] (dry) {VENDOR_GOLD} -> {EXTRACT_DIR} -> {DST_PROP}")
        return 0
    _extract_official_vendor(force=force)
    text = open(SRC_PROP, "rb").read().decode("utf-8", "replace")
    patched = _patch_abilist(text)
    os.makedirs(OUT_ODM_DIR, exist_ok=True)
    os.makedirs(os.path.join(OUT_ODM_DIR, "etc"), exist_ok=True)
    os.makedirs(os.path.join(EXTRACT_DIR, "patched"), exist_ok=True)
    with open(DST_PROP, "wb") as f:
        f.write(patched.encode("utf-8"))
    print(f"[ok] 64 位 ABI 覆盖件 -> {DST_PROP}")
    for line in patched.splitlines():
        if "abilist" in line:
            print(f"     {line}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="官方 odm build.prop -> 纯 64 位 ABI 覆盖件")
    ap.add_argument("--force", action="store_true", help="强制重新解包官方 vendor_a.img")
    ap.add_argument("--dry-run", action="store_true", help="只打印不执行")
    args = ap.parse_args()
    sys.exit(build_odm(force=args.force, dry_run=args.dry_run))


if __name__ == "__main__":
    main()