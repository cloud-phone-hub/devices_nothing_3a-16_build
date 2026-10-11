"""3a-16（Nothing Phone (3a) / 代号 Asteroids，Qualcomm qssi_64 / Android 16）构建环境。

各阶段脚本（build-* / unpack-roms / pack_super / pack-roms / vendor* / vendor_dlkm*）
统一从这里读取：
  - 构建选择：CDH_BUILD_ID（build-id）决定固件（roms/<build-id>.zip）与基线件；
  - 路径/命名：设备根、out/dist 布局、lineage 底包、版本目录等全部在此推导，
    可被 CDH_* 环境变量覆盖，脚本不硬编码绝对路径；
  - 宿主工具：debugfs / e2fsck / resize2fs / erofs-utils / lz4 / mtools / qemu-img / baselines。

设备事实（与 note13-5G-15 的差异）：
  - 官方固件为 **A/B OTA zip**（内含 payload.bin），unpack 阶段用 payload-dumper-go 把各
    分区解出并**平铺**到 roms/<build-id>/（无 super.img——动态逻辑分区以独立镜像分发）；
  - 动态逻辑分区（system/system_ext/product/vendor/vendor_dlkm/odm/system_dlkm）由
    pack_super 阶段用 liblp 组装为 super.img；引导分区（boot/init_boot/vendor_boot/dtbo）独立；
  - lineage 底包为 UTM virtio 虚机，Android 16 基线 lineage-23（vda 载体 raw .img 或 qcow2）。

工具解析优先级（_tool / erofs_tool / any_tool）：
  1. 环境变量直指（E2FSPROGS_<NAME> / QEMU_IMG）
  2. brew e2fsprogs sbin / erofs-utils / lz4 / mtools / Android SDK emulator 等常用安装位置
  3. PATH（shutil.which）
"""

import glob
import json
import os
import re
import shutil

# 宿主工具常用安装位置（macOS brew e2fsprogs / erofs-utils / lz4 / mtools / Android SDK）
_E2FSPROGS_SBIN = "/opt/homebrew/opt/e2fsprogs/sbin"
_EROFS_UTILS_BIN = "/opt/homebrew/opt/erofs-utils/bin"
_LZ4_BIN = "/opt/homebrew/opt/lz4/bin"
_MTOOLS_BIN = "/opt/homebrew/opt/mtools/bin"
_QEMU_CANDIDATES = (
    os.path.expanduser("~/Library/Android/sdk/emulator/qemu-img"),
    "/opt/homebrew/bin/qemu-img",
    "/usr/local/bin/qemu-img",
)


def env(name, default):
    """读取环境变量，未设置或为空时回退 default。"""
    return os.environ.get(name) or default


def _tool(name):
    """定位 e2fsprogs 工具；env 覆盖 > brew sbin > PATH，缺失时抛异常。"""
    cand = os.environ.get("E2FSPROGS_" + name.upper())
    if cand and os.path.isfile(cand):
        return cand
    brew = os.path.join(_E2FSPROGS_SBIN, name)
    if os.path.isfile(brew):
        return brew
    found = shutil.which(name)
    if found:
        return found
    raise FileNotFoundError(
        f"缺少 e2fsprogs 工具: {name}（brew install e2fsprogs）")


def e2fs_tools():
    """返回 (debugfs, e2fsck, resize2fs) 绝对路径元组。"""
    return _tool("debugfs"), _tool("e2fsck"), _tool("resize2fs")


def erofs_tool(name):
    """定位 erofs-utils 工具（fsck.erofs / dump.erofs / mkfs.erofs）。"""
    brew = os.path.join(_EROFS_UTILS_BIN, name)
    if os.path.isfile(brew):
        return brew
    found = shutil.which(name)
    if found:
        return found
    raise FileNotFoundError(
        f"缺少 erofs-utils 工具: {name}（brew install erofs-utils）")


