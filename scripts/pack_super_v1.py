#!/usr/bin/env python3
"""以 lpmake 全量重建 note13-5G-15 super.img（合并 Lineage 底包 odm_a）。

背景：note13-15 虚机 boot loop 根因是官方 MTK super 没有 odm_a 逻辑分区，而固件
（Lineage 底包 vda 的 fstab）要求挂载 /odm。原「官方 super 原位覆盖」路线只能改已存在
分区数据、无法新增 odm_a。故改为参考 pioneer-phone 的 super.py：用 lpmake 重建 super
的 liblp 元数据，再按 lpdump 定位把各分区数据 dd 写入。

分区来源表：
  system/system_ext/product/system_dlkm/odm_dlkm/mi_ext  <- 官方 super 解包
  vendor/vendor_dlkm                                    <- 自制产物（不变）
  odm                                                    <- Lineage 底包 odm_a.img

super 总大小保持 = 官方 super.img 大小（<= 底包 GPT super 分区大小，pack-roms 就地替换）。

工具：宿主无 lpmake/lpdump，走 docker 镜像 room-service-ubuntu-builder:latest（已含）。
产物 = out/dist/rom/super.img，并做回读自检。

用法：
  python3 build/scripts/pack_super.py
  python3 build/scripts/pack_super.py --dry-run    # 只打印不执行
"""

import argparse
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from buildenv import (  # noqa: E402
    LINEAGE_SUPER_DIR, PROJECT_ROOT, ROM_DIST, ROM_ROOT_DIR, SUPER_DIST, SUPER_IMG,
    TMP_DIR, VENDOR_DLKM_DIST, VENDOR_DIST,
)
import lpmemeta  # noqa: E402

METADATA_SIZE = 65536
METADATA_SLOTS = 2
SECTOR_SIZE = 512
DOCKER_IMAGE = "room-service-ubuntu-builder:latest"
SECTOR = 512
COPY_CHUNK = 4 * 1024 * 1024
WORK_DIR = os.path.join(TMP_DIR, "super")

# 分区（super 内名，无槽位后缀） -> 数据来源镜像
PART_SOURCES = {
    "system": os.path.join(ROM_ROOT_DIR, "system_a.img"),
    "system_ext": os.path.join(ROM_ROOT_DIR, "system_ext_a.img"),
    "product": os.path.join(ROM_ROOT_DIR, "product_a.img"),
    "vendor": VENDOR_DIST,
    "vendor_dlkm": VENDOR_DLKM_DIST,
    "odm": os.path.join(LINEAGE_SUPER_DIR, "odm_a.img"),
    "odm_dlkm": os.path.join(ROM_ROOT_DIR, "odm_dlkm_a.img"),
    "system_dlkm": os.path.join(ROM_ROOT_DIR, "system_dlkm_a.img"),
    "mi_ext": os.path.join(ROM_ROOT_DIR, "mi_ext_a.img"),
}
# 本次修复目的：odm 缺失必须报错；其余分区缺失仅警告跳过
REQUIRED = {"odm"}

# 逻辑分区预留容量（字节）：给频繁迭代的分区分配比镜像更大的 extent，使后续
# 「只改此分区」能用 dd 原地覆盖 vda.img（新镜像 ≤ 容量即可），无需重建 super/vda。
# lpmake 的 --partition size 取 max(镜像, 预留)，写入的仍是镜像实际字节。
# 默认给 vendor 预留 512MiB（super 空闲约 2.2GiB，留足余量）。提高预留或超出后
# 须重跑 super+pack 全量重建；之后所有 ≤ 预留的改动都走 pack-roms.py --patch 增量。
# 详见 skills/build-device/SKILL.md「单分区增量补丁」。
SUPER_RESERVE = {
    "vendor": 512 * 1024 * 1024,
}


def _run_tool(name, args, capture=False):
    """执行 lpmake/lpdump（宿主无工具则走 docker 镜像）。"""
    if shutil.which(name):
        return subprocess.run([name] + args, check=True,
                              capture_output=capture, text=capture)
    return subprocess.run(
        ["docker", "run", "--rm", "-v", f"{PROJECT_ROOT}:{PROJECT_ROOT}",
         "-w", PROJECT_ROOT, "--user", f"{os.getuid()}:{os.getgid()}",
         DOCKER_IMAGE, name] + args,
        check=True, capture_output=capture, text=capture)


