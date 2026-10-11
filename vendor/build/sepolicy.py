"""3a-16 vendor sepolicy：向 /etc/selinux/vendor_sepolicy.cil 追加最小 CDH 声明。

内容（本盘精简版，机制承自 popsicle-16 / note13-5G-15）：
  1. pioneer 段：guest 值守域（daemon 及其自研 adbd 的 shell/sync 子进程以
     seclabel u:r:pioneer:s0 运行，user/group root 且不降权）。该域需要完全 root
     语义：凡是它作为主体的判定都要放行，逐条 allow 枚举既写不全也易碎（vendor
     CIL 追加段还存在互相覆盖丢失的历史事故）。
  2. coredomain 属性区直读段（引导修复，承自 note13 实测）。
  3. 官方 gold vendor 属性类型段（gold_props.prop_types_cil，/dev/__properties__
     类型命名空间对齐真机）。
  4. FRP 承载块设备段（云机把 ro.frp.pst 软链到未使用的 misc/vda3）。
  5. wifi AIDL HAL 段（自研 IWifi/default，承 oriole-16 已验证实现）：hal_wifi_default
     的 sys_module、扫描档位 sysfs 标签、/dev/hvc9 控制面与 /dev/hvc10 数据面授权，
     配套 vendor_service_contexts 的 IWifi 映射与 vendor_file_contexts 的设备标签
     （均由 patch_commands 一并写入）。

HAL 专属段（audio/radio/sensors/camera/keymint/vibrator/...）不在本盘：相关 HAL 尚未
移植（见 build/vendor/source/_pending）。bring-up 时的扩展点：新增一个 `<hal>_cil(vers)`
片段函数并在 hal_cil_chain 里加一行（各段共用同一次 write，顺序与覆盖语义由该链统一
保证），需要服务名映射/设备标签时用 sepolicy._contexts_append_cmds 补对应 contexts。

pioneer 的 permissive 由**内核**承担（本盘 init 内嵌 secilc 不支持 CIL `permissive`
语句，Unknown keyword → init fatal）：
  * 内核补丁：kernel/source/security/selinux/ss/services.c (cdh_mark_pioneer_permissive)
  * 内核配置：kernel/build/lineage-kernel.config
    CONFIG_CDH_PIONEER_PERMISSIVE 在 security_load_policy() 解析完策略、发布前把
    pioneer 写进 policydb->permissive_map，等价于策略文件里的 "permissive pioneer"，
    且免疫策略追加段覆盖。

因此 pioneer 段只需保留 permissive **覆盖不到** 的两类规则，加上结构性类型声明：
  1. 启动链：源域是 init（换域 execve 的前置 process transition / file execute），
     permissive 只看源上下文 type，init 不是 pioneer，覆盖不到；
  2. 反向授权：源域是对端域、目标才是 pioneer（netd fwmark / system_server 管道 /
     媒体域 binder 回程），这些判定同样不因 pioneer 是 permissive 而放行；
  3. 类型/属性声明本身：init 的 `seclabel u:r:pioneer:s0` 若解析不到该类型会直接
     Invalid argument，故 (type pioneer)/(type pioneer_exec) 及其
     domain/exec_type/file_type/vendor_file_type 归属必须始终存在。

两条 vendor cil 约束（均实测自基座 vendor_sepolicy.cil）：
  * plat 类型在 vendor cil 中必须以「版本化影子名」引用（如 init_{vers} /
    shell_exec_{vers}），版本号运行时读 /etc/selinux/plat_sepolicy_vers.txt，不得
    硬编码。vendor 自有类型（如 mediacodec）未版本化，直接引用原名；
  * 属性（domain/exec_type/file_type/vendor_file_type）以原名关联，追加单成员
    typeattributeset 与基座的大集合语句按 union 语义合并。

entrypoint 永远只落 exec_type（plat 约束 neverallow domain ~exec_type:file entrypoint），
故 pioneer_exec / shell_exec_{vers} 仅授 entrypoint。

平台策略源 = **官方（Nothing）plat_sepolicy.cil**（CDH 架构 guest 平台 = 官方 super
零改动直拷，运行时 init 内嵌 secilc 以官方 plat 声明 class/权限；与 note13 的 AOSP 15
基线不同，AOSP 16 各家内核 classmap 已分叉，「各家一致」假设不成立）。构建期据此
做两件事：
  1. sanitize_vendor_cil：清理 lineage vendor 策略里官方类表没有的权限语句（实测
     netlink_route_socket 裸 nlmsg ×1 + nlmsg allowx ×2、system firmware_load ×1 ——
     目标内核 6.12.23 classmap 同样没有这些权限，内核永远不会对其判定，剔除/裁剪
     语义等价），否则开机 secilc "Failed to resolve permission" → InitFatalReboot；
  2. validate_cil 的 class/权限/名字全集改用官方 plat（名集 = 官方 plat +
     plat_pub_versioned.cil + vendor 基座 cil 声明全集，精确名匹配），片段内自声明
     的类型（pioneer/pioneer_exec 及 gold 属性类型）跳过；引用错名不再等到开机
     secilc 失败才发现。
"""

import os
import re
import shutil
import subprocess
import sys

from buildenv import TMP_DIR, erofs_tool, rom_img
from debugfs import cat
import gold_props

SELINUX_DIR = "/etc/selinux"
VERS_FILE = SELINUX_DIR + "/plat_sepolicy_vers.txt"
POLICY_FILE = SELINUX_DIR + "/vendor_sepolicy.cil"
PUB_VER_FILE = SELINUX_DIR + "/plat_pub_versioned.cil"
# 基座内该文件的原始标签（rm+write 新 inode 后须回填，debugfs write 不带 xattr）
POLICY_LABEL = "u:object_r:vendor_configs_file:s0"
MARKER = ";; cdh 3a-16: pioneer"
# ---- coredomain 直读 vendor_default_prop（引导修复段，见 coredomain_vendor_prop_read_cil）----
VENDOR_PROP_READ_MARKER = ";; cdh 3a-16: coredomain-vendor-prop-read"

