#!/usr/bin/env python3
"""3a-16 super.img 生成器与自校验读取器（纯 Python 实现 AOSP liblp 元数据）。

3a-16（Nothing Phone (3a) / Asteroids）官方固件是 **A/B OTA**，payload.bin 里各动态逻辑
分区以独立镜像分发，**没有 super.img**（实测：roms/<build-id>/ 下平铺 system/system_ext/
product/vendor/vendor_dlkm/odm/system_dlkm 七个镜像）。故 super 阶段必须自建 liblp 元数据
把它们组装成 super.img——这与 note13/popsicle 的「官方 super 字节直拷」是同一目标的两种
实现（那两台有官方 super 可抄）。

元数据形态逐字段对齐 **lineage-23.2 UTM 底包 super.img**（实测 2026-10-10 解析，见下），
而不是自行发明：底包 super 是同级已跑通机型（popsicle-16 / sm-s9380-16）实际引导时读的
那份元数据，形态已被验证。字段值：
    geometry: metadata_max_size=65536  metadata_slot_count=3  logical_block_size=4096
    header  : magic 0x414C5030  version 10.2  header_size=256  **flags=1（VIRTUAL_SUBDEVICE）**
    groups  : 3 个 = default(max 0) / virt_dynamic_partitions_a / virt_dynamic_partitions_b
              （两者 maximum_size = super_size - 4MiB = 12880707584）
    blockdev: 1 个 = "super"，first_logical_sector=2048 alignment=1MiB align_off=0
              size=super_size
    parttbl : 每个逻辑分区两条 `<name>_a`（1 个 linear extent）+ `<name>_b`（0 个 extent，
              first_extent 指向下一条），attr 全 0，`_a` 属 group 1、`_b` 属 group 2
    extent  : 单条 linear（num_sectors=容量扇区, target_type=0, target_data=super 内扇区），
              分区之间按 1MiB 对齐
`_a`/`_b` 名字必须带槽位后缀：first-stage fstab 的 logical 行用 `slotselect`，fs_mgr 用
`<fstab 分区名> + androidboot.slot_suffix` 去元数据表里查分区名（本盘 cmdline 为 `_a`）。

AOSP 结构定义见 system/core/fs_mgr/liblp/include/liblp/metadata_format.h（版本 10.x）：
  geometry 52B / header 256B（1.2 expanded）/ partitions 52B / extents 24B / groups 48B /
  block_devices 64B；校验一律 sha256，且计算时把自身的校验字段置零。

super.img 物理布局：
    [0,4096)     保留区（防误判引导扇区）
    [4096,8192)  geometry 主副本
    [8192,12288) geometry 备份
    [12288,..)   元数据槽位（3 主 + 3 备，各 64KiB，全部写同一有效元数据）
    [1MiB,..)    逻辑分区数据（first_logical_sector = 2048）

产物 = out/dist/rom/super.img，容量 = 底包 vda GPT 的 super 分区容量（SUPER_SIZE），
pack-roms 原位写入该分区。写完做回读自检：分区名/extent 容量/镜像字节逐分区 sha256 比对。

用法（可在任何目录）：
  python3 build/scripts/pack_super.py
  python3 build/scripts/pack_super.py --dry-run    # 只打印分区表不执行
"""

import argparse
import hashlib
import os
import shutil
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from buildenv import ROM_ROOT_DIR, SUPER_DIST, SUPER_PARTITIONS, SUPER_SIZE  # noqa: E402
from buildenv import rom_img  # noqa: E402

# ---- liblp 常量（与底包实测一致）----
SECTOR = 512
RESERVED_BYTES = 4096                  # LP_PARTITION_RESERVED_BYTES
GEOMETRY_SIZE = 4096                   # LP_METADATA_GEOMETRY_SIZE
METADATA_MAX_SIZE = 65536              # geometry.metadata_max_size
METADATA_SLOT_COUNT = 3                # geometry.metadata_slot_count（底包同值）
LOGICAL_BLOCK_SIZE = 4096              # geometry.logical_block_size
GEOMETRY_MAGIC = 0x616C4467            # "gDla"
HEADER_MAGIC = 0x414C5030              # "0LPA"
MAJOR_VERSION = 10
MINOR_VERSION = 2
HEADER_FLAGS_VIRTUAL_SUBDEVICE = 1     # header.flags @128，底包实测 =1

