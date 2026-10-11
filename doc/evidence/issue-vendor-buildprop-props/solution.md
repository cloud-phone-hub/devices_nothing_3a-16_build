# Issue: 官方 vendor build.prop 属性启用（必排除属性 + 必锁值属性）—— 解决方案

- 设备: Xiaomi Redmi Note 13 5G (note13-5G-15)
- 宿主: macOS (darwin, QEMU + hvf)
- 状态: **已定位并修复**（2026-09-27）：官方两件（`vendor_a/build.prop`、`vendor_a/build_gold_cn.prop`）
  去重共 663 个属性全量二分完成：**3 个属性必须排除**（否则不能正常启动）、**2 个属性必须锁环境值**；
  其余全量启用后设备可正常启动且框架 12 分钟稳定（无重启、无新 ANR）。

---

## 1. 固证：属性清单

### 1.1 会导致不能正常启动的属性（必须排除，3 个）

| # | 属性 | 官方值（`vendor_a/build.prop`） | 现象 | 处置 |
|---|---|---|---|---|
| 1 | `ro.frp.pst` | `/dev/block/by-name/frp`（L250） | 启动卡住（PDB 超时，无法进入系统） | 不写入 sets |
| 2 | `ro.hwui.use_vulkan` | `true`（L50） | libhwui abort（HWUI 强制 Vulkan 初始化失败） | 不写入 sets |
| 3 | `ro.vendor.product.cpu.abilist32` | `armeabi-v7a,armeabi`（L12） | 派生全局 abilist32 非空 → framework 判定支持 32 位 → 硬连 `zygote_secondary`（不存在）→ `android.display` FATAL、system_server 周期自杀重启 | 不写入 sets（空值口径，详见 issue-zygote-secondary-missing） |

### 1.2 必须锁环境值的属性（2 个）

| # | 属性 | 官方值 | 本环境值 | 原因 |
|---|---|---|---|---|
| 1 | `ro.vendor.product.cpu.abilist` | `arm64-v8a,armeabi-v7a,armeabi`（L11） | `arm64-v8a` | 纯 64 位口径；否则派生全局 abilist32 非空（同 1.1#3） |
| 2 | `persist.system.powerhal.applist_enable` | `1`（L729） | `0` | **本次新发现**：applist 服务（`vendor.mediatek.hardware.mtkpower_applist.IMtkpower_applist/default`）在本环境不存在；值=1 时每次 app 死亡回调都会持有 AMS 锁死等该服务 → 全系统卡死 → 看门狗每 ~2.5 分钟杀 system_server 重启（表现为"一直重启"）。根因链见 §2 |

> 另：`ro.vendor.product.cpu.abilist64` 与官方一致固定 `arm64-v8a`，已显式写入 sets 固化口径。

**落位与固化**：
- `build/baselines/gold_images_OS3.0.9.0.VNQCNXM_20260514.0000.00_15.0_cn_8fbf4887e3.json`
  → `vendor.build-prop-sets`（698 条 = 35 原有 + 663 官方全量口径）；`vendor.build-prop-unsets` 为空。
- 二分/复跑工具 `props_bisect.py` 的 `KNOWN_BAD`（3 键）与 `VALUE_FIXUP`（abilist / abilist32 / applist_enable=0）
  已同步固化，避免复跑时重新打开。

## 2. 根因链（applist → 框架重启循环，ANR 栈实锤）

```
# system_server 主线程：等 AMS 锁（被 binder:24904_12 持有）
"main" prio=5 tid=1 Blocked
  at ActivityManagerService$LocalService.onForegroundServiceNotificationUpdate(ActivityManagerService.java:20849)
  - waiting to lock <0x09309800> (a com.android.server.am.ActivityManagerService) held by thread 327

# 持锁线程 327：app 死亡 → MTK powerhal applist 查询死等
"binder:24904_12" prio=5 tid=327 Native
  native: #00 .../libc.so (nanosleep+8)
  native: #03 .../libbinder.so (android::ServiceManagerShim::getService const+452)
  native: #04 .../libpowerhalwrap.so (getMtkPowerAPPList+108)
  native: #05 .../libpowerhalwrap.so (applistNotifyForegroundApp+144)
  native: #07 .../libpowerhalwrap_jni.so (...nativeNotifyAppState+200)
  at com.mediatek.powerhalwrapper.PowerHalWrapper.NotifyAppCrash(PowerHalWrapper.java:430)
  at com.mediatek.server.am.AmsExtImpl.onNotifyAppCrash(AmsExtImpl.java:692)
  at com.android.server.am.ActivityManagerService.handleAppDiedLocked(ActivityManagerService.java:3803)
  - locked <0x09309800> (a com.android.server.am.ActivityManagerService)
```

```
$ adb shell service check vendor.mediatek.hardware.mtkpower_applist.IMtkpower_applist/default
Service vendor.mediatek.hardware.mtkpower_applist.IMtkpower_applist/default: not found
```

触发源：`com.android.camera` 每 ~2 秒崩一次（`open libmialgoengine.so failed`）
→ 持续产生 app 死亡事件 → 与 applist=1 组合成 system_server 重启循环。
（相机崩溃本身是 vendor 库打包缺口，见 §5，独立于属性问题。）

## 3. 方法与判据（重要教训）

- **失效判据（勿再使用）**：仅查一次 `sys.boot_completed=1`。重启循环在启动完成
  3~5 分钟后才开始，检查在循环开始前就返回 OK → 循环被判成"正常启动"。
- **有效判据（本 issue 采用）**：启动完成后**稳定观察 ≥10~12 分钟**，要求
  `zygote64` / `system_server` PID 不变、无新 `/data/anr/*`、`boot_completed` 恒为 1。
- **二分流程**：每批 50 键写入 baselines `vendor.build-prop-sets` → 重建 vendor/patch
  → 重启测试实例 → 判启动+稳定观察；失败批次对半递归定位到单键后剔除并继续。

## 4. 验证结果

| # | 验证点 | 结果 |
|---|---|---|
| 1 | 属性覆盖 | 663/663（353 + 300 为记录批次，在途 10 键由后续启动隐式覆盖） |
| 2 | 设备侧修复验证 | `setprop persist.system.powerhal.applist_enable 0` + 重启：12 分钟观察 `zygote64`(pid 475)/`system_server`(pid 741) PID 恒定、无新 ANR（修复前每 ~2.5 分钟重启一轮） |
| 3 | 构建侧固化 + 重建复验 | **通过**：baselines 键值=0、工具 `VALUE_FIXUP` 锁 0；重建后 `/vendor/build.prop:460=0`，12 分钟观察 `zygote64`(pid 476)/`system_server`(pid 743) PID 恒定、无新 ANR |

## 5. 遗留 / 下一步

1. `com.android.camera` 崩溃循环（缺 `libmialgoengine.so`）：gold vendor 有该库
   （`gold-vendor/lib64` 1691 个库），设备 `/vendor/lib64` 仅 121 个 → 独立打包缺口，
   建议另开 issue（它是 app 死亡事件源，但已不再拖垮系统稳定性）。
2. 早期"每批 10 键 + 单点判启动"的批次结论已由本次全量重跑覆盖；后续复跑沿用 §3 稳定判据。
3. 二分工具当前位于 `/tmp/props_bisect.py`（临时目录），如需长期保留建议归档进 `build/scripts/`。