# FRP 承载块设备：真机 vendor build.prop 带 ro.frp.pst（QTI 风格路径
# /dev/block/bootdevice/by-name/frp），云机 GPT（lineage UTM 底盘）没有 frp 分区、
# 也没有该 by-name 目录，故由
# build/vendor/patch/etc/init/cph_frp_symlink.rc 把该路径软链到未使用的 misc(vda3)。
# 平台策略只给 system_server 授了 frp_block_device，而该节点的类型是 misc_block_device：
# DAC 由 ueventd 规则放开（0660 root:system），SELinux 侧此处补 allow（策略里带
# dontaudit，故日志无 AVC，表现为 open EACCES）。
FRP_MARKER = ";; cdh 3a-16: frp-block-device"
FRP_BLK_PERMS = ["open", "read", "write", "ioctl", "getattr", "map", "lock",
                 "append"]

# ---- wifi AIDL HAL（自研虚拟 HAL）注册附加（承 oriole-16 已验证版）----
# lineage 基座有 wificond + supplicant/hostapd，但没有 IWifi HAL（VINTF 未声明
# android.hardware.wifi.IWifi）→ WifiService 拿不到 chip/iface，Wi-Fi 恒不可用。
# 自研服务由 build/vendor/source/android.hardware.wifi 编译，seclabel 用基座既有的
# hal_wifi_default 域；本段补该域缺的能力 + 三个新设备/文件标签。
WIFI_MARKER = ";; cdh 3a-16: wifi-hal"
# service_contexts 映射：缺这条则 AServiceManager_addService 无上下文直接失败。
VSC = "/etc/selinux/vendor_service_contexts"
VSC_LABEL = "u:object_r:vendor_service_contexts_file:s0"
WIFI_VSC_LINES = [
    "android.hardware.wifi.IWifi                 u:object_r:hal_wifi_service:s0",
]
VFC = "/etc/selinux/vendor_file_contexts"
VFC_LABEL = "u:object_r:vendor_configs_file:s0"
# 内核模块 mac80211_hwsim 的 module param（扫描档位注入点）独立 sysfs 标签：
# sysfs 节点无法用 file_contexts 打标，只能 genfscon；标签同时挂 file_type
# （pioneer 的 file_type 宽授权 => 可写扫描档位）与 sysfs_type
# （plat 已给 hal_wifi -> sysfs_type 只读规则 => HAL 可读档位）。
WIFI_SCAN_PARAM = "cdh_wifi_scan_param"
WIFI_SCAN_PATH = "/module/mac80211_hwsim/parameters/scan_profile"
# 宿主 wifi 控制面 /dev/hvc9 的独立设备标签：基座 vendor_file_contexts 用正则
# /dev/hvc[0-9]+ 统一标 console_device，追加字面行以免把 root console(0/1) 与
# radio(4) 通道一并放开给 wifi HAL。
WIFI_CHANNEL_TYPE = "cdh_wifi_channel"
WIFI_FC_LINES = [
    "# cdh 3a-16: wifi host channel /dev/hvc9",
    "/dev/hvc9  u:object_r:cdh_wifi_channel:s0",
]
# 数据面 /dev/hvc10 由内核 mac80211_hwsim 直桥独占（filp_open/kernel_read/
# kernel_write），SELinux 主体是内核（u:r:kernel:s0）而非某个 HAL 域；基座正则
# /dev/hvc[0-9]+ -> console_device 不给 kernel 读写，故单独建标签并授权 kernel。
WIFI_DATA_TYPE = "cdh_wifi_data"
WIFI_DATA_FC_LINES = [
    "# cdh 3a-16: wifi data plane /dev/hvc10 (kernel hwsim 直桥)",
    "/dev/hvc10  u:object_r:cdh_wifi_data:s0",
]

# QTI perf2 HAL：官方 system 的 framework（android.util.BoostFramework）在每个 app 的
# handleBindApplication 里同步连它，缺它时 vendor.perfservice 一个事务都收不到，
# 全体 app 卡在 IPerfManager.setClientBinder 上（实测 2026-10-11，见
# build/vendor/source/vendor.qti.hardware.perf2/build.py 的背景说明）。HAL 已收编，
# 本段补其策略：lineage 基座与官方 plat 侧都没有任何 vendor_hal_perf* 名字（实测
# 基线 vendor cil / plat_sepolicy.cil / plat_pub_versioned.cil 命中数全 0），故类型
# 全部自声明。
PERF_MARKER = ";; cdh 3a-16: perf2-hal"
PERF_VSC_LINES = [
    "vendor.qti.hardware.perf2.IPerf/default      u:object_r:vendor_hal_perf2_service:s0",
]
# 官方 vendor_hal_perf_client 成员里对本盘有意义的非 vendor 域（裸名，按 plat 版本
# 影子 <dom>_<vers> 引用）。vendor_perfservice 另行处理（system_ext 裸名，无版本影子）。
PERF_CLIENT_DOMAINS = ("system_server", "surfaceflinger", "platform_app",
                       "priv_app", "system_app", "radio", "nfc", "bluetooth",
                       "network_stack")

# 运行态平台策略源：官方（Nothing）system 分区（unpack 自 A/B OTA 平铺产物；与 guest
# 运行时一致——super 零改动直拷）。官方 system.img 是 EROFS 且 system-as-root（镜像内
# /etc 是符号链接，宿主 dump.erofs 按 /etc 读会报 "invalid de[0].nameoff"），
# 故用 fsck.erofs --extract --path=/system/etc/selinux 提取（见 _official_plat_text）。
OFFICIAL_PLAT_IMG = rom_img("system")
OFFICIAL_PLAT_XDIR = os.path.join(TMP_DIR, "vendor", "official_plat")
# 官方 system_ext 分区：perfservice 的域 vendor_perfservice 声明在这里（实测
# system_ext_sepolicy.cil 有 (type vendor_perfservice)，且 guest ps -Z 显示该服务确以
# 此域运行）。运行态 init 把 plat + system_ext + vendor 的 cil 交给同一次 secilc 合并，
# 所以 vendor 段可直接引用该域名（perf 客户端清单需要它，见 perf_policy_cil）。
OFFICIAL_SYSEXT_IMG = rom_img("system_ext")
OFFICIAL_SYSEXT_XDIR = os.path.join(TMP_DIR, "vendor", "official_system_ext")