FIRST_LOGICAL_SECTOR = 2048            # 1MiB
ALIGNMENT = 1048576                    # 1MiB
ALIGNMENT_OFFSET = 0
SECTORS_PER_ALIGNMENT = ALIGNMENT // SECTOR
GROUP_UNLIMITED = 0                    # "default" 组 maximum_size=0 表示不限
GROUP_MAX_SLACK = 4 * 1024 * 1024      # 动态组容量 = super - 4MiB（底包实测）

LP_TARGET_TYPE_LINEAR = 0

# 结构体内偏移（单一数据源，构造与回读共用）
GEOM_MAX_SIZE_OFF = 40
GEOM_SLOT_COUNT_OFF = 44
GEOM_BLOCK_SIZE_OFF = 48
GEOM_CHECKSUM_OFF = 8
GEOM_STRUCT_SIZE = 52

HEADER_SIZE = 256
HEADER_CHECKSUM_OFF = 12
HEADER_TABLES_SIZE_OFF = 44
HEADER_TABLES_CHECKSUM_OFF = 48
HEADER_PARTITIONS_OFF = 80
HEADER_EXTENTS_OFF = 92
HEADER_GROUPS_OFF = 104
HEADER_BLOCK_DEVICES_OFF = 116
HEADER_FLAGS_OFF = 128

PARTITION_ENTRY_SIZE = 52              # 36s name + attr + first_extent + num_extents + group + 保留
EXTENT_ENTRY_SIZE = 24                 # Q num_sectors + I target_type + Q target_data + I source
GROUP_ENTRY_SIZE = 48                  # 36s name + I flags + Q maximum_size
BLOCKDEV_ENTRY_SIZE = 64               # Q fls + I align + I align_off + Q size + 36s name + I flags

GROUP_DEFAULT = 0
GROUP_A = 1
GROUP_B = 2

META_START = RESERVED_BYTES + GEOMETRY_SIZE * 2     # 12288


def _align_up(value, unit):
    return (value + unit - 1) // unit * unit


def _sha256(data):
    return hashlib.sha256(data).digest()


def _pad_name(name, length):
    b = name.encode("ascii")
    if len(b) > length:
        raise ValueError(f"name too long: {name}")
    return b + b"\x00" * (length - len(b))


def _build_geometry():
    """LpMetadataGeometry：magic + struct_size + sha256 + 三个上限字段（校验字段置零后算）。"""
    g = bytearray(GEOMETRY_SIZE)
    struct.pack_into("<I", g, 0, GEOMETRY_MAGIC)
    struct.pack_into("<I", g, 4, GEOM_STRUCT_SIZE)
    struct.pack_into("<I", g, GEOM_MAX_SIZE_OFF, METADATA_MAX_SIZE)
    struct.pack_into("<I", g, GEOM_SLOT_COUNT_OFF, METADATA_SLOT_COUNT)
    struct.pack_into("<I", g, GEOM_BLOCK_SIZE_OFF, LOGICAL_BLOCK_SIZE)
    g[GEOM_CHECKSUM_OFF:GEOM_CHECKSUM_OFF + 32] = _sha256(g[:GEOM_STRUCT_SIZE])
    return bytes(g)


