#!/usr/bin/env python3
"""3a-16 vendor_boot.img 处理：删除 ramdisk fragment 内的 first_stage fstab 副本 + 对齐 ro.hardware。

为什么删 fstab：lineage vendor_boot 的 platform fragment 里带一份 lineage 原版
`first_stage_ramdisk/system/etc/fstab.virtio`（含 odm 挂载、无 vdc /mnt）。GRUB 的
`eval initrd` 会把 boot.img ramdisk 排在前面、vendor_boot fragment 排在后面，initramfs
解包时后者覆盖前者，导致 init 读到的是 lineage 原版 fstab（挂 /odm 失败）。删掉 fragment
内的副本后，boot.img 注入的 fstab（build-boot.py，fstab.qcom）成为唯一来源。

ro.hardware 对齐真机：lineage 基底 bootconfig 带 `androidboot.hardware=virtio`（init 据此
定 ro.hardware=virtio，进而拼 fstab.${ro.hardware} / init.${ro.hardware}.rc）。真机为
**qcom**（官方 vendor_boot cmdline `androidboot.hardware=qcom`），本机改同值后 init 走
fstab.qcom + init.qcom.rc。**注意 qcom(4) 与 virtio(6) 不等长**，故不能像 note13 那样
等长原地替换——这里在尾部 bootconfig 区重写文本并同步 header 的 bootconfig_size。

vendor_boot v4 布局（page 对齐由 header 字段推导）：
  header(2128) → [pad] → vendor_ramdisk（各 fragment 压缩块顺序拼接）
  → [pad] → vendor_ramdisk_table（每 fragment: size/offset/type/name[32]/board_id）
  → [pad] → bootconfig
fragment 尺寸变化后须同步更新 header 的 vendor_ramdisk_size 与 table 内各 fragment 的
size/offset（table 条目数与条目大小不变）。非目标 fragment（如 recovery）原样保留。

用法（可在任何目录）：
  python3 build/scripts/build-vendor-boot.py             # 产出 out/dist/rom/vendor_boot.img
  python3 build/scripts/build-vendor-boot.py --dry-run   # 只打印不执行
"""

import argparse
import os
import shutil
import struct
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from buildenv import LINEAGE_OUT_DIR, VENDOR_BOOT_DIST  # noqa: E402
from buildenv import env  # noqa: E402

# vendor_boot 基底：lineage-23 解包产物（与 boot.img 同源）
LINEAGE_DIR = env("CDH_LINEAGE_DIR", LINEAGE_OUT_DIR)
VENDOR_BOOT_BASE = os.path.join(LINEAGE_DIR, "vendor_boot_a.img")

# vendor_boot v4 header 字段偏移（小端 u32）
VENDOR_BOOT_MAGIC = b"VNDRBOOT"
HDR_VERSION_OFF = 8
PAGE_SIZE_OFF = 12
VENDOR_RAMDISK_SIZE_OFF = 24
HEADER_SIZE_OFF = 2096
TABLE_SIZE_OFF = 2112
TABLE_NUM_OFF = 2116
TABLE_ESIZE_OFF = 2120
BOOTCONFIG_SIZE_OFF = 2124

# 要删除的 fstab 项（lineage 基底 fragment 内的原版副本，名字是 lineage 的 fstab.virtio*；
# 本机构建注入的是 fstab.qcom（见 build-boot.py），故这里删的是残留旧名副本）
FSTAB_NAMES = ("fstab.virtio", "fstab.virtio.gsi.sda", "fstab.virtio.gsi.vdc")
FSTAB_SUFFIXES = tuple("system/etc/" + n for n in FSTAB_NAMES) + FSTAB_NAMES

# ro.hardware 对齐真机（qcom）；长度可不同（本例 6->4），走 bootconfig 重写 + size 同步
HW_OLD = b"androidboot.hardware=virtio"
HW_NEW = b"androidboot.hardware=qcom"

# verified boot state：lineage 基底若带 orange，原位替换为 green（等长，补空格）——
# 真机为 green+locked；guest 报 orange+locked 不一致会让 host secure_env 拒答。
ORANGE = b"androidboot.verifiedbootstate=orange"
GREEN = b"androidboot.verifiedbootstate=green "  # 尾随空格补齐等长