# ---- 服务 execv 前置检查权限集（init_{vers} -> pioneer_exec:file）----
_INIT_EXEC_P = "ioctl read open execute getattr map".split()


def _allow(target, ttype, cls, perms):
    """渲染一条 allow；权限 set 化去重排序（输出稳定、天然无重复权限）。"""
    body = " ".join(sorted(set(perms)))
    return f"(allow {target} {ttype} ({cls} ({body})))\n"


def pioneer_cil(vers: str) -> str:
    """生成 pioneer 追加段（vers = plat 策略版本后缀，如 202504）。

    内核已把 pioneer 类型置入 permissive_map（内核开关 CONFIG_CDH_PIONEER_PERMISSIVE），
    凡以 pioneer 为主体的 AVC 判定一律放行，故本段只保留 permissive **覆盖不到** 的
    两类规则：
      1. 启动链：源域是 init（换域 exec 的前置 process transition / file execute）；
      2. 反向授权：源域是对端域、目标才是 pioneer（netd / system_server / 媒体域），
         这些判定不因 pioneer 是 permissive 而放行。
    另有两条 pioneer -> {pioneer_exec,shell_exec}:file entrypoint 属结构性声明，内核侧
    已覆盖，保留以保证「内核开关关掉时仍能起服务」。
    """
    stmts = [
        "(type pioneer)\n",
        "(type pioneer_exec)\n",
        "(typeattributeset domain (pioneer))\n",
        "(typeattributeset exec_type (pioneer_exec))\n",
        "(typeattributeset file_type (pioneer_exec))\n",
        "(typeattributeset vendor_file_type (pioneer_exec))\n",
        # ---- 启动链：源域 init，permissive 覆盖不到 ----
        # init 换域拉起 pioneer 服务（noatsecure/siginh/rlimitinh 消 transition 噪音）
        _allow(f"init_{vers}", "pioneer", "process",
               "transition noatsecure siginh rlimitinh".split()),
        _allow(f"init_{vers}", "pioneer_exec", "file", _INIT_EXEC_P),
        # ---- 结构性 entrypoint（内核侧已覆盖，保留兜底）----
        _allow("pioneer", "pioneer_exec", "file", ["entrypoint"]),
        _allow("pioneer", f"shell_exec_{vers}", "file", ["entrypoint"]),
        # ---- 反向授权：源域为对端域，permissive 覆盖不到 ----
        # fwmarkd（netd 域）经 SCM_RIGHTS 持有 pioneer 的 socket fd 写 fwmark：
        # 缺 use 时 connect 在 fwmark 客户端路径被拒（avc denied { use }
        # scontext=netd tcontext=pioneer tclass=fd，note13 实测）。
        _allow(f"netd_{vers}", "pioneer", "fd", ["use"]),
        # netd InetDiag（destroySockets）要读写 pioneer 的 socket 才能完成销毁，
        # 否则每 0.5s 刷 avc denied（note13 实测）；udp 同理预留。
        # setopt：fwmarkd（binder 线程）给 connect 路径上 pioneer 的 socket 打
        # SO_MARK，缺它 connect 直接 EACCES（note13 实测，2117 次 avc 全是它）
        _allow(f"netd_{vers}", "pioneer", "tcp_socket",
               ["getattr", "getopt", "setattr", "setopt", "read", "write"]),
        _allow(f"netd_{vers}", "pioneer", "udp_socket", ["getattr", "getopt", "setattr", "read", "write"]),
        # 投屏媒体链路反向授权（note13 实测）: binder 回程（reply 携带 binder
        # 对象 / 服务回调）由对端域发起 transfer/call。缺 mediaswcodec_{vers} →
        # pioneer:binder transfer 时 Codec2 AIDL "software" 取 IConfigurable 的
        # transaction 直接 FAILED_TRANSACTION，CHECK abort（且被 vendor dontaudit
        # 吞掉 avc，logcat 无痕迹）。
        # mediacodec 是 vendor 自有类型（lineage vendor cil 未版本化声明）；
        # mediaswcodec 是 plat 公共类型（版本化影子），swcodec 软编解码进程二选一
        # 实跑，两个域都授。
        *(_allow(src, "pioneer", cls, perms)
          for src in ("mediacodec", f"mediaswcodec_{vers}",
                      f"mediaextractor_{vers}", f"mediaserver_{vers}",
                      f"audioserver_{vers}", f"cameraserver_{vers}",
                      f"surfaceflinger_{vers}", f"system_server_{vers}",
                      f"servicemanager_{vers}", f"hwservicemanager_{vers}")
          for cls, perms in (("binder", ["transfer", "call"]),
                             ("fd", ["use"]))),
        # 自研 adbd 的 shell/sync 服务子进程继承 pioneer 域（daemon 是 root 常驻服务，
        # 子进程不降权）。system_server 需把命令输出写回子进程的 stdio 管道
        # （fifo_file）、并读写它的 socketpair（unix_stream_socket），缺任一条时
        # `dumpsys`/`settings`/`pm` 一律 FAILED_TRANSACTION（note13 实测 avc：
        # comm="dumpsys" tclass=fifo_file denied { write }；
        # comm="cmd" tclass=unix_stream_socket denied { read write }）。
        _allow(f"system_server_{vers}", "pioneer", "fifo_file",
               ["read", "write", "getattr", "ioctl"]),
        _allow(f"system_server_{vers}", "pioneer", "unix_stream_socket",
               ["read", "write", "getattr", "ioctl", "getopt", "setopt", "shutdown"]),
    ]
    return f"{MARKER}: pioneer minimal declaration (kernel permissive).\n" + "".join(stmts)


