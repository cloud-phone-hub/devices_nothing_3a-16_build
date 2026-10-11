# Issue:官方 odm 加载冲突（odm_sepolicy.cil 引用 MTK 专属类型导致 init 崩溃）—— 解决方案

- 设备: Xiaomi Redmi Note 13 5G (note13-5G-15)
- 宿主: macOS (darwin, QEMU + hvf)
- 状态: **已修复并验证**(2026-09-14)

---

## 1. 问题描述

云机（官方 super.img 零改动 + lineage virtio vendor 经 vdc 挂 `/mi_ext` + 内核
getname_flags 重定向）启动到 init 编译 SELinux 策略时崩溃，`InitFatalReboot` 反复重启。

```
init: /system/bin/secilc: Failed to resolve allow statement at /odm/etc/selinux/odm_sepolicy.cil:1
init: /system/bin/secilc: Failed to resolve AST
init: /system/bin/secilc: Failed to compile cildb: -2
init: Unable to open SELinux policy
init: InitFatalReboot: signal 6
```

## 2. 根因

### 2.1 官方 super 没有独立 odm 分区

解包官方 `super.img`（liblp 分区表）验证：**不存在 `odm_a/b` 逻辑分区**，
只有 `odm_dlkm_a/b` 且为 0MB（AOSP 分区模型占位条目）。gold（MIUI/MTK）选择
"odm 内容合并进 vendor 分区"，而非独立 odm 分区。

### 2.2 系统内 `/odm` 是指向官方 vendor 的符号链接

官方 system_a 的 `/odm`（如 `/odm/etc`）是指向 `/vendor/odm/etc` 的符号链接。
因此 `/odm/...` 实际解析到**官方 vendor 内的 odm 子树**（厂商内容）。

### 2.3 冲突点：官方 odm_sepolicy.cil 引用 MTK 专属类型

官方 vendor `/odm/etc/selinux/odm_sepolicy.cil`（624B）内容：

```
(allow hal_dumpstate_impl thermal_data_file (dir (getattr search)))
(allow hal_dumpstate_impl teei_data_file (file (read getattr open)))
(allow hal_dumpstate_impl servicemanager_202404 (binder (call transfer)))
...
```

这些类型（`thermal_data_file`/`teei_data_file`/`hal_dumpstate_impl`
/`servicemanager_202404`）是 **MTK/厂商专属**，而我们实际加载的是 **lineage
virtio vendor 的 vendor_sepolicy.cil**（不含这些类型声明）。secilc 编译时解析
odm_sepolicy.cil 引用了不存在的类型 → `Failed to resolve allow statement`
→ 无法打开 SELinux 策略 → init 直接重启。

### 2.4 为什么不能直接"删掉"或"替换"odm

- `/odm` 在官方 super 中属于厂商内容，替换为 lineage odm 会破坏"官方身份"目标，
  且需内核重定向 `/vendor/odm` + patch 打包等，得不偿失（见 §4.2 评估）。
- odm_sepolicy.cil 只是"漏网"的一个文件，其余官方 odm 内容（build.prop、
  selinux contexts、sku 属性）保持厂商原样是符合目标的。

## 3. 证据

| # | 证据 | 结论 |
|---|---|---|
| 1 | 官方 super liblp 分区表：无 `odm_a/b`，`odm_dlkm_a/b` 为 0MB | 官方无独立 odm 分区 |
| 2 | 官方 system_a `/odm/etc` 为 symlink → `/vendor/odm/etc` | `/odm` 解析到官方 vendor 子树 |
| 3 | `qemu.log`：`cdh_redirect: /mi_ext/odm/etc/selinux/odm_sepolicy.cil` | 内核正确屏蔽了冲突文件 |
| 4 | 官方 vendor 的 odm_sepolicy.cil 含 `thermal_data_file`/`teei_data_file` 等 MTK 类型 | 与 lineage vendor 策略不兼容 |
| 5 | `secilc: Failed to resolve allow statement at /odm/etc/selinux/odm_sepolicy.cil:1` | init 编译策略崩溃的直接证据 |
| 6 | 剥离后 init 正常走到 SELinux 编译 → `Loading SELinux policy` → 继续引导 | 因果闭合 |

## 4. 解决方案

### 4.1 代码修改（内核白名单，屏蔽单个冲突文件）

在 `kernel/source/fs/namei.c` 的 `cdh_redirect_vendor_path` 白名单中增加一条
**精确文件**规则，把 `/odm/etc/selinux/odm_sepolicy.cil` 重定向到不存在的
`/mi_ext/odm/...` → 内核返回 ENOENT → init 跳过 odm 策略（等价 lineage 无 odm
策略的情形）。**只屏蔽这一个文件，其余官方 odm 内容保持原样。**

```c
{ "/odm/etc/selinux/odm_sepolicy.cil",
  sizeof("/odm/etc/selinux/odm_sepolicy.cil") - 1,        false },
```

重定向后，实际读取路径为 `/mi_ext/odm/etc/selinux/odm_sepolicy.cil`（不存在 →
open ENOENT），init 编译策略时不会引入 MTK 类型 → secilc 通过。

> 说明：因为是精确文件匹配（`false` = 非目录），只影响这一个文件；其它
> `/odm/*`（build.prop、selinux contexts、sku 属性、vintf fragment）仍走官方。

### 4.2 为什么"不"把 lineage odm 打进来

逐一考察后否掉两个相邻方案：

| 方案 | 结论 |
|---|---|
| 内核重定向 `/vendor/odm` 子树到 patch 的 lineage odm | 会遮蔽官方 odm 全部内容（NFC/se 权限、iron_gl/iron_p_gl rc、vintf fragment、sku 属性），破坏身份目标；且要重编内核 + patch 加子树，代价大收益低 |
| 在 vendor build.prop 里直接声明全局 `ro.product.cpu.abilist32=`（空）解决 64 位 only | 已用此方案解决 32 位问题（见 §5 遗留），无需再依赖 lineage odm 的 `ro.odm.product.cpu.abilist32=` |
| **仅屏蔽 odm_sepolicy.cil 这一个文件（本方案）** | 官方 odm 除该文件外全部保留，代价最小（白名单一行），身份目标不受损 |

## 5. 验证

1. `qemu.log`：`cdh_redirect: /mi_ext/odm/etc/selinux/odm_sepolicy.cil` 命中。
2. init 越过 secilc：`Compiling SELinux policy` → `Loading SELinux policy` → 无
   `Failed to resolve allow statement` 崩溃。
3. 系统继续引导（本 issue 之后 32 位自检/zygote/BatteryService 等后续问题，见各自 issue）。

## 6. 遗留 / 后续

1. **32 位 only 的"正规化"**：当前 64 位 only 口径是直接在 vendor build.prop
   里定义全局 `ro.product.cpu.abilist*`（abilist32 置空）实现的；lineage odm 的
   `/etc/build.prop` 里同样有 `ro.odm.product.cpu.abilist32=`（空），若未来想让
   odm/vendor 载荷更贴近 lineage，可把该 prop 一并纳入 vdc（本 issue 不涉及）。
2. **P0-1 挂载隐藏**：当前 `vdc /mi_ext` 挂载条目对 app 可见（真机无 vdc），
   后续在 `fs/namespace.c` 按路径前缀隐藏 `/mi_ext`，收敛挂载视角。