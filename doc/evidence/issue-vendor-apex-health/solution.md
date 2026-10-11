# Issue:/vendor/apex 未重定向 —— BatteryService 因缺 health HAL 崩溃 —— 解决方案

- 设备: Xiaomi Redmi Note 13 5G (note13-5G-15)
- 宿主: macOS (darwin, QEMU + hvf)
- 状态: **已解决并验证**（2026-09-14，非 APEX 普通服务路线落地，BatteryService 连续两轮 system_server 均通过）

---

## 1. 问题描述

32 位问题解决后，引导推进到 system_server，`BatteryService` 起不来 → FATAL 崩溃循环
（实测每 ~64s 一轮，永不到 `boot_completed`）：

```
E BatteryService: health: cannot register callback. (no supported health HAL service)
E System  : java.lang.RuntimeException: Failed to start service com.android.server.BatteryService: onStart threw an exception
    at com.android.server.BatteryService.registerHealthCallback(BatteryService.java:323)
```

早期一轮还带 `Caused by: java.util.NoSuchElementException: IHealth service instance default
isn't available. Perhaps no permission? at com.android.server.health.HealthServiceWrapperHidl.<init>`，
说明 MIUI 15 框架会同时探测 AIDL（`android.hardware.health.IHealth/default`，首选，v3/v2）与
HIDL（`@2.1/2.0`，兜底）两系 health HAL——但本机一个都没有。

## 2. 根因

guest 的 `/vendor/apex` 是**官方的**（只有 cas/widevine 两个 APEX，无 health）；lineage
virtio vendor 的 7 个 cuttlefish HAL APEX（含 `com.google.cf.health.apex`）在 patch.img
`/mi_ext/vendor/apex` 里，但内核重定向白名单不覆盖 `/vendor/apex`，apexd 扫不到 → 缺
health HAL → BatteryService 崩溃循环。

> 本质：不是"缺 health HAL"，而是路线选择问题——health HAL 不一定要放进 APEX /vendor/apex
> 才能被框架看到；框架只认 servicemanager 里注册的 `IHealth/default` 实例（binder 服务），
> 不关心它来自 APEX 还是普通 `/vendor/bin/hw` 服务。Cuttlefish 把 HAL 打 APEX 是其打包约定。

## 3. 证据

| # | 证据 | 结论 |
|---|---|---|
| 1 | qemu.log：`BatteryService: health: cannot register callback. (no supported health HAL service)` + `onStart threw an exception`，system_server 崩溃循环 | BatteryService 缺 health HAL |
| 2 | qemu.log：`apexd: Scanning /vendor/apex → Found cas / widevine` | apexd 只扫到官方 2 个 APEX |
| 3 | lineage unpacked `vendor/apex/com.google.cf.health.apex` payload 内含 AIDL v3 health 服务（`/bin/hw/android.hardware.health-service.cuttlefish`，377112 B，仅依赖 libbinder_ndk/liblog/libc/libm/libdl） | cuttlefish 服务可独立于 APEX 运行 |
| 4 | 官方 vendor `/vendor/apex` 仅 cas/widevine | 官方 APEX 无 health |

## 4. 解决方案（实际落地：非 APEX 普通 vendor 服务）

### 4.1 选型对比（原文案 vs 实际方案）

| 方案 | 做法 | 评价 |
|---|---|---|
| 原方案 A：重定向 `/vendor/apex` → lineage 7 个 HAL APEX | 内核白名单加一行 → 重编内核 → 重打 boot | 引 APEX 签名信任实测风险（test-key on MIUI apexd）；`/proc/mounts` 暴露 `/apex/com.google.cf.*` 等 7 个挂载；必须重编内核 |
| 方案 B：移植官方 health-service.example 二进制 + 3 库 + rc + manifest | 官方 vendor 搬运到 patch.img | 依赖闭包大；AOSP generic 版读 `/sys/class/power_supply`（virtio 无电池），电量行为不可控 |
| **实际方案（选定）：抽取 cuttlefish 服务做普通服务化** | 从 cf.health APEX payload 抽二进制，以 `/vendor/bin/hw/` + rc + vintf fragment 注入 | 零内核改动、无 APEX 签名信任问题、无 `/apex` 挂载暴露；服务行为与 cuttlefish 官方一致 |