_RE_CLASS = re.compile(r"\(class\s+(\S+)\s+\(([^)]*)\)\)")
_RE_COMMON = re.compile(r"\(common\s+(\S+)\s+\(([^)]*)\)\)")
_RE_CLASSCOMMON = re.compile(r"\(classcommon\s+(\S+)\s+(\S+)\)")
_RE_ALLOW = re.compile(r"\(allow\s+(\S+)\s+(\S+)\s+\((\S+)\s+\(([^)]*)\)\)\)")
_RE_TYPE = re.compile(r"\(type\s+(\S+)\)")
_RE_TYPEATTR = re.compile(r"\(typeattribute\s+(\S+)\)")
# allow 家族（同一权限语法）：用于平台兼容清理整行解析
_RE_ALLOW_FAMILY = re.compile(
    r"\((allow|dontaudit|auditallow|neverallow)\s+(\S+)\s+(\S+)\s+\((\S+)\s+\(([^)]*)\)\)\)")
# allowx：权限值可含嵌套括号（(range 0x… 0x…)），贪婪匹配到行尾
_RE_ALLOWX_FULL = re.compile(
    r"\(allowx\s+(\S+)\s+(\S+)\s+\((\S+)\s+(\S+)\s+\((.+)\)\)\)\s*$")
# 内核支持的 xperm 家族（CIL allowx 的 op 名）：6.12.23 avtab.h 仅
# AVTAB_XPERMS_IOCTLFUNCTION/IOCTLDRIVER；nlmsg（netlink 消息类型过滤）是更新
# 内核线特性，本盘内核没有。
KERNEL_XPERM_OPS = {"ioctl"}

_decl_cache = None
_name_cache = None
_sysext_cache = None


def _official_plat_text() -> str:
    """提取官方 plat_sepolicy.cil 文本（EROFS system 分区的 /etc/selinux）。

    fsck.erofs --extract --path 每次全量重提（~5MB，亚秒级），避免缓存陈旧；
    提取失败即报错退出（校验数据不允许静默降级）。两个实测坑（2026-10-09）：
      * 目标目录必须不存在（fsck.erofs 自建该目录，预先存在会静默不产出）；
      * path 必须用 system-as-root 的真实路径 /system/etc/selinux —— 镜像内
        /etc 是符号链接，fsck.erofs 沿链接查找会报 "invalid de[0].nameoff /
        failed to lookup"，提取不到。
    """
    if not os.path.isfile(OFFICIAL_PLAT_IMG):
        print(f"[err] 官方 system_a.img 缺失: {OFFICIAL_PLAT_IMG}（先 unpack）")
        sys.exit(1)
    shutil.rmtree(OFFICIAL_PLAT_XDIR, ignore_errors=True)
    parent = os.path.dirname(OFFICIAL_PLAT_XDIR)
    os.makedirs(parent, exist_ok=True)
    res = subprocess.run(
        [erofs_tool("fsck.erofs"), f"--extract={OFFICIAL_PLAT_XDIR}",
         "--path=/system/etc/selinux", OFFICIAL_PLAT_IMG],
        capture_output=True, text=True)
    cil = os.path.join(OFFICIAL_PLAT_XDIR, "plat_sepolicy.cil")
    if res.returncode != 0 or not os.path.isfile(cil):
        print(f"[err] 官方 plat 提取失败: {res.stdout.strip()} {res.stderr.strip()}")
        sys.exit(1)
    with open(cil, encoding="utf-8", errors="replace") as f:
        return f.read()


def plat_class_perms() -> dict:
    """解析官方 plat_sepolicy.cil 的 (class)/(common)/(classcommon) 声明，
    返回 {class: 合法权限集}；读不到即报错退出（校验数据不允许静默降级）。"""
    global _decl_cache
    if _decl_cache is not None:
        return _decl_cache
    blob = _official_plat_text()
    classes, commons, links = {}, {}, {}
    for raw in blob.splitlines():
        s = raw.strip()
        m = _RE_CLASS.match(s)
        if m:
            classes[m[1]] = set(m[2].split())
            continue
        m = _RE_COMMON.match(s)
        if m:
            commons[m[1]] = set(m[2].split())
            continue
        m = _RE_CLASSCOMMON.match(s)
        if m:
            links[m[1]] = m[2]
    _decl_cache = {c: own | commons.get(links.get(c), set())
                   for c, own in classes.items()}
    return _decl_cache


def _read_cil(img, path) -> str:
    blob = cat(img, path)
    if not blob:
        print(f"[err] 读不到策略声明: {path} @ {img}")
        sys.exit(1)
    return blob.decode("utf-8", "replace")


def _cil_names(text: str) -> set:
    """(type X) / (typeattribute X) 声明名集合。
    正则不误伤: (typetransition/typeattributeset 无 type 后空白，不匹配)。"""
    return {m[1] for m in _RE_TYPE.finditer(text)} | \
           {m[1] for m in _RE_TYPEATTR.finditer(text)}


def plat_name_set() -> set:
    """官方 plat cil 的 type/typeattribute 名全集。版本化影子的 typeattribute 声明
    在 plat_pub_versioned.cil（vendor 侧文件），由调用方经 _cil_names 解析后并入。"""
    global _name_cache
    if _name_cache is None:
        _name_cache = _cil_names(_official_plat_text())
    return _name_cache


