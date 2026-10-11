#!/usr/bin/env python3
"""3a-16 QTI perf2 HAL 收编：官方 vendor.qti.hardware.perf2-hal-service + 依赖库。

背景（实测 2026-10-11，实例 runtime/3a-16/10）：官方 Nothing framework 的
`ActivityThread.handleBindApplication` 里会构造 `android.util.BoostFramework`，其
`com.qualcomm.qti.Performance.connectPerfServiceLocked()` 同步调用
`IPerfManager$Stub$Proxy.setClientBinder`；对端是 system_ext 的 `vendor.perfservice`
（`/system_ext/etc/init/perfservice.rc`，本盘照官方 system 原样运行）。该服务 dlopen
`libqti-perfd-client_system.so` 去连 QTI perf HAL，而 AIDL
`vendor.qti.hardware.perf2.IPerf/default` 在 lineage 基座上不存在（实测
`service check` = not found，且 `ANDR-PERF-CLIENT-SYS: IPerf:: Perf HAL Service 2.2
is not available.` 由 system_server 与各 app 进程反复打印），于是 perfservice 收不到
任何事务（`dumpsys vendor.perfservice` = FAILED_TRANSACTION），每个 app 的主线程都停在
`setClientBinder` 的 binder 等待上并持住 `Performance` 类锁 —— 冷启动 9~25s、
`ANR ... failed to complete startup` 覆盖 tts/latin/chrome/gms/photos/settings、
桌面灰屏「Nothing Launcher isn't responding」。这就是用户报的「不能操作」。

只要该服务能注册即可解开阻塞（perf 功能本身在云机上无意义），故整件收编官方 vendor 的
HAL 二进制与其依赖库（清单由官方二进制 DT_NEEDED 逐层闭包算出，见 LIBS 注释）。
同族 popsicle-16 已用同一修法验证（其记录的阻塞栈与本案逐帧相同）。

产物（相对路径即镜像内 /vendor 路径）：
  bin/hw/vendor.qti.hardware.perf2-hal-service        (0755, vendor_hal_perf_default_exec)
  lib64/<6 个依赖库>                                   (vendor_file)
  etc/init/vendor.qti.hardware.perf2-hal-service.rc   (vendor_configs_file)
  etc/vintf/manifest/vendor.qti.hardware.perf2.xml     (vendor_configs_file)

SELinux：`vendor_hal_perf_*` 类型/域不在 lineage 基座里（基线 vendor cil 命中数 0），
由 sepolicy.perf_policy_cil 手写最小段，另补 vendor_service_contexts 映射
（见 sepolicy.PERF_VSC_LINES）。

用法：
  python3 build/vendor/source/vendor.qti.hardware.perf2/build.py
  python3 build/vendor/source/vendor.qti.hardware.perf2/build.py --dry-run
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile

# 本文件位于 build/vendor/source/vendor.qti.hardware.perf2/ 下，向上 5 级为设备根
_DEVICE_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
sys.path.insert(0, os.path.join(_DEVICE_ROOT, "build", "scripts"))

from buildenv import (  # noqa: E402
    VENDOR_STAGE_DIR, baselines_file, erofs_tool, rom_img,
)

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
VENDOR_IMG = rom_img("vendor")

BINARY = "vendor.qti.hardware.perf2-hal-service"
# 官方二进制的 DT_NEEDED 里非系统库的部分，加上它们的传递依赖（一次性收编，避免逐轮
# 试错 CANNOT LINK）。与 popsicle-16 的清单不同：Nothing 这版 perf 引擎不含
# snapdragonServices/qape/scenariod/lllite 那一族，实测官方 /lib64 里根本没有；
# 传递闭包算完只剩下面 6 个（libxml2.so 由 /system/lib64 提供，不在 vendor 内）。
LIBS = [
    "vendor.qti.hardware.perf2-V1-ndk.so",
    "libq-perflog.so",
    "libperfconfig.so",
    "libperfgluelayer.so",
    "libqti-perfd-client.so",
    "libqti-util.so",
]
RC_NAME = "vendor.qti.hardware.perf2-hal-service.rc"
VINTF_NAME = "vendor.qti.hardware.perf2.xml"

LABEL_BIN = "u:object_r:vendor_hal_perf_default_exec:s0"
LABEL_LIB = "u:object_r:vendor_file:s0"
LABEL_CONFIG = "u:object_r:vendor_configs_file:s0"


def erofs_extract(src_inner, dst, mode, dry_run=False):
    """从官方 vendor.img 提取 src_inner 到 dst（幂等：先提临时再原子替换）。"""
    rel = os.path.relpath(dst, VENDOR_STAGE_DIR)
    if dry_run:
        print(f"[..] (dry) EROFS 提取 {src_inner} -> {rel}")
        return
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".tmp"
    if os.path.exists(tmp):
        os.unlink(tmp)
    res = subprocess.run(
        [erofs_tool("fsck.erofs"), f"--extract={tmp}", f"--path={src_inner}",
         VENDOR_IMG],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if res.returncode != 0 or not os.path.isfile(tmp):
        print(res.stdout.decode("utf8", "replace"))
        print(f"[err] EROFS 提取失败: {src_inner}（rc={res.returncode}）",
              file=sys.stderr)
        if os.path.exists(tmp):
            os.unlink(tmp)
        sys.exit(1)
    os.chmod(tmp, mode)
    os.replace(tmp, dst)
    print(f"    取 {src_inner} -> {rel}")


def write_asset(src_rel, dst, dry_run=False):
    src = os.path.join(MODULE_DIR, src_rel)
    if dry_run:
        print(f"[..] (dry) 写 {os.path.relpath(dst, VENDOR_STAGE_DIR)}")
        return
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(src, encoding="utf-8") as f:
        content = f.read()
    with open(dst, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"    写 {os.path.relpath(dst, VENDOR_STAGE_DIR)}")


def register_labels(dry_run=False):
    path = baselines_file()
    if not path or not os.path.isfile(path):
        print(f"[err] 基线清单缺失: {path or 'None'}", file=sys.stderr)
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    labels = doc.setdefault("vendor", {}).setdefault("selinux-labels", {})
    entries = {f"bin/hw/{BINARY}": LABEL_BIN}
    for lib in LIBS:
        entries[f"lib64/{lib}"] = LABEL_LIB
    entries[f"etc/init/{RC_NAME}"] = LABEL_CONFIG
    entries[f"etc/vintf/manifest/{VINTF_NAME}"] = LABEL_CONFIG
    missing = {k: v for k, v in entries.items() if labels.get(k) != v}
    if not missing:
        print("[..] baselines 标签已齐，跳过")
        return
    labels.update(missing)
    if dry_run:
        print(f"[..] (dry) baselines 将登记标签 {len(missing)} 条")
        return
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path),
                               prefix=".baselines.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
    print(f"[ok] baselines 登记标签 {len(missing)} 条")


def build(dry_run=False):
    if not os.path.isfile(VENDOR_IMG) and not dry_run:
        print(f"[err] 缺官方 vendor 镜像: {VENDOR_IMG}（先 unpack）", file=sys.stderr)
        sys.exit(1)

    print("==> 收编官方 perf2 HAL 二进制与依赖库")
    erofs_extract(f"/bin/hw/{BINARY}",
                  os.path.join(VENDOR_STAGE_DIR, "bin", "hw", BINARY),
                  0o755, dry_run=dry_run)
    for lib in LIBS:
        erofs_extract(f"/lib64/{lib}",
                      os.path.join(VENDOR_STAGE_DIR, "lib64", lib),
                      0o644, dry_run=dry_run)

    print("==> rc / vintf")
    write_asset(os.path.join("vendor", "etc", "init", RC_NAME),
                os.path.join(VENDOR_STAGE_DIR, "etc", "init", RC_NAME),
                dry_run=dry_run)
    write_asset(os.path.join("vendor", "etc", "vintf", "manifest", VINTF_NAME),
                os.path.join(VENDOR_STAGE_DIR, "etc", "vintf", "manifest",
                             VINTF_NAME),
                dry_run=dry_run)

    print("==> SELinux 标签登记")
    register_labels(dry_run=dry_run)

    print("[ok] perf2 HAL 收编完成")
    return 0


def main():
    ap = argparse.ArgumentParser(description="3a-16 perf2 HAL 收编")
    ap.add_argument("--dry-run", action="store_true", help="只打印不执行")
    args = ap.parse_args()
    sys.exit(build(dry_run=args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
