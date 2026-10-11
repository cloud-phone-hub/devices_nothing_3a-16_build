#!/usr/bin/env python3
"""解包 3a-16（Nothing Phone (3a) / Asteroids，Qualcomm / Android 16）构建输入。

两个独立来源：
  * 官方 A/B OTA zip（内含 payload.bin）：用 build/tools/dump_payload.py（payload-dumper-go）
    把全部 38 个分区解出并**平铺**到 roms/<build-id>/（输入分区源，无 super.img——动态
    逻辑分区以独立镜像分发，由 pack_super 阶段组装）。
  * lineage 底包（UTM 虚机，同 note13/popsicle）：从 zip 提取 vda（raw .img 或 qcow2），
    转 raw 后解析 GPT 分区表，导出 boot/super 等物理分区到 lineage-rom/<底包>/，再拆 super。

任务（可选组合）：
  extract   从官方 OTA zip 解出各分区镜像到 roms/<build-id>/（幂等）
  lineage   解 lineage 底包 vda -> GPT 分区 -> 拆 super 逻辑分区（已解压跳过）

用法（可在任何目录）：
  python3 build/scripts/unpack-roms.py                 # extract+lineage
  python3 build/scripts/unpack-roms.py extract         # 只解官方 OTA
  python3 build/scripts/unpack-roms.py lineage         # 只解 lineage 底包
  python3 build/scripts/unpack-roms.py lineage --list   # 只打印底包 GPT 分区表
"""

import os
import shutil
import struct
import subprocess
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from buildenv import (  # noqa: E402
    LINEAGE_OUT_DIR, LINEAGE_SUPER_DIR, LINEAGE_ZIP, ROM_ROOT_DIR, ROM_ZIP,
    SUPER_PARTITIONS, TMP_DIR, lineage_vda_member,
)

BUILD_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # <device>/build
DUMP_TOOL = os.path.join(BUILD_DIR, "tools", "dump_payload.py")

SECTOR = 512
GPT_SIGNATURE = b"EFI PART"
# AOSP 动态分区 (super) metadata 常量（liblp/metadata_format.h）
LP_METADATA_GEOMETRY_SIZE = 4096
LP_PARTITION_RESERVED_BYTES = 4096
LP_METADATA_HEADER_MAGIC = 0x414C5030
LP_PARTITION_ATTR_SLOT_SUFFIXED = 1 << 1
LP_PARTITION_ATTR_DISABLED = 1 << 3
SPARSE_MAGIC = 0xed26ff3a
METADATA_OFFSET = LP_PARTITION_RESERVED_BYTES + LP_METADATA_GEOMETRY_SIZE * 2
LINEAGE_TMP = os.path.join(TMP_DIR, "lineage")       # 底包临时工作区（转换 raw）
LINEAGE_UNPACKED_MARK = os.path.join(LINEAGE_OUT_DIR, ".unpacked")  # lineage 解压完成标记


# ---------------- extract：官方 OTA zip（payload.bin -> 分区镜像）----------------

def extract_ota():
    """从官方 OTA zip 解出各分区镜像到 roms/<build-id>/（平铺），幂等。

    委托 build/tools/dump_payload.py（封装 payload-dumper-go 2.1.0）：原地读 zip 内
    payload.bin，解全部分区为 <name>.img 平铺输出；已解出的分区自动跳过（幂等）。
    """
    if not os.path.isfile(ROM_ZIP):
        print(f"[skip] 固件不存在: {ROM_ZIP}")
        return False
    if not os.path.isfile(DUMP_TOOL):
        raise SystemExit(f"[err] 缺少解包工具: {DUMP_TOOL}")
    print(f"解官方 OTA: {os.path.basename(ROM_ZIP)} -> {os.path.relpath(ROM_ROOT_DIR)}")
    os.makedirs(ROM_ROOT_DIR, exist_ok=True)
    rc = subprocess.run([sys.executable, DUMP_TOOL,
                         "--input", ROM_ZIP, "--out", ROM_ROOT_DIR])
    if rc.returncode != 0:
        raise SystemExit(f"[err] dump_payload 失败 (rc={rc.returncode})")
    return True


