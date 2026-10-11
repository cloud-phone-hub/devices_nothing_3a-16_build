"""vendor_dlkm /etc/build.prop 基线对齐（参考 popsicle-16 / note13-5G-15）。

官方 3a-16（Nothing Phone (3a) / Asteroids）vendor_dlkm 的 /etc/build.prop 是真机
设备身份（brand / model / fingerprint），而 lineage 基座渲染的是 VirtIO 版本。按基线
清单 vendor-dlkm.baseline-build-prop 自官方 vendor_dlkm.img（OTA 解包件，EROFS，镜像
根即分区根，故基线件路径为 /etc/build.prop；用 dump.erofs 提取）取得官方件，按
length+sha256 校验后覆盖写入基座 /etc/build.prop。
SELinux 标签沿用基线 label（vendor_configs_file 域，/etc 配置件，同 pixel6-15）。
"""

import hashlib
import os
import subprocess
import sys

from buildenv import erofs_tool, load_baselines, rom_img
from debugfs import path_exists

# 官方 vendor_dlkm（OTA payload 解包件，EROFS；基线件来源）
VENDOR_DLKM_OFFICIAL = rom_img("vendor_dlkm")


def _baseline_build_prop():
    """自基线清单取 /etc/build.prop 基线件：(img_path, length, sha256, label)。"""
    e = (load_baselines() or {}).get("vendor-dlkm", {}).get("baseline-build-prop")
    if not e:
        print("[err] 基线清单缺少 vendor-dlkm.baseline-build-prop"
              "（build/baselines/<build-id>.json）")
        sys.exit(1)
    return (e["path"], e["length"], e["sha256"], e["label"])


def _official_build_prop(img_path, length, sha256, work_dir):
    """自官方 vendor_dlkm.img 提取基线件，按 length+sha256 校验，返回宿主临时文件。"""
    if not os.path.isfile(VENDOR_DLKM_OFFICIAL):
        print(f"[err] 官方 vendor_dlkm.img 缺失: {VENDOR_DLKM_OFFICIAL}（先 unpack）")
        sys.exit(1)
    tmp = os.path.join(work_dir, "build.prop")
    res = subprocess.run([erofs_tool("dump.erofs"), f"--path={img_path}", "--cat",
                          VENDOR_DLKM_OFFICIAL],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if res.returncode != 0 or not res.stdout:
        print(f"[err] 官方 vendor_dlkm 缺少基线件: {img_path}")
        sys.exit(1)
    data = res.stdout
    if len(data) != length or hashlib.sha256(data).hexdigest() != sha256:
        print(f"[err] 官方基线件校验失败 {img_path}: len={len(data)}"
              f" sha256={hashlib.sha256(data).hexdigest()}"
              f"（期望 {length} / {sha256}）")
        sys.exit(1)
    with open(tmp, "wb") as f:
        f.write(data)
    return tmp


def prop_cmds(work_img, work_dir):
    """/etc/build.prop 官方基线覆盖的 debugfs 命令清单（重写 + 归一元数据 + SELinux 标签）。"""
    bp_path, bp_len, bp_sha, bp_label = _baseline_build_prop()
    bp_tmp = _official_build_prop(bp_path, bp_len, bp_sha, work_dir)
    cmds = []
    if path_exists(work_img, bp_path):
        cmds.append(f"rm {bp_path}")
    cmds += [
        f"write {bp_tmp} {bp_path}",
        f"sif {bp_path} mode 0x81a4",
        f"sif {bp_path} uid 0",
        f"sif {bp_path} gid 0",
        f"ea_set {bp_path} security.selinux {bp_label}",
    ]
    return cmds