### 4.2 落地实现

源码子项目 `build/vendor/source/android.hardware.health/build.py`（被
`source_build.run_source_builds` 自动调用，位于 build-vendor.py 注入之前）：

- **输入**：`lineage-rom/lineage-22.0/unpacked/vendor/apex/com.google.cf.health.apex` 的
  `apex_payload.img`（zip 内 ext4），debugfs 提取三件：
  - `/bin/hw/android.hardware.health-service.cuttlefish`（AIDL v3，`IHealth/default`）
  - `/etc/android.hardware.health-service.cuttlefish.rc`
  - `/etc/vintf/android.hardware.health-service.cuttlefish.xml`
- **改写**：rc 执行路径 `/apex/com.google.cf.health/bin/hw/...` → `/vendor/bin/hw/...`
- **落盘**（相对路径即镜像内 `/vendor` 路径）：
  - `out/dist/vendor/bin/hw/android.hardware.health-service.cuttlefish`（0755）
  - `out/dist/vendor/etc/init/android.hardware.health-service.cuttlefish.rc`
  - `out/dist/vendor/etc/vintf/manifest/android.hardware.health-service.cuttlefish.xml`
- **标签**：幂等登记 baselines `vendor.selinux-labels`（inject.py 对 out/dist/vendor 全量
  要求标签）：
  - `bin/hw/...cuttlefish` → `u:object_r:hal_health_default_exec:s0`
  - `etc/init/...rc`、`etc/vintf/manifest/...xml` → `u:object_r:vendor_configs_file:s0`
  （与 lineage vendor_a.img 同路径实测 xattr 一致；`hal_health_default`/`_exec` 域与
  init transition 规则 lineage vendor_sepolicy.cil 已内置）
- **流水线**：`build-vendor.py`(source_build→inject) → `build-patch.py` → `pack-roms.py` 把
  `patch.img` 落到最终产物目录 `out/device/note13-5g/15/os3.0.9.0.vnqcnxm/`。只需重跑
  `build/main.py --from vendor`（vendor + patch 两段够了，patch.img 自动就位）。

**为什么零内核改动**：落盘三条路径全部命中内核 `getname_flags` 既有白名单目录规则
`/vendor/bin`、`/vendor/etc`（fs/namei.c），重定向到 `/mi_ext/vendor/...` 即读到
patch.img 内容；init（rc）、libvintf（manifest、system_server 读 VINTF）、Binder
（服务进程注册 servicemanager）全部走重定向可达。不需要为 `/vendor/apex` 新增规则。

### 4.3 为什么不用 HIDL `health@2.0`

框架首选 AIDL `IHealth`（v3/v2），HIDL 只是兜底。cf.health 与 lineage 官方即为 AIDL v3
（`<hal format="aidl"><name>android.hardware.health</name><version>3</version>
<fqname>IHealth/default</fqname></hal>`）；直接命中首选路径，工作量也最小。

## 5. 验证（实测 qemu.log）

1. `init: starting service 'vendor.health-cuttlefish'...` / `started service ... has pid 443`
   —— 服务由 init 拉起，全程仅启动一次（无崩溃循环）。
2. `android.hardware.health-service.cuttlefish: default: Hal init done` —— 服务初始化成功。
3. `servicemanager: Caller(pid=662,uid=1000,sid=u:r:system_server:s0) Found
   android.hardware.health.IHealth/default in device VINTF manifest` —— 我们注入的 vintf
   fragment 经重定向被 system_server 读到，实例已注册。
4. `BatteryService: health: Waited 2ms and received the update.` —— 电池数据回调正常。
5. `OnBootPhase_200_com.android.server.BatteryService` —— BatteryService 进入 boot phase
   200；连续两轮 system_server（662 与重启后的 1599）均通过。