def sys_ext_name_set() -> set:
    """官方 system_ext cil 的声明名全集（进程内缓存，提取失败返回空集）。

    这些名字不参与 vendor 基座校验（gold 属性段仍按原口径过滤，避免把 vendor 段
    扩到 system_ext 域上），只供 perf 段引用 vendor_perfservice 这类「system_ext
    侧声明、运行态与 vendor cil 同一次 secilc 合并」的域名。缺镜像/缺文件时降级为
    空集：perf 段据此不写该客户端，行为退回上一版（宁少授权，不冒 secilc 失败 =>
    InitFatalReboot 的险）。
    """
    global _sysext_cache
    if _sysext_cache is not None:
        return _sysext_cache
    _sysext_cache = set()
    cil = os.path.join(OFFICIAL_SYSEXT_XDIR, "system_ext_sepolicy.cil")
    if os.path.isfile(OFFICIAL_SYSEXT_IMG):
        shutil.rmtree(OFFICIAL_SYSEXT_XDIR, ignore_errors=True)
        os.makedirs(os.path.dirname(OFFICIAL_SYSEXT_XDIR), exist_ok=True)
        res = subprocess.run(
            [erofs_tool("fsck.erofs"), f"--extract={OFFICIAL_SYSEXT_XDIR}",
             "--path=/etc/selinux", OFFICIAL_SYSEXT_IMG],
            capture_output=True, text=True)
        if res.returncode == 0 and os.path.isfile(cil):
            with open(cil, encoding="utf-8", errors="replace") as f:
                _sysext_cache = _cil_names(f.read())
        else:
            print(f"[..] system_ext 策略提取失败（perf 段不引 system_ext 域）："
                  f"{res.stdout.strip()} {res.stderr.strip()}")
    return _sysext_cache


# 类型名可含连字符（gold 属性类型，如 vendor_ctl_port-bridge_prop）与大写字母
# （本盘官方 vendor 属性类型 vendor_nothing_newAAC_prop）：字符集漏掉 '-' / 'A-Z'
# 会把片段内自声明的类型当未知名字误报（实测 2026-10-10 首次构建即报此类型未知）。
_TYPE_DECL_RE = re.compile(r"^\(type\s+([A-Za-z0-9_\-. ]+?)\s*\)\s*$", re.M)


def _declared_types(cil: str) -> set:
    """收集 CIL 片段里 (type X) 自声明的类型名。

    这些类型随本片段一起写进 vendor_sepolicy.cil，编译期必定存在，故 validate_cil
    对它们跳过「名字全集」存在性检查（此前用硬编码白名单，新增类型会误报）。
    """
    names = set()
    for group in _TYPE_DECL_RE.findall(cil):
        names.update(group.split())
    return names


def sanitize_vendor_cil(text: str, table: dict) -> str:
    """按官方 plat 类表清理 vendor CIL（运行态平台兼容，构建期一次完成）。

    背景：运行态 init 内嵌 secilc 以**官方 plat 策略**声明 class/权限，而 lineage
    vendor 策略是随更新内核线（6.12.81-line）生成的，含官方类表没有的权限；任何一条
    解析失败即 secilc 报 "Failed to resolve permission" → InitFatalReboot。本盘内核
    6.12.23 classmap 同样没有这些权限（nlmsg 于 netlink_route_socket、firmware_load
    于 system），内核永远不会对其判定，剔除/裁剪语义等价。

    规则：
      * allow 家族（allow/dontaudit/auditallow/neverallow）：class 不在官方类表 →
        整条删除；部分权限不在（`-perm` 否定式不参与判定）→ 仅剔除缺失权限，全缺则
        整条删除；
      * allowx：op 不在内核 xperm 家族（KERNEL_XPERM_OPS，nlmsg 即此类）或 class
        未知 → 整条删除；
      * 无法整行解析的语句原样保留（不做过删）。

    输出与输入行序一致，未改动行逐字节保留。
    """
    out, dropped, pruned = [], [], []
    for ln in text.splitlines(keepends=True):
        s = ln.strip()
        if s.startswith(("(allow ", "(dontaudit ", "(auditallow ", "(neverallow ")):
            m = _RE_ALLOW_FAMILY.fullmatch(s)
            if m is None:
                out.append(ln)
                continue
            kind, src, tgt, cls, perm_s = m.groups()
            perms = perm_s.split()
            if cls not in table:
                dropped.append(f"{kind} {src} {tgt} (class {cls} 未知)")
                continue
            miss = [p for p in perms if not p.startswith("-") and p not in table[cls]]
            if not miss:
                out.append(ln)
                continue
            keep = [p for p in perms if p.startswith("-") or p in table[cls]]
            if not keep:
                dropped.append(f"{kind} {src} {tgt} ({cls}: {' '.join(miss)})")
                continue
            pruned.append(f"{kind} {src} {tgt} ({cls}: {' '.join(miss)})")
            eol = ln[len(ln.rstrip("\r\n")):]
            out.append(f"({kind} {src} {tgt} ({cls} ({' '.join(keep)})))" + eol)
            continue
        if s.startswith("(allowx "):
            m = _RE_ALLOWX_FULL.fullmatch(s)
            if m is None:
                out.append(ln)
                continue
            src, tgt, op, cls, _vals = m.groups()
            if op not in KERNEL_XPERM_OPS or cls not in table:
                dropped.append(f"allowx {src} {tgt} (op {op} class {cls})")
                continue
            out.append(ln)
            continue
        out.append(ln)
    for d in dropped:
        print(f"[..] 平台兼容剔除: {d}")
    for p in pruned:
        print(f"[..] 平台兼容裁剪: {p}")
    if dropped or pruned:
        print(f"[ok] vendor CIL 平台兼容清理：剔除 {len(dropped)} 条、"
              f"裁剪 {len(pruned)} 条（官方 plat 类表）")
    else:
        print("[ok] vendor CIL 平台兼容检查通过（无语句需清理）")
    return "".join(out)