def _build_metadata(super_size, names, extents):
    """组装 header + 4 张表。

    names:   逻辑分区名（不含槽位后缀），按 super 内顺序；每个生成 `<n>_a`（1 extent）
             与 `<n>_b`（0 extent）两条表项，与底包一致。
    extents: [(num_sectors, target_data_sector)]，与 `_a` 分区一一对应。
    """
    def desc(offset, num, entry_size):
        return struct.pack("<III", offset, num, entry_size)

    n_part = len(names) * 2
    n_ext = len(extents)
    n_grp = 3
    n_dev = 1

    part_size = n_part * PARTITION_ENTRY_SIZE
    ext_size = n_ext * EXTENT_ENTRY_SIZE
    grp_size = n_grp * GROUP_ENTRY_SIZE
    dev_size = n_dev * BLOCKDEV_ENTRY_SIZE
    tables_size = part_size + ext_size + grp_size + dev_size

    tables = bytearray(tables_size)
    off = 0
    # partitions：_a/_b 交替；_a 用第 k 个 extent，_b 零 extent 且 first_extent 指向下一条
    extent_index = 0
    for base_name in names:
        for suffix, group in (("_a", GROUP_A), ("_b", GROUP_B)):
            num_ext = 1 if suffix == "_a" else 0
            struct.pack_into("<36s5I", tables, off, _pad_name(base_name + suffix, 36),
                             0,                       # attrs
                             extent_index,            # first_extent_index
                             num_ext,                 # num_extents
                             group, 0)                # group_index + 保留
            extent_index += num_ext
            off += PARTITION_ENTRY_SIZE
    # extents：linear
    for num_sectors, target_data in extents:
        struct.pack_into("<QIQI", tables, off,
                         num_sectors, LP_TARGET_TYPE_LINEAR, target_data, 0)
        off += EXTENT_ENTRY_SIZE
    # groups
    dyn_max = super_size - GROUP_MAX_SLACK
    for gname, maximum in (("default", GROUP_UNLIMITED),
                           ("virt_dynamic_partitions_a", dyn_max),
                           ("virt_dynamic_partitions_b", dyn_max)):
        struct.pack_into("<36sIQ", tables, off, _pad_name(gname, 36), 0, maximum)
        off += GROUP_ENTRY_SIZE
    # block_devices：super
    struct.pack_into("<Q", tables, off + 0, FIRST_LOGICAL_SECTOR)
    struct.pack_into("<I", tables, off + 8, ALIGNMENT)
    struct.pack_into("<I", tables, off + 12, ALIGNMENT_OFFSET)
    struct.pack_into("<Q", tables, off + 16, super_size)
    struct.pack_into("<36sI", tables, off + 24, _pad_name("super", 36), 0)
    off += BLOCKDEV_ENTRY_SIZE

    hdr = bytearray(HEADER_SIZE)
    struct.pack_into("<I", hdr, 0, HEADER_MAGIC)
    struct.pack_into("<H", hdr, 4, MAJOR_VERSION)
    struct.pack_into("<H", hdr, 6, MINOR_VERSION)
    struct.pack_into("<I", hdr, 8, HEADER_SIZE)
    struct.pack_into("<I", hdr, HEADER_TABLES_SIZE_OFF, tables_size)
    hdr[HEADER_TABLES_CHECKSUM_OFF:HEADER_TABLES_CHECKSUM_OFF + 32] = _sha256(tables)
    struct.pack_into("<III", hdr, HEADER_PARTITIONS_OFF, 0, n_part, PARTITION_ENTRY_SIZE)
    struct.pack_into("<III", hdr, HEADER_EXTENTS_OFF, part_size, n_ext, EXTENT_ENTRY_SIZE)
    struct.pack_into("<III", hdr, HEADER_GROUPS_OFF, part_size + ext_size,
                     n_grp, GROUP_ENTRY_SIZE)
    struct.pack_into("<III", hdr, HEADER_BLOCK_DEVICES_OFF, part_size + ext_size + grp_size,
                     n_dev, BLOCKDEV_ENTRY_SIZE)
    struct.pack_into("<I", hdr, HEADER_FLAGS_OFF, HEADER_FLAGS_VIRTUAL_SUBDEVICE)
    hdr[HEADER_CHECKSUM_OFF:HEADER_CHECKSUM_OFF + 32] = _sha256(bytes(hdr))
    return bytes(hdr) + bytes(tables)


def plan_extents(partitions):
    """partitions: [(name, image_path)] -> [(name, image_path, num_sectors, target_data)]。

    extent 容量按 4096（logical_block_size）向上对齐，分区起始按 1MiB 对齐，
    与底包布局一致。
    """
    out = []
    cursor = FIRST_LOGICAL_SECTOR
    for name, image_path in partitions:
        size = os.path.getsize(image_path)
        num_sectors = _align_up(size, LOGICAL_BLOCK_SIZE) // SECTOR
        out.append((name, image_path, num_sectors, cursor))
        cursor = _align_up(cursor + num_sectors, SECTORS_PER_ALIGNMENT)
    return out