6. 不再出现 `no supported health HAL service` / `BatteryService onStart threw an exception`。
7. `/vendor/apex` 仍为官方内容、无新增 `/apex/...` 挂载（不启用任何 APEX）→ 挂载视角优于
   原方案 A。

## 6. 遗留 / 后续

1. **boot 尚未到 boot_completed**（battery 已不是阻塞点）：system_server 在 battery 通过后
   有一次重启（662→1599）；当前可见 `keystore2: await_boot_completed Pending ... Overdue`、
   `ZygoteProcess: Got error connecting to zygote, retrying`、MIUI 蓝牙
   `BluetoothOppLauncherActivity ... does not exist` 等新观察点，需另行推进（不属于本 issue）。
2. **battery 状态细节**：cf.health 读 `/sys/class/power_supply`，virtio 云机无真实电池，
   电量/充电状态表现待观测；后续可评估改源码子项目注入固定虚拟电量。
3. **其余 lineage HAL**（power/sensors/audio/boot.virt 等如其后暴露缺失）可按同一
   "普通服务化"手法从 lineage 内容抽取，或再评估 APEX 路线。
4. **P0-1 挂载隐藏**仍可选（`/mi_ext` vdc 挂载条目）。

## 7. 迁移记录（2026-09-28）：改为源码移植 + 真机口径对齐

原方案（从 lineage `com.google.cf.health.apex` 提取 cuttlefish 二进制、服务
`vendor.health-cuttlefish`）改为**源码移植**，形态与真机（gold）完全一致：

- 模块 `build/vendor/source/android.hardware.health/`：
  - `aidl/aosp/android/hardware/health/`：AOSP **v3 冻结** AIDL 快照（12 个 .aidl +
    `.hash=3bab6273a5491102b29c9d7a1f0efa749533f46d`，与真机 vintf 声明的 v3 一致）；
  - `src/cdh_note13_health_service.cpp`：自研 `IHealth` v3 实现（含
    registerCallback/unregisterCallback/update、getHealthInfo 等全接口），电池数据由
    顶部 `kProfile` 常量模拟（**按真机 dumpsys 实测口径固化**）；
  - `vendor/etc/{init,vintf}`：与真机同名的 rc/vintf（服务 `vendor.health-default`、
    fqname `IHealth/default` v3）；
  - `build.py`（本机 NDK 工具链编译 + 旧 APEX 产物清理 + baselines 标签迁移）/`deps.py`（AIDL）。
- 产物：`/vendor/bin/hw/android.hardware.health-service.example`（与真机同路径同名），
  旧 `android.hardware.health-service.cuttlefish` 三件套构建时删除（避免双注册同实例）。
- 说明：真机 rc 里的 charger 模式服务 `vendor.charger` 不启用（云机无充电模式）；
  `getStorageInfo`/`getDiskStats` 返回空数组（真机由 eMMC 驱动填充）。

**对齐验证（2026-09-28 00:17，dumpsys battery 逐字段）**：

| 字段 | 真机 | 云机 | | 字段 | 真机 | 云机 |
|---|---|---|---|---|---|---|
| AC powered | true | true | | level | 100 | 100 |
| USB powered | true | true | | scale | 100 | 100 |
| Wireless/Dock | false/false | false/false | | voltage | 4389 | 4389 |
| Max charging current | 0 | 0 | | temperature | 290 | 290 |
| Max charging voltage | 0 | 0 | | technology | Li-poly | Li-poly |
| Charge counter | 4967 | 4967 | | Charging state | 0 | 0 |
| status / health | 5 / 2 | 5 / 2 | | Charging policy | 0 | 0 |
| present | true | true | | | | |

- `service list`：`android.hardware.health.IHealth/default` 在册；`init.svc.vendor.health-default=running`；
  旧 cuttlefish 产物残留 0；`no supported health HAL service` 计数 0；boot 20s 达成。
- 改口径方式：编辑 `kProfile` 常量后重建（构建期固化）；临时改框架上报值可用
  `dumpsys battery set level|status ...`（不改 HAL）。