def _align_up(value, align):
    return (value + align - 1) // align * align


def _is_fstab(name):
    """命中 boot.img 注入同名的 first_stage fstab（fragment 内为 first_stage_ramdisk/system/etc/...）。"""
    return any(name.endswith(sfx) for sfx in FSTAB_SUFFIXES)


def _drop_fstab_from_cpio(archive):
    """newc cpio 删除目标 fstab 项，返回 (新 cpio, 删除数)。"""
    raw_out = bytearray()
    dropped = 0
    off = 0
    n = len(archive)

    def pad4():
        nonlocal raw_out
        raw_out += b"\x00" * ((4 - len(raw_out) % 4) % 4)

    while off + 110 <= n:
        hdr = archive[off:off + 110]
        if hdr[:6] not in (b"070701", b"070702"):
            raise ValueError(f"cpio magic 错误: {hdr[:6]!r} @ {off}")
        size = int(hdr[54:62], 16)
        namesize = int(hdr[94:102], 16)
        if namesize == 0:
            raise ValueError("cpio namesize 为 0")
        name_start = off + 110
        name_end = name_start + namesize
        name_raw = archive[name_start:name_end]
        name = name_raw.rstrip(b"\x00").decode("utf-8", "replace")
        data_start = _align_up(name_end, 4)
        data_end = data_start + size
        payload = archive[data_start:data_end]

        if not _is_fstab(name):
            # newc 对齐是"绝对偏移 mod 4"，须按 raw_out 累计长度对齐
            new_hdr = bytearray(hdr)
            new_hdr[54:62] = b"%08x" % len(payload)
            raw_out += bytes(new_hdr)
            raw_out += name_raw
            pad4()
            raw_out += payload
            pad4()
        else:
            dropped += 1
        off = _align_up(data_end, 4)
        if name == "TRAILER!!!":
            break
    return bytes(raw_out), dropped


def _strip_fragment(blob):
    """若 fragment 是 legacy-lz4(newc cpio) 且含 fstab 项，删掉后重压。

    非 cpio / 解压失败 / 无目标项 => 原样返回 (blob, 0)。
    """
    if not shutil.which("lz4"):
        raise SystemExit("[err] 缺少命令: lz4")
    work = tempfile.mkdtemp(prefix="cdh-vb.")
    try:
        in_lz4 = os.path.join(work, "frag.in.lz4")
        cpio = os.path.join(work, "frag.cpio")
        out_lz4 = os.path.join(work, "frag.out.lz4")
        with open(in_lz4, "wb") as f:
            f.write(blob)
        if subprocess.run(["lz4", "-d", "-f", in_lz4, cpio],
                          capture_output=True).returncode != 0:
            return blob, 0
        archive = open(cpio, "rb").read()
        if not archive.startswith((b"070701", b"070702")):
            return blob, 0
        patched, dropped = _drop_fstab_from_cpio(archive)
        if dropped == 0:
            return blob, 0
        subprocess.run(["lz4", "-12", "-l", "-f", "-", out_lz4],
                       input=patched, check=True, capture_output=True)
        return open(out_lz4, "rb").read(), dropped
    finally:
        shutil.rmtree(work, ignore_errors=True)