def validate_cil(cil: str, name_universe: set) -> None:
    """逐条 allow 校验: class/perm 合法性（按平台声明）、源/目标名存在性
    （name_universe = plat cil + plat_pub_versioned.cil + vendor 基座 cil 的
    声明全集，精确名匹配）与冗余（完全重复语句、同一 (src,tgt,class) 拆分授权）。
    违规即报错退出。"""
    valid = plat_class_perms()
    names = plat_name_set() | set(name_universe)
    # 本段自有类型/标签（片段内 (type X) 声明，如 pioneer/gold 属性类型）
    # + CIL 关键字目标，不做存在性检查
    skip = {"self"} | _declared_types(cil)
    seen_stmt, seen_pair, errs, n = set(), set(), [], 0
    for ln in cil.splitlines():
        s = ln.strip()
        if not s.startswith("(allow "):
            continue
        n += 1
        m = _RE_ALLOW.fullmatch(s)
        if not m:
            errs.append(f"语句格式不可解析: {s}")
            continue
        src, tgt, cls, perms = m[1], m[2], m[3], m[4].split()
        if s in seen_stmt:
            errs.append(f"重复语句: {s}")
            continue
        seen_stmt.add(s)
        if cls not in valid:
            errs.append(f"未知 class {cls}: {s}")
            continue
        bad = sorted(set(perms) - valid[cls])
        if bad:
            errs.append(f"非法权限 {cls}:{bad} -> {tgt}")
        for name in (src, tgt):
            if name in skip or name in names:
                continue
            errs.append(f"未知 type/attribute {name}: {s}")
        key = (src, tgt, cls)
        if key in seen_pair:
            errs.append(f"拆分冗余授权 ({src} {tgt} {cls}): {s}")
        seen_pair.add(key)
    if errs:
        for e in errs:
            print(f"[err] sepolicy 校验: {e}")
        sys.exit(1)
    print(f"[ok] CIL 校验通过（{n} 条 allow，平台 {len(valid)} class、"
          f"{len(names)} type/attr 声明）")


def coredomain_vendor_prop_read_cil(vers: str) -> str:
    """coredomain 服务直读 vendor_default_prop 属性区（引导修复段）。

    note13 实测：lmkd/keystore2 读取 /dev/__properties__/u:object_r:vendor_default_prop:s0
    被拒（avc denied { read } tclass=file）后 20ms 内 SIGABRT —— keystore2 起不来导致
    vold 等 IKeystoreService 卡死、boot 永不完成。授权直读该属性区文件即可让服务正常
    启动。风险：若 plat 对 coredomain→vendor_default_prop 有 neverallow，本段会导致
    init 编译策略失败（引导极早失败，可立即发现回退）。
    """
    return (
        f"{VENDOR_PROP_READ_MARKER}\n"
        + _allow(f"lmkd_{vers}", f"vendor_default_prop_{vers}", "file",
                 ["read", "getattr", "map", "open"])
        + _allow(f"keystore_{vers}", f"vendor_default_prop_{vers}", "file",
                 ["read", "getattr", "map", "open"])
        # 诊断：crash_dump 默认被拒 ptrace/dac_override（note13 实测 avc denied），
        # tombstone 永远写不出来，崩溃根因不可见。授权后 abort 栈可落盘定位。
        + _allow(f"crash_dump_{vers}", "domain", "process", ["ptrace"])
        + _allow(f"crash_dump_{vers}", "self", "capability", ["dac_override"])
    )


def frp_pdb_cil(vers: str) -> str:
    """FRP 承载块设备段：system_server（pdb）读写 misc_block_device。

    本盘没有 frp 分区，pdb 经 ro.frp.pst 打开的是软链到 misc(vda3) 的节点
    （类型 misc_block_device），平台策略只允许 system_server 访问 frp_block_device。
    """
    return (
        f"{FRP_MARKER}\n"
        + _allow(f"system_server_{vers}", "misc_block_device", "blk_file",
                 list(FRP_BLK_PERMS))
    )



def wifi_hal_cil(vers: str) -> str:
    """wifi HAL 段：hal_wifi_default 注册 IWifi/default + 档位 sysfs 标签 + 宿主通道。

    lineage 基座没有 IWifi HAL（vendor VINTF 无 android.hardware.wifi-service.xml），
    但 plat 侧已按 hal_wifi 属性备齐能力，本段只需补：
      * service_contexts 的 hal_wifi_service 映射（见 WIFI_VSC_LINES），否则
        AServiceManager_addService 无上下文而失败；
      * 内核模块参数 scan_profile 的独立 sysfs 标签（genfscon）+ 属性挂载：
        plat 已有 `hal_wifi -> sysfs_type (file 只读)`，故挂 sysfs_type 即得读权限；
        挂 file_type 则让 console 域 pioneer 的宽授权覆盖该节点，可写入扫描档位；
        另补 hal_wifi_default 的 write（rc 里 chmod 0666 放开 DAC）与 init 的
        setattr（chmod 本身）；
      * sys_module capability —— rc 声明了 NET_ADMIN NET_RAW SYS_MODULE，其中
        net_admin/net_raw 由 plat 的 `hal_wifi self capability` 覆盖，sys_module
        在本盘基座缺失（cuttlefish 基座的 hal_wifi_default.te 有这一条）；
      * 宿主 wifi 控制面 /dev/hvc9 的独立设备标签与 HAL 读写授权（标签行由
        patch_commands 追加到 vendor_file_contexts）；
      * 数据面 /dev/hvc10 授权给 kernel（内核 mac80211_hwsim 直桥独占）。

    服务注册本身（hal_wifi_server -> hal_wifi_service add/find）与
    hal_wifi -> hal_wifi_client binder transfer/call 都由 plat 规则覆盖，无需重复声明。
    """
    return (
        f"{WIFI_MARKER}\n"
        + _allow("hal_wifi_default", "self", "capability", ["sys_module"])
        + f"(type {WIFI_SCAN_PARAM})\n"
        + f"(roletype object_r {WIFI_SCAN_PARAM})\n"
        + f"(typeattributeset file_type ({WIFI_SCAN_PARAM}))\n"
        + f"(typeattributeset sysfs_type ({WIFI_SCAN_PARAM}))\n"
        + f'(genfscon sysfs "{WIFI_SCAN_PATH}" '
          f"(u object_r {WIFI_SCAN_PARAM} ((s0) (s0))))\n"
        + _allow("hal_wifi_default", WIFI_SCAN_PARAM, "file",
                 ["getattr", "open", "write"])
        + _allow(f"init_{vers}", WIFI_SCAN_PARAM, "file",
                 ["getattr", "setattr"])
        + f"(type {WIFI_CHANNEL_TYPE})\n"
        + f"(roletype object_r {WIFI_CHANNEL_TYPE})\n"
        + f"(typeattributeset dev_type ({WIFI_CHANNEL_TYPE}))\n"
        + _allow("hal_wifi_default", WIFI_CHANNEL_TYPE, "chr_file",
                 "getattr ioctl open read write".split())
        + f"(type {WIFI_DATA_TYPE})\n"
        + f"(roletype object_r {WIFI_DATA_TYPE})\n"
        + f"(typeattributeset dev_type ({WIFI_DATA_TYPE}))\n"
        + _allow("kernel", WIFI_DATA_TYPE, "chr_file",
                 "getattr ioctl open read write".split())
    )