# ---------------- GPT 磁盘（lineage 底包 vda）----------------

def read_partitions(img_path):
    """读取 GPT 磁盘分区表，返回 Partition-like 列表。"""
    with open(img_path, "rb") as f:
        hdr = f.read(SECTOR * 2)[SECTOR:SECTOR * 2]
        if hdr[0:8] != GPT_SIGNATURE:
            raise SystemExit(f"[err] 不是合法 GPT 磁盘镜像: {img_path}")
        part_lba, num_entries, entry_size = struct.unpack("<QII", hdr[72:72 + 16])
        if num_entries == 0 or entry_size == 0 or entry_size > 4096:
            raise SystemExit("[err] GPT 分区表字段异常")
        f.seek(part_lba * SECTOR)
        entries = f.read(num_entries * entry_size)
    parts = []
    for i in range(num_entries):
        e = entries[i * entry_size:(i + 1) * entry_size]
        if len(e) < entry_size:
            break
        _tguid, _uguid, first, last = struct.unpack("<16s16sQQ", e[:48])
        if first == 0 and last == 0:
            continue
        name = e[56:56 + 72].decode("utf-16-le", "ignore").rstrip("\x00 \t")
        parts.append({"name": name or f"p{i+1}", "first_lba": first, "last_lba": last})
    return parts


