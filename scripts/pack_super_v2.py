#!/usr/bin/env python3
"""以官方 super.img 为基底，原位追加自制 vendor / vendor_dlkm 逻辑分区。

与 pack_super_old.py（lpmake 全量重建 + 合并 lineage odm_a）不同，本脚本保持官方
super 的 liblp 元数据与其它分区不动，仅把 out/dist/rom 下的 vendor.img /
vendor_dlkm.img 写入官方 super 已有的 vendor_a / vendor_dlkm_a 分区数据区，
并做回读自检。官方 super.img 原文件不被修改（先复制到 work 再原位覆盖）。

⚠️ 官方 MTK super 无 odm_a 逻辑分区：若引导仍需 /odm（lineage 底包 fstab 要求），
请改用 pack_super_old.py（lpmake 重建并合并 odm_a）。本脚本只做 vendor 追加。

用法：
  python3 build/scripts/pack_super.py
  python3 build/scripts/pack_super.py --dry-run   # 只打印将执行的动作
"""

import argparse
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from buildenv import (  # noqa: E402
    ROM_DIST, SUPER_DIST, SUPER_IMG, TMP_DIR, VENDOR_DLKM_DIST, VENDOR_DIST,
)
import lpmemeta  # noqa: E402

WORK_DIR = os.path.join(TMP_DIR, "super")

# 追加进官方 super 的分区：super 内名（无槽位后缀）-> 宿主镜像
APPEND = {
    "vendor": VENDOR_DIST,
    "vendor_dlkm": VENDOR_DLKM_DIST,
}


def pack_super(dry_run=False):
    if not os.path.isfile(SUPER_IMG):
        print(f"[err] 官方 super.img 缺失: {SUPER_IMG}（先 unpack）")
        sys.exit(1)
    for part, src in APPEND.items():
        if not os.path.isfile(src):
            print(f"[err] 追加镜像缺失 {part}: {src}")
            sys.exit(1)

    # 解析官方 super，确认 vendor_a / vendor_dlkm_a 存在且容量足够
    parts, extents = lpmemeta.read_super(SUPER_IMG)
    for part, src in APPEND.items():
        alloc = lpmemeta.partition_size(parts, extents, part)
        isize = os.path.getsize(src)
        if alloc <= 0:
            print(f"[err] 官方 super 中无逻辑分区 {part}_a，无法追加")
            sys.exit(1)
        state = "容量足够" if isize <= alloc else "镜像超容量!"
        print(f"[super] {part}_a 分配 {alloc} B，镜像 {isize} B（{state}）")
        if isize > alloc:
            print(f"[err] {part} 镜像 {isize} B > 官方分配 {alloc} B，无法追加")
            sys.exit(1)

    if dry_run:
        print(f"[..] (dry) 以官方 super 为基底追加 {len(APPEND)} 个分区 -> {SUPER_DIST}")
        for part, src in APPEND.items():
            print(f"      {part}_a <- {src}（{os.path.getsize(src)} B）")
        return 0

    os.makedirs(WORK_DIR, exist_ok=True)
    os.makedirs(ROM_DIST, exist_ok=True)
    work = os.path.join(WORK_DIR, "super.work.img")
    if os.path.exists(work):
        os.remove(work)
    shutil.copy2(SUPER_IMG, work)
    print(f"[ok] 官方 super 基底副本: {SUPER_IMG} -> {work}")

    # 原位追加（覆盖 vendor_a / vendor_dlkm_a 数据区，官方元数据不动）
    lpmemeta.update_partitions(work, APPEND)

    # 回读自检
    ok = True
    for part, src in APPEND.items():
        if not lpmemeta.verify_partition(work, part, src):
            ok = False
    if not ok:
        print("[err] 回读校验失败，不产出 super.img")
        sys.exit(1)

    shutil.copy2(work, SUPER_DIST)
    print(f"[ok] super.img -> {SUPER_DIST}（官方基底 + 追加 vendor/vendor_dlkm，"
          f"{os.path.getsize(SUPER_DIST)} B）")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="note13-5G-15 super.img：官方基底 + 追加 vendor/vendor_dlkm")
    ap.add_argument("--dry-run", action="store_true", help="只打印不执行")
    args = ap.parse_args()
    sys.exit(pack_super(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