def build_vendor_boot(dry_run=False):
    if not os.path.isfile(VENDOR_BOOT_BASE):
        print(f"[err] 缺少 lineage vendor_boot 基底: {VENDOR_BOOT_BASE}")
        return 1
    if dry_run:
        print(f"[..] (dry) {VENDOR_BOOT_BASE} -> {VENDOR_BOOT_DIST}"
              f"（删 fragment 内 fstab 副本: {', '.join(FSTAB_NAMES)}；"
              f"androidboot.hardware -> qcom）")
        return 0

    data = bytearray(open(VENDOR_BOOT_BASE, "rb").read())
    if data[:8] != VENDOR_BOOT_MAGIC:
        print(f"[err] 非 vendor_boot 镜像: {VENDOR_BOOT_BASE}")
        return 1

    hdr_ver = struct.unpack_from("<I", data, HDR_VERSION_OFF)[0]
    if hdr_ver != 4:
        print(f"[err] 仅支持 vendor_boot v4（当前 {hdr_ver}）")
        return 1

    page = struct.unpack_from("<I", data, PAGE_SIZE_OFF)[0]
    hdr_size = struct.unpack_from("<I", data, HEADER_SIZE_OFF)[0]
    vrs = struct.unpack_from("<I", data, VENDOR_RAMDISK_SIZE_OFF)[0]
    tbl_size, tbl_num, tbl_esize = struct.unpack_from("<III", data, TABLE_SIZE_OFF)
    bc_size = struct.unpack_from("<I", data, BOOTCONFIG_SIZE_OFF)[0]

    vr_off = _align_up(hdr_size, page)
    tbl_off = vr_off + _align_up(vrs, page)
    bc_off = tbl_off + _align_up(tbl_size, page)

    # —— verified boot state 修正（等长，全局安全）——
    if ORANGE in data:
        data = bytearray(bytes(data).replace(ORANGE, GREEN))
        print("[..] vendor_boot cmdline: verifiedbootstate orange -> green")
    else:
        print("[..] vendor_boot cmdline 无 orange，跳过")

    # —— ro.hardware 对齐真机（改写尾部 bootconfig，同步 bootconfig_size）——
    bootconfig = bytearray(data[bc_off:bc_off + bc_size])
    if HW_OLD in bootconfig:
        bootconfig = bytearray(bytes(bootconfig).replace(HW_OLD, HW_NEW))
        struct.pack_into("<I", data, BOOTCONFIG_SIZE_OFF, len(bootconfig))
        print(f"[..] vendor_boot bootconfig: androidboot.hardware virtio -> qcom"
              f"（{bc_size} -> {len(bootconfig)} B）")
    else:
        print("[warn] vendor_boot bootconfig 无 androidboot.hardware=virtio，跳过")

    seg = bytes(data[vr_off:vr_off + vrs])
    table = bytes(data[tbl_off:tbl_off + tbl_size])

    new_seg = bytearray()
    new_table = bytearray()
    dropped_total = 0
    for i in range(tbl_num):
        entry = bytearray(table[i * tbl_esize:(i + 1) * tbl_esize])
        rsize, roff, _rtype = struct.unpack_from("<III", entry, 0)
        blob = seg[roff:roff + rsize]
        new_blob, dropped = _strip_fragment(blob)
        dropped_total += dropped
        name = entry[12:44].rstrip(b"\x00").decode("latin1") or "(platform)"
        print(f"[..] fragment{i} {name!r}: {rsize} B -> {len(new_blob)} B（删 fstab {dropped} 项）")
        struct.pack_into("<II", entry, 0, len(new_blob), len(new_seg))
        new_seg += new_blob
        new_table += entry
    if dropped_total == 0:
        print(f"[warn] fragment 内未发现 first_stage fstab 副本: {VENDOR_BOOT_BASE}")

    out = bytearray(data[:vr_off])
    out += new_seg
    out += b"\x00" * (_align_up(len(out), page) - len(out))
    out += bytes(new_table)
    out += b"\x00" * (_align_up(len(out), page) - len(out))
    out += bootconfig
    struct.pack_into("<I", out, VENDOR_RAMDISK_SIZE_OFF, len(new_seg))
    struct.pack_into("<I", out, TABLE_SIZE_OFF, len(new_table))

    os.makedirs(os.path.dirname(VENDOR_BOOT_DIST), exist_ok=True)
    tmp = VENDOR_BOOT_DIST + ".new.%d" % os.getpid()
    with open(tmp, "wb") as f:
        f.write(out)
    os.replace(tmp, VENDOR_BOOT_DIST)
    print(f"[ok] vendor_boot.img -> {VENDOR_BOOT_DIST}"
          f"（{len(out)} B，删除 fstab {dropped_total} 项）")
    return 0


def main():
    ap = argparse.ArgumentParser(description="3a-16 vendor_boot.img 处理")
    ap.add_argument("--dry-run", action="store_true", help="只打印不执行")
    args = ap.parse_args()
    sys.exit(build_vendor_boot(dry_run=args.dry_run))


if __name__ == "__main__":
    main()