def perf_policy_cil(vers: str, names=None) -> str:
    """perf2 HAL 段：服务启动 / 注册 + 官方客户端清单的查找与 binder 通道。

    平台 neverallow 规定 `(and (domain) (not (vendor_hal_perf_client
    vendor_hal_perf_server)))` 不得 add/find vendor_hal_perf2_service，所以整段照官方
    vendor 策略的写法来：本 HAL 域并入 vendor_hal_perf_server、调用方并入
    vendor_hal_perf_client，再由属性统一给 find / binder / fd（官方同属性即如此授权，
    逐条见 out/tmp/gold-vendor/etc/selinux/vendor_sepolicy.cil 的
    「allow vendor_hal_perf_client …」三句）。

    客户端清单 = 官方 vendor_hal_perf_client 成员里本盘能解析到名字的那些：
    plat 侧域走 plat_pub_versioned.cil 的版本化影子（<dom>_<vers>），
    vendor_perfservice 是官方 system_ext 声明的裸名（它才是 app 侧
    IPerfManager.setClientBinder 的对端 —— 缺它的 find 授权时该调用永不返回，
    实测 2026-10-11：全体 app 卡在 handleBindApplication，ANR
    「failed to complete startup」）。名字解析不到就不写（代价是 secilc 失败 =>
    InitFatalReboot），故按 names 过滤。
    """
    avail = plat_name_set() | sys_ext_name_set() | set(names or ())
    clients = [f"{d}_{vers}" for d in PERF_CLIENT_DOMAINS
               if f"{d}_{vers}" in avail]
    if "vendor_perfservice" in avail:
        clients.append("vendor_perfservice")
    if not clients:
        print("[..] perf 段无可用客户端域，只声明类型/启动链")
    return (
        f"{PERF_MARKER}\n"
        # 类型与域
        "(type vendor_hal_perf_default)\n"
        "(type vendor_hal_perf_default_exec)\n"
        "(type vendor_hal_perf2_service)\n"
        "(typeattribute vendor_hal_perf_server)\n"
        "(typeattribute vendor_hal_perf_client)\n"
        "(roletype object_r vendor_hal_perf_default)\n"
        "(roletype object_r vendor_hal_perf_default_exec)\n"
        "(roletype object_r vendor_hal_perf2_service)\n"
        # neverallow 豁免集：本 HAL 域=server，官方客户端清单=client
        "(typeattributeset domain (vendor_hal_perf_default))\n"
        "(typeattributeset vendor_hal_perf_server (vendor_hal_perf_default))\n"
        + (f"(typeattributeset vendor_hal_perf_client"
           f" ({' '.join(clients)}))\n" if clients else "")
        # init 启动该服务
        + f"(typetransition init_{vers} vendor_hal_perf_default_exec process"
        " vendor_hal_perf_default)\n"
        + _allow(f"init_{vers}", "vendor_hal_perf_default", "process",
                 ["transition", "siginh", "rlimitinh", "noatsecure"])
        + _allow(f"init_{vers}", "vendor_hal_perf_default_exec", "file",
                 ["read", "getattr", "map", "execute", "open"])
        + _allow("vendor_hal_perf_default", "vendor_hal_perf_default_exec",
                 "file", ["read", "getattr", "map", "execute", "open",
                          "entrypoint"])
        # 注册 / 查找（neverallow 豁免已就位）
        + _allow("vendor_hal_perf_default", "vendor_hal_perf2_service",
                 "service_manager", ["add", "find"])
        + (_allow("vendor_hal_perf_client", "vendor_hal_perf2_service",
                  "service_manager", ["find"]) if clients else "")
        # 客户端 <-> HAL 双向 binder（HAL 会回调客户端 binder，故两向都要）
        + (_allow("vendor_hal_perf_client", "vendor_hal_perf_server", "binder",
                  ["call", "transfer"])
           + _allow("vendor_hal_perf_client", "vendor_hal_perf_server", "fd",
                    ["use"])
           + _allow("vendor_hal_perf_server", "vendor_hal_perf_client", "binder",
                    ["call", "transfer"])
           + _allow("vendor_hal_perf_server", "vendor_hal_perf_client", "fd",
                    ["use"]) if clients else "")
        # 与 servicemanager 通 binder（注册路径）
        + _allow("vendor_hal_perf_default", f"servicemanager_{vers}", "binder",
                 ["call", "transfer"])
        + _allow(f"servicemanager_{vers}", "vendor_hal_perf_default", "binder",
                 ["transfer"])
        + _allow("vendor_hal_perf_default", f"servicemanager_{vers}", "fd",
                 ["use"])
    )