def lz4_tool():
    """定位 lz4 命令（vendor_boot fragment 编解码）。"""
    brew = os.path.join(_LZ4_BIN, "lz4")
    if os.path.isfile(brew):
        return brew
    found = shutil.which("lz4")
    if found:
        return found
    raise FileNotFoundError("缺少 lz4（brew install lz4）")


def mtools_tool(name):
    """定位 mtools 命令（mtype/mcopy，EFI FAT 分区读写）；brew 位置 > PATH。"""
    brew = os.path.join(_MTOOLS_BIN, name)
    if os.path.isfile(brew):
        return brew
    found = shutil.which(name)
    if found:
        return found
    raise FileNotFoundError(f"缺少 mtools {name}（brew install mtools）")


def _qemu_img():
    """定位 qemu-img（QEMU_IMG 环境变量优先，其次常见安装位置，最后 PATH）。"""
    cand = os.environ.get("QEMU_IMG")
    if cand and os.path.isfile(cand):
        return cand
    for p in _QEMU_CANDIDATES:
        if os.path.isfile(p):
            return p
    found = shutil.which("qemu-img")
    if found:
        return found
    raise FileNotFoundError("缺少 qemu-img（可用 QEMU_IMG 环境变量指定）")


def any_tool(names, suffixes=()):
    """依次在 PATH 与常见位置查找任一命令，返回首个可执行绝对路径或 None。"""
    for name in names:
        found = shutil.which(name)
        if found:
            return found
        for suf in suffixes:
            cand = os.path.join(suf, name)
            if os.path.isfile(cand):
                return cand
    return None


def baselines_file():
    """定位 build/baselines/<build-id>.json（基线件清单）。

    优先 CDH_BASELINES_JSON；其次 CDH_BUILD_ID 对应文件；否则取 baselines/ 目录
    内唯一 JSON（单 ROM 设备）。无法确定时返回 None。
    """
    p = os.environ.get("CDH_BASELINES_JSON")
    if p:
        return p if os.path.isfile(p) else None
    build_dir = os.path.dirname(os.path.abspath(__file__))
    baselines_dir = os.path.join(os.path.dirname(build_dir), "baselines")
    build_id = os.environ.get("CDH_BUILD_ID")
    if build_id:
        p = os.path.join(baselines_dir, f"{build_id}.json")
        return p if os.path.isfile(p) else None
    entries = sorted(glob.glob(os.path.join(baselines_dir, "*.json")))
    return entries[0] if len(entries) == 1 else None


def load_baselines():
    """读取基线清单 JSON；文件缺失或解析失败时返回 None。"""
    p = baselines_file()
    if not p:
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# =====================================================================
# 设备事实与路径（单一数据源，env 优先，其次 manifest.json 推导）
# =====================================================================

# 本文件位于 build/scripts/ 下，向上 3 级即设备根；再向上 3 级为项目根
_DEV_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_DEV_ROOT)))

ROMS_DIR = os.path.join(_DEV_ROOT, "roms")          # 官方固件输入（非源码）
OUT_DIR = os.path.join(_DEV_ROOT, "out")            # 设备编译产物

# 设备/厂商名（版本目录用 out/device/<vendor>/<device>/<os>/<version>）
_VENDOR = "nothing"
_DEVICE = "3a-16"