def build_super_image(partitions, super_size, out_path):
    """按 partitions（[(name, image_path)]）组装 super.img（liblp 元数据 + 数据区）。"""
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    laid = plan_extents(partitions)
    req_bytes = laid[-1][3] * SECTOR + laid[-1][2] * SECTOR if laid else 0
    if super_size < req_bytes:
        raise ValueError(f"super 空间不足: 需 {req_bytes} B, super_size={super_size}")

    geometry = _build_geometry()
    metadata = _build_metadata(super_size, [n for n, _ in partitions],
                               [(ns, td) for _n, _p, ns, td in laid])
    if len(metadata) > METADATA_MAX_SIZE:
        raise ValueError(f"元数据超出槽位: {len(metadata)} > {METADATA_MAX_SIZE}")
    slot_blob = metadata.ljust(METADATA_MAX_SIZE, b"\x00")

    total_data = sum(os.path.getsize(p) for _n, p in partitions)
    print(f"[lp] 构建 super.img: {len(partitions)} 逻辑分区, "
          f"{total_data} B 数据 -> {out_path}")
    with open(out_path, "wb") as f:
        f.truncate(super_size)
        f.seek(RESERVED_BYTES)
        f.write(geometry)                                   # geometry 主副本
        f.write(geometry)                                   # geometry 备份
        # 主/备槽位全部写入同一有效元数据，保证任意槽位可选
        for slot in range(METADATA_SLOT_COUNT):
            f.seek(META_START + slot * METADATA_MAX_SIZE)
            f.write(slot_blob)
            f.seek(META_START + METADATA_SLOT_COUNT * METADATA_MAX_SIZE
                   + slot * METADATA_MAX_SIZE)
            f.write(slot_blob)
        for name, image_path, _ns, target_data in laid:
            f.seek(target_data * SECTOR)
            print(f"[lp]   {name}_a <- {os.path.basename(image_path)} "
                  f"@{target_data * SECTOR} ({os.path.getsize(image_path)} B)")
            with open(image_path, "rb") as src:
                shutil.copyfileobj(src, f, 16 * 1024 * 1024)
    print(f"[lp] 完成: {os.path.getsize(out_path)} B")
    return laid


# ---------------- 回读自检 ----------------