def hal_cil_chain(merged: str, vers: str, names=None) -> str:
    """按固定顺序补齐缺失的 CDH CIL 段
    （pioneer + coredomain 属性区直读 + 官方 gold vendor 属性类型段 + FRP + wifi + perf）。

    各段都基于工作镜像里的「原始」vendor_sepolicy.cil 拼接（debugfs 命令此刻尚未
    执行），而命令序列里只有最后一次 write 生效 —— 所以最后调用的那个函数必须把所有
    前序段一并写出，否则会把它们覆盖丢失。新增 HAL 段时只需在这里加一行（连同对应的
    `<hal>_cil(vers)` 片段函数）。
    names = 本策略已声明的类型/属性名全集（供 gold prop types 段过滤源域不存在的授权、
    perf 段过滤客户端域名；perf 段另自行并入官方 plat / system_ext 声明名）。
    """
    cil = ""
    if MARKER not in merged:
        cil += pioneer_cil(vers)
    if VENDOR_PROP_READ_MARKER not in merged:
        cil += coredomain_vendor_prop_read_cil(vers)
    if gold_props.MARKER not in merged:
        cil += gold_props.prop_types_cil(vers, names or set())
    if FRP_MARKER not in merged:
        cil += frp_pdb_cil(vers)
    if WIFI_MARKER not in merged:
        cil += wifi_hal_cil(vers)
    if PERF_MARKER not in merged:
        cil += perf_policy_cil(vers, names)
    return cil


def patch_commands(work_img, tmp_dir=None) -> list:
    """补齐 vendor_sepolicy.cil 的各 CDH 段，返回 debugfs 命令清单。

    幂等：基座副本每次从 lineage vendor_a.img 重新拷贝，正常不会已含标记；
    若已含（重复构建同一 work.img）则跳过不重复追加。
    """
    vers = cat(work_img, VERS_FILE)
    if not vers:
        print(f"[err] 读不到 {VERS_FILE}")
        sys.exit(1)
    vers = vers.decode("ascii", "ignore").strip()

    orig = cat(work_img, POLICY_FILE)
    if orig is None:
        print(f"[err] 读不到 {POLICY_FILE}")
        sys.exit(1)
    orig_text = orig.decode("utf-8", "replace")
    # 运行态平台兼容清理（官方 plat 类表）：secilc 以官方 plat 解析权限，
    # lineage 策略里官方类表没有的权限语句必须先剔除，否则开机 InitFatalReboot。
    merged = sanitize_vendor_cil(orig_text, plat_class_perms())
    # 名字全集: vendor 基座自有类型（mediacodec 等，未版本化）
    #          + plat_pub_versioned.cil（plat 公共类型的版本化 typeattribute 影子）
    #          + 官方 plat（plat_name_set 内部缓存）
    name_universe = _cil_names(merged) | \
        _cil_names(_read_cil(work_img, PUB_VER_FILE))
    cil = hal_cil_chain(merged, vers, name_universe)
    tmp_dir = tmp_dir or os.path.join(TMP_DIR, "vendor")
    os.makedirs(tmp_dir, exist_ok=True)
    cmds = []
    if cil or merged != orig_text:
        validate_cil(cil, name_universe | sys_ext_name_set())
        tmp = os.path.join(tmp_dir, "vendor_sepolicy.cil")
        with open(tmp, "wb") as f:
            f.write(merged.encode("utf-8") + cil.encode("utf-8"))
        print(f"[ok] vendor_sepolicy.cil 追加 CDH 段（plat vers {vers}，"
              f"+{len(cil)} B）")
        cmds += [
            f"rm {POLICY_FILE}",
            f"write {tmp} {POLICY_FILE}",
            f"ea_set {POLICY_FILE} security.selinux {POLICY_LABEL}",
        ]
    else:
        print("[ok] vendor_sepolicy.cil 各段齐全，跳过")

    cmds += _contexts_append_cmds(work_img, VSC, VSC_LABEL,
                                  vsc_missing, tmp_dir)
    cmds += _contexts_append_cmds(work_img, VFC, VFC_LABEL,
                                  wifi_vfc_missing, tmp_dir)
    return cmds


def vsc_missing(vsc: bytes) -> list:
    """vendor_service_contexts 里尚缺的 CDH HAL 服务映射行（wifi + perf2）。"""
    return [line for line in (*WIFI_VSC_LINES, *PERF_VSC_LINES)
            if line.encode("utf-8") not in vsc]


def wifi_vfc_missing(vfc: bytes) -> list:
    """vendor_file_contexts 里尚缺的 wifi 设备字面行（hvc9 控制面 + hvc10 数据面）。"""
    missing = []
    if f"u:object_r:{WIFI_CHANNEL_TYPE}:s0".encode() not in vfc:
        missing += WIFI_FC_LINES
    if f"u:object_r:{WIFI_DATA_TYPE}:s0".encode() not in vfc:
        missing += WIFI_DATA_FC_LINES
    return missing


def _contexts_append_cmds(work_img, path, label, missing_fn, tmp_dir):
    """把缺失的上下文字段追加到 /etc/selinux 下的 contexts 文件（幂等）。

    与 vendor_sepolicy.cil 同机制：读的是工作镜像里的「原始」内容，debugfs 命令序列
    中只有最后一次 write 生效 —— 本盘每个 contexts 文件只有这一个写入者，故安全。
    """
    data = cat(work_img, path)
    if data is None:
        print(f"[err] 读不到 {path}")
        sys.exit(1)
    missing = missing_fn(data)
    if not missing:
        print(f"[ok] {path} 已含 CDH 映射，跳过")
        return []
    tmp = os.path.join(tmp_dir, os.path.basename(path) + "_cdh")
    with open(tmp, "wb") as f:
        f.write(data + ("\n" + "\n".join(missing) + "\n").encode("utf-8"))
    print(f"[ok] {path} 追加 {len(missing)} 行（CDH HAL）")
    return [f"rm {path}", f"write {tmp} {path}",
            f"ea_set {path} security.selinux {label}"]