def export_partitions(img_path, parts, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    n = 0
    with open(img_path, "rb") as src:
        for p in parts:
            size = (p["last_lba"] - p["first_lba"] + 1) * SECTOR
            out = os.path.join(out_dir, f"{p['name']}.img")
            print(f"  导出 {p['name']} ({size/2**30:.2f} GiB) -> {os.path.basename(out)}")
            src.seek(p["first_lba"] * SECTOR)
            with open(out, "wb") as dst:
                _copy_stream(src, dst, size)
            n += 1
    return n


# ---------------- lineage 底包 ----------------

def _qemu_img():
    qi = shutil.which("qemu-img")
    if not qi:
        raise SystemExit("[err] 未找到 qemu-img，需本机安装或写入 PATH")
    return qi


def extract_vda_from_zip():
    """从底包 zip 提取 vda 载体（raw .img 直通拷贝，或 qcow2 留待转换）。"""
    if not os.path.isfile(LINEAGE_ZIP):
        print(f"[skip] 底包不存在: {LINEAGE_ZIP}")
        return None
    os.makedirs(LINEAGE_TMP, exist_ok=True)
    with zipfile.ZipFile(LINEAGE_ZIP) as zf:
        member = lineage_vda_member(zf.namelist())
        if not member:
            raise SystemExit("[err] 底包中未找到 vda 成员（vda.img/vda.qcow2）")
        ext = os.path.splitext(member)[1]
        tmp = os.path.join(LINEAGE_TMP, "vda" + ext)
        print(f"  提取 {member} -> {tmp}")
        with zf.open(member) as src, open(tmp, "wb") as dst:
            _copy_stream(src, dst)
    return tmp


def to_raw(vda, raw):
    """qcow2 转 raw；raw .img 直接复用（拷贝到工作区，避免后续被清理）。"""
    if vda.endswith(".img"):
        print(f"  vda 已是 raw: {os.path.basename(vda)}")
        shutil.copyfile(vda, raw)
        return raw
    print(f"  qemu-img convert qcow2 -> raw: {os.path.basename(raw)}")
    subprocess.run([_qemu_img(), "convert", "-f", "qcow2", "-O", "raw", vda, raw],
                   check=True)
    return raw


def _lineage_unpacked():
    """lineage 底包是否已解压：.unpacked 标记，或现存产物补认（历史解压无标记）。

    产物补认以关键产物为准（GPT super.img + vendor 阶段两个基座），半成品会重跑。
    """
    if os.path.isfile(LINEAGE_UNPACKED_MARK):
        return True
    done = [
        os.path.join(LINEAGE_OUT_DIR, "super.img"),
        os.path.join(LINEAGE_SUPER_DIR, "vendor_a.img"),
        os.path.join(LINEAGE_SUPER_DIR, "vendor_dlkm_a.img"),
    ]
    if not all(os.path.isfile(p) for p in done):
        return False
    with open(LINEAGE_UNPACKED_MARK, "w") as f:
        f.write("")
    print(f"[..] 现存产物补认完成标记: {LINEAGE_UNPACKED_MARK}")
    return True


def lineage_task(only_list=False):
    """底包 vda -> GPT 分区 -> 拆 super 逻辑分区；已解压过则跳过。"""
    if not only_list and _lineage_unpacked():
        print(f"[skip] lineage 底包已解压: {LINEAGE_OUT_DIR}")
        return 0
    tmp_vda = os.path.join(LINEAGE_TMP, "vda.raw")
    try:
        vda = extract_vda_from_zip()
        if vda is None:
            return 0
        if not os.path.isfile(tmp_vda):
            to_raw(vda, tmp_vda)
        parts = read_partitions(tmp_vda)
        if only_list:
            print("分区表 (lineage 底包):")
            for p in parts:
                size = (p["last_lba"] - p["first_lba"] + 1) * SECTOR
                print(f"  {p['name']:<24} {size/2**30:8.2f} GiB  LBA {p['first_lba']}..{p['last_lba']}")
            print(f"\n共 {len(parts)} 个分区")
            return 0
        print(f"解析 GPT 完成，{len(parts)} 个分区 -> {LINEAGE_OUT_DIR}")
        n = export_partitions(tmp_vda, parts, LINEAGE_OUT_DIR)
        print(f"[ok] 底包分区已导出 {n} 个到 {LINEAGE_OUT_DIR}")
        sup = os.path.join(LINEAGE_OUT_DIR, "super.img")
        if os.path.isfile(sup):
            m = unpack_super(sup, LINEAGE_SUPER_DIR)
            print(f"[ok] 底包 super 拆出 {m} 个逻辑分区")
        with open(LINEAGE_UNPACKED_MARK, "w") as f:
            f.write("")
        print(f"[ok] 标记解压完成: {LINEAGE_UNPACKED_MARK}")
    finally:
        shutil.rmtree(LINEAGE_TMP, ignore_errors=True)
    return 0


# ---------------- super 逻辑分区拆解（liblp）----------------

def _read_table(f, hdr, offset, desc):
    off, num, entry_size = desc
    header_size = struct.unpack_from("<I", hdr, 8)[0]
    f.seek(METADATA_OFFSET + header_size + off)
    return f.read(num * entry_size), num, entry_size


def _is_sparse(path):
    with open(path, "rb") as f:
        return struct.unpack("<I", f.read(4))[0] == SPARSE_MAGIC


def _unsparse(path):
    simg2img = shutil.which("simg2img")
    if not simg2img:
        print("    [warn] 本机无 simg2img，保留 sparse 格式。")
        return
    tmp = path + ".raw"
    subprocess.run([simg2img, path, tmp], check=True)
    os.replace(tmp, path)


def _unsparse_to(path):
    """把 sparse 镜像转为独立的 raw 文件并返回其路径（不覆盖原文件）。"""
    simg2img = shutil.which("simg2img")
    if not simg2img:
        raise SystemExit("[err] 本机无 simg2img，无法展开 super.img")
    raw = path + ".raw-unpack"
    subprocess.run([simg2img, path, raw], check=True)
    return raw


def unpack_super(super_path, out_dir):
    """Python 版 lpunpack：把 super.img 逻辑分区拆为独立镜像（AOSP liblp 通用）。

    super.img 常以 sparse 形式分发；这里先就地转 raw 再解析，以免
    liblp metadata 落在未展开区而无法定位。
    """
    if not os.path.isfile(super_path):
        print(f"[skip] 不存在: {super_path}")
        return 0
    raw = None
    if _is_sparse(super_path):
        raw = _unsparse_to(super_path)
        super_path = raw
    os.makedirs(out_dir, exist_ok=True)
    with open(super_path, "rb") as f:
        f.seek(METADATA_OFFSET)
        hdr = f.read(256)
        if struct.unpack_from("<I", hdr, 0)[0] != LP_METADATA_HEADER_MAGIC:
            raise SystemExit(f"[err] {super_path} 不是合法 super 动态分区镜像")
        pdata, pnum, pesz = _read_table(f, hdr, 0, struct.unpack_from("<III", hdr, 80))
        edata, _en, eesz = _read_table(f, hdr, 0, struct.unpack_from("<III", hdr, 92))
        exported = 0
        for i in range(pnum):
            e = pdata[i * pesz:(i + 1) * pesz]
            name = e[0:36].split(b"\x00", 1)[0].decode("ascii", "ignore")
            attrs = struct.unpack_from("<I", e, 36)[0]
            first_ext, num_ext = struct.unpack_from("<II", e, 40)
            if not name or (attrs & LP_PARTITION_ATTR_DISABLED):
                continue
            if (attrs & LP_PARTITION_ATTR_SLOT_SUFFIXED) and not name.endswith(("_a", "_b")):
                name += "_a"
            segs, total = [], 0
            for k in range(num_ext):
                et = edata[(first_ext + k) * eesz:(first_ext + k + 1) * eesz]
                nsectors, ttype, tdata, _tsource = struct.unpack_from("<QIQI", et, 0)
                if ttype != 0:  # LP_TARGET_TYPE_LINEAR
                    continue
                segs.append((tdata * SECTOR, nsectors * SECTOR))
                total += nsectors * SECTOR
            if not segs:
                continue
            out = os.path.join(out_dir, f"{name}.img")
            print(f"  逻辑分区 {name} ({total/2**30:.2f} GiB) -> {os.path.basename(out)}")
            with open(out, "wb") as dst:
                for soff, slen in segs:
                    f.seek(soff)
                    remaining = slen
                    while remaining > 0:
                        buf = f.read(min(16 * 1024 * 1024, remaining))
                        if not buf:
                            raise SystemExit("[err] super 读取越界")
                        dst.write(buf)
                        remaining -= len(buf)
            if _is_sparse(out):
                _unsparse(out)
            exported += 1
    if raw is not None and os.path.exists(raw):
        os.remove(raw)  # 释放 simg2img 展开的临时 raw
    return exported


# ---------------- 工具 ----------------

def _copy_stream(src, dst, length=None):
    chunk = 16 * 1024 * 1024
    remaining = length  # None 表示读到底
    while remaining is None or remaining > 0:
        buf = src.read(min(chunk, remaining) if remaining else chunk)
        if not buf:
            break
        dst.write(buf)
        if remaining is not None:
            remaining -= len(buf)


def _report_official():
    """打印官方分区清单（各 <name>.img 是否存在 + 大小）。"""
    if not os.path.isdir(ROM_ROOT_DIR):
        print(f"[warn] 官方解包目录不存在: {ROM_ROOT_DIR}")
        return
    imgs = sorted(f for f in os.listdir(ROM_ROOT_DIR) if f.endswith(".img"))
    print(f"[ok] 官方分区镜像 {len(imgs)} 个 -> {os.path.relpath(ROM_ROOT_DIR)}")
    missing = [p for p in SUPER_PARTITIONS if not os.path.isfile(
        os.path.join(ROM_ROOT_DIR, f"{p}.img"))]
    if missing:
        print(f"    [warn] 动态逻辑分区缺失: {', '.join(missing)}")


def main():
    argv = sys.argv[1:]
    only_list = "--list" in argv
    keys = [a.lower() for a in argv if a != "--list"]
    if not keys:
        keys = ["extract", "lineage"]

    todo = [t for t in ("extract", "lineage") if t in keys]
    if not todo:
        print("未匹配到任务，可用关键字: extract / lineage")
        return 1

    for t in todo:
        print(f"==> [{t}]")
        if t == "extract":
            extract_ota()
            _report_official()
        elif t == "lineage":
            lineage_task(only_list=only_list)
    return 0


if __name__ == "__main__":
    sys.exit(main())