def read_super_image(super_path, base=0):
    """读回 liblp 元数据，返回 (partitions, extents, geometry, header_fields)。

    partitions: [(name, attrs, first_extent, num_extents, group_index)]
    extents:    [(num_sectors, target_data_sector)]
    base:       super 元数据在文件内的字节偏移（独立 super.img 传 0）。
    """
    with open(super_path, "rb") as f:
        f.seek(base + RESERVED_BYTES)
        geom = f.read(GEOMETRY_SIZE)
        if struct.unpack_from("<I", geom, 0)[0] != GEOMETRY_MAGIC:
            raise SystemExit(f"[err] {super_path} geometry magic 不匹配")
        max_size = struct.unpack_from("<I", geom, GEOM_MAX_SIZE_OFF)[0]
        slot_count = struct.unpack_from("<I", geom, GEOM_SLOT_COUNT_OFF)[0]
        block_size = struct.unpack_from("<I", geom, GEOM_BLOCK_SIZE_OFF)[0]
        meta_start = base + META_START
        f.seek(meta_start)
        hdr = f.read(HEADER_SIZE)
        if struct.unpack_from("<I", hdr, 0)[0] != HEADER_MAGIC:
            raise SystemExit(f"[err] {super_path} header magic 不匹配")
        version = struct.unpack_from("<HH", hdr, 4)
        header_size = struct.unpack_from("<I", hdr, 8)[0]
        tables_size = struct.unpack_from("<I", hdr, HEADER_TABLES_SIZE_OFF)[0]
        flags = struct.unpack_from("<I", hdr, HEADER_FLAGS_OFF)[0]
        tables_base = meta_start + header_size
        f.seek(tables_base)
        tables = f.read(tables_size)

    def table(header_off):
        off, num, size = struct.unpack_from("<III", hdr, header_off)
        return tables[off:off + num * size], num, size

    pdata, pnum, psize = table(HEADER_PARTITIONS_OFF)
    edata, enum, esize = table(HEADER_EXTENTS_OFF)
    gdata, gnum, gsize = table(HEADER_GROUPS_OFF)
    bdata, bnum, bsize = table(HEADER_BLOCK_DEVICES_OFF)

    partitions = []
    for i in range(pnum):
        e = pdata[i * psize:(i + 1) * psize]
        name = e[0:36].split(b"\x00", 1)[0].decode("ascii")
        attr, fe, ne, gi = struct.unpack_from("<4I", e, 36)
        partitions.append((name, attr, fe, ne, gi))
    extents = []
    for i in range(enum):
        e = edata[i * esize:(i + 1) * esize]
        ns, ttype, td, _src = struct.unpack_from("<QIQI", e, 0)
        extents.append((ns, ttype, td))
    groups = []
    for i in range(gnum):
        e = gdata[i * gsize:(i + 1) * gsize]
        groups.append((e[0:36].split(b"\x00", 1)[0].decode("ascii"),
                       struct.unpack_from("<IQ", e, 36)))
    blockdevs = []
    for i in range(bnum):
        e = bdata[i * bsize:(i + 1) * bsize]
        fls, align, aoff = struct.unpack_from("<QII", e, 0)
        blockdevs.append((e[24:60].split(b"\x00", 1)[0].decode("ascii"),
                          fls, align, aoff, struct.unpack_from("<Q", e, 16)[0]))
    info = dict(max_size=max_size, slot_count=slot_count, block_size=block_size,
                version=version, header_size=header_size, tables_size=tables_size,
                flags=flags, groups=groups, blockdevs=blockdevs)
    return partitions, extents, info


def checksums_ok(super_path):
    """校验 geometry/header/tables 的 sha256（各自校验字段置零后重算）。"""
    with open(super_path, "rb") as f:
        f.seek(RESERVED_BYTES)
        geom_raw = f.read(GEOMETRY_SIZE)
        geom = bytearray(geom_raw)
        geom[GEOM_CHECKSUM_OFF:GEOM_CHECKSUM_OFF + 32] = bytes(32)
        if _sha256(bytes(geom[:GEOM_STRUCT_SIZE])) != geom_raw[GEOM_CHECKSUM_OFF:
                                                               GEOM_CHECKSUM_OFF + 32]:
            return False, "geometry"
        f.seek(META_START)
        hdr_raw = f.read(HEADER_SIZE)
        stored_hdr = hdr_raw[HEADER_CHECKSUM_OFF:HEADER_CHECKSUM_OFF + 32]
        stored_tables = hdr_raw[HEADER_TABLES_CHECKSUM_OFF:HEADER_TABLES_CHECKSUM_OFF + 32]
        header_size = struct.unpack_from("<I", hdr_raw, 8)[0]
        tables_size = struct.unpack_from("<I", hdr_raw, HEADER_TABLES_SIZE_OFF)[0]
        tables = f.read(tables_size)
        hdr = bytearray(hdr_raw)
        hdr[HEADER_CHECKSUM_OFF:HEADER_CHECKSUM_OFF + 32] = bytes(32)
    if _sha256(tables) != stored_tables:
        return False, "tables"
    if _sha256(bytes(hdr)) != stored_hdr:
        return False, "header"
    return True, ""


def _image_sha256(path, size):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        remaining = size
        while remaining > 0:
            buf = f.read(min(16 * 1024 * 1024, remaining))
            if not buf:
                break
            h.update(buf)
            remaining -= len(buf)
    return h.hexdigest()


def _super_region_sha256(super_path, offset, size):
    h = hashlib.sha256()
    with open(super_path, "rb") as f:
        f.seek(offset)
        remaining = size
        while remaining > 0:
            buf = f.read(min(16 * 1024 * 1024, remaining))
            if not buf:
                break
            h.update(buf)
            remaining -= len(buf)
    return h.hexdigest()