def _manifest():
    """读取 build/manifest.json（固件/构建声明），缺失或解析失败返回 {}。"""
    try:
        with open(os.path.join(_DEV_ROOT, "build", "manifest.json"),
                  encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


def _manifest_build():
    builds = (_manifest().get("build") or [])
    return builds[0] if builds else {}


# ---- 构建选择：CDH_BUILD_ID 优先，其次 manifest.json 首个 build ----
BUILD_ID = env("CDH_BUILD_ID", _manifest_build().get("build-id") or "")

# Android 版本（主/主.次，取自 manifest build 的 android-version，如 16.0.0）
_ANDROID_VERSION = _manifest_build().get("android-version") or ""
OS = _ANDROID_VERSION.split(".")[0] if _ANDROID_VERSION else "16"
_ANDROID_MINOR = ".".join(_ANDROID_VERSION.split(".")[:2]) if _ANDROID_VERSION else "16.0"

# 版本串（Nothing OTA 版本，如 B4.1-260810-1153；取自 manifest build 的 version）
OS_VERSION = env("CDH_OS_VERSION", _manifest_build().get("version") or "")

# 版本目录：out/device/nothing/3a-16/<os>/<version 小写>（多设备共用的项目级 out/device）
VERSION_DIR = os.path.join(PROJECT_ROOT, "out", "device",
                           _VENDOR, _DEVICE, OS, OS_VERSION.lower())

# ---- 官方固件输入（A/B OTA zip，内含 payload.bin）----
# ROM_ZIP = roms/<build-id>.zip（CDH_FACTORY_ZIP 优先）
_FACTORY_DIR_ENV = os.environ.get("CDH_FACTORY_DIR")
ROM_ZIP = env("CDH_FACTORY_ZIP",
              os.path.join(ROMS_DIR, f"{BUILD_ID}.zip") if BUILD_ID else "")
# ROM_ROOT_DIR = OTA 解包产物目录（各分区镜像**平铺**于此，CDH_FACTORY_DIR 可覆盖）
ROM_ROOT_DIR = (_FACTORY_DIR_ENV
                if _FACTORY_DIR_ENV
                else (os.path.join(ROMS_DIR, BUILD_ID) if BUILD_ID else ""))

# 官方分区镜像（平铺在 ROM_ROOT_DIR）
def rom_img(name):
    """ROM_ROOT_DIR 下的分区镜像路径（<name>.img）。"""
    return os.path.join(ROM_ROOT_DIR, f"{name}.img")


BOOT_IMG = rom_img("boot")
INIT_BOOT_IMG = rom_img("init_boot")
VENDOR_BOOT_IMG = rom_img("vendor_boot")
DTBO_IMG = rom_img("dtbo")
VBMETA_IMG = rom_img("vbmeta")

# 官方动态逻辑分区（super 成员）——pack_super 由此组装 super.img。
# 顺序即 super 内分区顺序（liblp 布局），按官方机型常见次序排列。
SUPER_PARTITIONS = [
    "system", "system_ext", "product", "vendor", "vendor_dlkm", "odm", "system_dlkm",
]

# super.img 容量 = lineage UTM 底包 vda GPT 的 super 分区容量（实测 2026-10-10：
# first_lba=264192, last_lba=25430015, sector=512 -> 12884901888 B = 12 GiB），
# 与底包自身 super.img 文件大小一致。pack-roms 原位写入该分区，恰好填满。
SUPER_SIZE = 12884901888

# ---- lineage 底包（UTM 虚机，同 note13/popsicle 的 vda->分区 模式）----
# 解析顺序：CDH_LINEAGE_ZIP > manifest lineage-zip（存在时）> lineage-rom/*.zip 唯一件
LINEAGE_ZIP = env("CDH_LINEAGE_ZIP", "")
if not LINEAGE_ZIP:
    _man_zip = _manifest_build().get("lineage-zip") or ""
    if _man_zip:
        _cand = os.path.join(PROJECT_ROOT, "lineage-rom", _man_zip)
        if os.path.isfile(_cand):
            LINEAGE_ZIP = _cand
if not LINEAGE_ZIP:
    _zips = sorted(glob.glob(os.path.join(PROJECT_ROOT, "lineage-rom", "*.zip")))
    # 多底包共存时优先 lineage-23（本设备基线），避免字母序误选旧版
    _pref = [z for z in _zips if "lineage-23" in os.path.basename(z)]
    _zips = _pref or _zips
    LINEAGE_ZIP = _zips[0] if _zips else ""

# 底包内 vda 成员候选：lineage-23.2 起为 raw .img（直通拷贝，无需 qemu-img），
# 旧版为 qcow2（qemu-img convert）。
LINEAGE_VDA_MEMBERS = (
    "LineageOS_on_arm64.utm/Data/vda.img",
    "LineageOS_on_arm64.utm/Data/vda.qcow2",
)
LINEAGE_VDA_MEMBER = LINEAGE_VDA_MEMBERS[0]


def lineage_vda_member(names):
    """在 zip 成员名单中探测底包 vda 载体名（决定直通/转换）；未找到返回 None。"""
    for m in LINEAGE_VDA_MEMBERS:
        if m in names:
            return m
    return None


LINEAGE_OUT_DIR = env("CDH_LINEAGE_DIR",
                      os.path.join(PROJECT_ROOT, "lineage-rom", "lineage-23"))
# lineage 底包 super 逻辑分区（vendor/vendor_dlkm/odm 基座，virtio 内核匹配）
LINEAGE_SUPER_DIR = os.path.join(LINEAGE_OUT_DIR, "super")
# lineage 底包 vda 内 boot 链分区（build-boot / build-vendor-boot 基座）
LINEAGE_BOOT_IMG = os.path.join(LINEAGE_OUT_DIR, "boot_a.img")
LINEAGE_INIT_BOOT_IMG = os.path.join(LINEAGE_OUT_DIR, "init_boot_a.img")
LINEAGE_VENDOR_BOOT_IMG = os.path.join(LINEAGE_OUT_DIR, "vendor_boot_a.img")

# ---- 输出布局 ----
DIST_DIR = os.path.join(OUT_DIR, "dist")
ROM_DIST = os.path.join(DIST_DIR, "rom")        # 分发镜像（boot/vendor/super/...）
TMP_DIR = os.path.join(OUT_DIR, "tmp")          # 调试临时（不用 /tmp）
KERNEL_OUT = os.path.join(OUT_DIR, "kernel")    # 内核编译产物（Image/modules/config）
KERNEL_MODULES_DIR = os.path.join(DIST_DIR, "kernel", "modules")  # 模块归集
VENDOR_OUT = os.path.join(OUT_DIR, "vendor", "bin", "hw")         # HAL 产物
KERNEL_SRC = os.path.join(_DEV_ROOT, "kernel", "source")          # 内核源码

# ---- vendor patch 注入树 ----
VENDOR_PATCH_DIR = os.path.join(_DEV_ROOT, "build", "vendor", "patch")
# vendor 注入的第二棵树：source 子模块落盘的 /vendor 内容（out/dist/vendor，
# 相对路径即镜像内 /vendor 路径，如 bin/daemon）。由 inject 一并注入。
VENDOR_STAGE_DIR = os.path.join(DIST_DIR, "vendor")

# ---- 分发/最终产物 ----
VENDOR_DIST = os.path.join(ROM_DIST, "vendor.img")                # build-vendor 产物
VENDOR_DLKM_DIST = os.path.join(ROM_DIST, "vendor_dlkm.img")      # build-vendor_dlkm 产物
SUPER_DIST = os.path.join(ROM_DIST, "super.img")                  # pack_super 产物（liblp）
BOOT_DIST = os.path.join(ROM_DIST, "boot.img")                    # build-boot 产物（写 vda boot_a/boot_b）
INIT_BOOT_DIST = os.path.join(ROM_DIST, "init_boot.img")          # build-boot 产物（ramdisk 载体，若底包有）
VENDOR_BOOT_DIST = os.path.join(ROM_DIST, "vendor_boot.img")      # build-vendor-boot 产物（写 vda vendor_boot_a/_b）
PATCH_DIST = os.path.join(ROM_DIST, "patch.img")                  # vdc 载入的 lineage vendor/vendor_dlkm 打包镜像
VERSION_VDA = os.path.join(VERSION_DIR, "vda.img")                # pack-roms 产物
VERSION_DATA = os.path.join(VERSION_DIR, "data.qcow2")            # pack-roms 数据盘

# ---- 构建阶段 ----
STAGES = ["unpack", "kernel", "boot", "vendor_boot",
          "vendor", "vendor_dlkm", "patch", "super", "pack"]