def pack_super(dry_run=False):
    if not os.path.isfile(SUPER_IMG):
        print(f"[err] 官方 super.img 缺失: {SUPER_IMG}（先 unpack）")
        sys.exit(1)

    super_size = os.path.getsize(SUPER_IMG)
    os.makedirs(WORK_DIR, exist_ok=True)
    os.makedirs(ROM_DIST, exist_ok=True)

    # 收集分区镜像（缺源：odm 必错，其余警告）
    parts = []
    for part, src in PART_SOURCES.items():
        if not os.path.isfile(src):
            msg = f"[warn] {part} 源缺失，跳过: {src}"
            if part in REQUIRED:
                print(f"[err] 必需分区 {part} 缺失: {src}")
                sys.exit(1)
            print(msg)
            continue
        psize = os.path.getsize(src)
        # 容量 = max(镜像, 预留)：lpmake extent 按此分配（预留冗余供后续增量补丁），
        # 实际仍只 dd 写入 psize 字节。
        capacity = max(psize, SUPER_RESERVE.get(part, 0))
        parts.append((part, src, psize, capacity))
        extra = f"（预留 {capacity} B）" if capacity != psize else ""
        print(f"[super] 分区 {part}_a ({psize} B){extra} <- {src}")
    if not parts:
        print("[err] 未收集到任何分区镜像", file=sys.stderr)
        sys.exit(1)

    # 预留冗余会撑大各 extent；若分配容量之和逼近官方 super_size，放大 super（上限由
    # pack-roms 就地替换时的 GPT super 分区容量约束，note13 底包 super 槽 12.8GiB > 9.1GiB）。
    alloc_sum = sum(cap for _p, _s, _sz, cap in parts)
    if alloc_sum + 256 * 1024 * 1024 > super_size:
        new_size = ((alloc_sum + 256 * 1024 * 1024 + (1 << 20) - 1) >> 20) << 20
        print(f"[super] 预留冗余撑大 super: {super_size} -> {new_size} B")
        super_size = new_size

    if dry_run:
        print(f"[..] (dry) lpmake 重建 super（{super_size} B），"
              f"含 {len(parts)} 个逻辑分区（odm_a 来自 Lineage 底包）")
        for p, s, sz, cap in parts:
            extra = f"（预留 {cap} B）" if cap != sz else ""
            print(f"      {p + '_a':14s} <- {s}（{sz} B）{extra}")
        return 0

    work = os.path.join(WORK_DIR, "super.work.img")
    if os.path.exists(work):
        os.remove(work)

    # 1. lpmake 仅 metadata（分区块需各自覆盖，先得到全尺寸骨架）
    lp = [
        "lpmake",
        "--metadata-size", str(METADATA_SIZE),
        "--metadata-slots", str(METADATA_SLOTS),
        "--super-name", "super",
        "--device", f"super:{super_size}",
        "--group", f"main_a:{super_size}",
        "--group", "main_b:0",
        "--virtual-ab",
    ]
    for part, _src, _psize, capacity in parts:
        # extent 按容量分配（含预留冗余）；分区仍标 readonly，数据后续 dd 写入。
        lp += ["--partition", f"{part}_a:readonly:{capacity}:main_a"]
    lp += ["--force-full-image", "--output", work]
    print("[super] lpmake（仅 metadata）...")
    _run_tool("lpmake", lp)

    # 2. lpdump 定位各分区起始扇区
    print("[super] lpdump 解析分区偏移...")
    result = _run_tool("lpdump", [work], capture=True)
    offsets = {}
    for line in result.stdout.splitlines():
        if not line.startswith("super:"):
            continue
        fields = line.split()
        start_sector = int(fields[1])
        part_name = fields[4].rstrip(":")
        offsets[part_name] = start_sector

    # 3. 按偏移 dd 写入各分区数据
    print("[super] 写入分区数据...")
    with open(work, "r+b") as out:
        for part, src, psize, _capacity in parts:
            pname = f"{part}_a"
            sector = offsets.get(pname)
            if sector is None:
                print(f"[warn] {pname} 偏移未找到，跳过写入")
                continue
            print(f"[super] dd {pname} @ sector {sector}（{os.path.basename(src)}）")
            out.seek(sector * SECTOR)
            remaining = psize
            with open(src, "rb") as r:
                while remaining > 0:
                    chunk = min(remaining, COPY_CHUNK)
                    out.write(r.read(chunk))
                    remaining -= chunk

    # 4. 回读自检（校验写入分区 + odm_a 存在）
    ok = True
    for part, src, _psize, _capacity in parts:
        if not lpmemeta.verify_partition(work, part, src):
            ok = False
    if not ok:
        print("[err] 回读校验失败，不产出 super.img")
        sys.exit(1)

    shutil.copy2(work, SUPER_DIST)
    print(f"[ok] super.img -> {SUPER_DIST}（{os.path.getsize(SUPER_DIST) // 2**20} MB，"
          f"含 odm_a）")
    return 0


def main():
    ap = argparse.ArgumentParser(description="note13-5G-15 super.img lpmake 全量重建")
    ap.add_argument("--dry-run", action="store_true", help="只打印不执行")
    args = ap.parse_args()
    sys.exit(pack_super(dry_run=args.dry_run))


if __name__ == "__main__":
    main()