def verify(super_path, partitions):
    """回读自检：元数据校验和 + 逐分区 extent 容量与镜像 sha256 比对。"""
    parts, extents, info = read_super_image(super_path)
    print(f"[chk] geometry: max_size={info['max_size']} slot_count={info['slot_count']} "
          f"block_size={info['block_size']}")
    print(f"[chk] header: version={info['version'][0]}.{info['version'][1]} "
          f"size={info['header_size']} tables_size={info['tables_size']} flags={info['flags']}")
    print(f"[chk] groups: {[(n, m[1]) for n, m in info['groups']]}")
    print(f"[chk] block_devices: {info['blockdevs']}")
    ok, what = checksums_ok(super_path)
    if not ok:
        print(f"[err] 校验和不匹配: {what}")
        return False
    print("[ok] geometry/header/tables sha256 校验通过")

    by_name = {p[0]: p for p in parts}
    bad = 0
    for name, image_path in partitions:
        entry = by_name.get(f"{name}_a")
        if entry is None:
            print(f"[err] 元数据缺分区 {name}_a")
            bad += 1
            continue
        _attr, fe, ne, _gi = entry[1], entry[2], entry[3], entry[4]
        if ne != 1:
            print(f"[err] {name}_a num_extents={ne}（期望 1）")
            bad += 1
            continue
        ns, ttype, td = extents[fe]
        capacity = ns * SECTOR
        size = os.path.getsize(image_path)
        if ttype != LP_TARGET_TYPE_LINEAR or capacity < size:
            print(f"[err] {name}_a extent 异常: type={ttype} capacity={capacity} size={size}")
            bad += 1
            continue
        src_sha = _image_sha256(image_path, size)
        dst_sha = _super_region_sha256(super_path, td * SECTOR, size)
        same = src_sha == dst_sha
        if not same:
            bad += 1
        print(f"[chk] {name + '_a':16s} capacity={capacity:>14d} size={size:>14d} "
              f"offset={td * SECTOR:>14d} sha256={'一致' if same else '不一致!!'} "
              f"{dst_sha[:16]}")
    return bad == 0


def collect_partitions():
    """官方逻辑分区（buildenv.SUPER_PARTITIONS）在 ROM_ROOT_DIR 下的镜像，缺失即报错。

    全部取官方件：/vendor、/vendor_dlkm 由 super 挂载（内核白名单只劫持 etc/bin/lib/
    lib64/apex 等前缀，其余路径仍读官方分区），lineage 内容由 patch.img（vdc 挂
    /mnt/vendor）经重定向提供——与 note13/popsicle「官方 super 零改动」一致。
    """
    missing = []
    out = []
    for name in SUPER_PARTITIONS:
        path = rom_img(name)
        if os.path.isfile(path):
            out.append((name, path))
        else:
            missing.append(path)
    if missing:
        print(f"[err] 官方逻辑分区镜像缺失（先 unpack，目录 {ROM_ROOT_DIR}）:")
        for m in missing:
            print(f"      {m}")
        sys.exit(1)
    return out


def pack_super(dry_run=False):
    partitions = collect_partitions()
    total = sum(os.path.getsize(p) for _n, p in partitions)
    if dry_run:
        print(f"[..] (dry) super_size={SUPER_SIZE} B，{len(partitions)} 逻辑分区 "
              f"{total} B 数据 -> {SUPER_DIST}")
        for name, path in partitions:
            print(f"      {name}_a <- {path}（{os.path.getsize(path)} B）")
        return 0
    build_super_image(partitions, SUPER_SIZE, SUPER_DIST)
    if not verify(SUPER_DIST, partitions):
        print("[err] super.img 回读自检失败")
        sys.exit(1)
    print(f"[ok] super.img -> {SUPER_DIST}（{os.path.getsize(SUPER_DIST)} B）")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="3a-16 super.img 组装（官方 7 逻辑分区 + 自建 liblp 元数据）")
    ap.add_argument("--dry-run", action="store_true", help="只打印不执行")
    args = ap.parse_args()
    sys.exit(pack_super(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
