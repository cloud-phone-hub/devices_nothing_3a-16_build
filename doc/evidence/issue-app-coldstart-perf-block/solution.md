# Issue: 全体 app 冷启动 9~25s + 开机 ANR 风暴（QTI perf2 链路缺失）—— 解决方案

- 设备: Nothing Phone (3a)（Asteroids / volcano / Android 16，固件 `Asteroids_B4.1-260810-1153`）
- 宿主: macOS (darwin, QEMU + hvf)，实例 `out/core/daemon/runtime/3a-16/10`
- 状态: **已解决**（2026-10-11，任务 #25）。两步缺一不可：① 收编官方 perf2 AIDL HAL；
  ② 把 `vendor_perfservice` 等官方 perf 客户端域补进 vendor sepolicy 的
  `vendor_hal_perf_client`。修后整轮开机 `ANR in ` / `failed to complete startup` 计数 **0**，
  Settings 冷启动 9~25s → **1.06s**。

---

## 1. 现象与取证（修复前）

用户报「图标文字很小，还不能操作」。密度是独立问题（`ro.sf.lcd_density=160`，另案已修 388），
「不能操作」的实测根因是**每个 app 的主线程在 `handleBindApplication` 里被同步 binder 钉住**：

```
# dumpsys dropbox --print（user 版无 root，/data/anr 读不到，dropbox 是唯一完整栈通道）
Subject: Process ProcessRecord{...com.nothing.launcher/u0a234} failed to complete startup
----- pid 6672 -----
"main" prio=5 tid=1 Native
  native: #02 libbinder.so (android::IPCThreadState::waitForResponse)
  at android.os.BinderProxy.transact(BinderProxy.java:599)
  at com.qualcomm.qti.IPerfManager$Stub$Proxy.setClientBinder(IPerfManager.java:418)
  at com.qualcomm.qti.Performance.connectPerfServiceLocked(Performance.java:171)
  at com.qualcomm.qti.Performance.<init>(Performance.java:121)
  - locked <@addr=0x...> (a java.lang.Class<com.qualcomm.qti.Performance>)   ← 类锁
  at android.util.BoostFramework.<init>(BoostFramework.java:242)
  at android.app.ActivityThread.handleBindApplication(ActivityThread.java:7864)
```

链路：官方 framework 的 `android.util.BoostFramework.<init>` → `com.qualcomm.qti.Performance`
→ `IPerfManager.setClientBinder`（同步事务到 `vendor.perfservice`）。perfservice 自己要先连
QTI AIDL perf2 HAL，而 lineage vendor 底包里没有这个 HAL（`dumpsys vendor.perfservice` =
FAILED_TRANSACTION），于是它一个事务都不回，全体 app 主线程持 `Performance` 类锁 parked。

伴生指标（修前实测）：`am start -W -S -n com.android.settings/.Settings` TotalTime 9~25s；
53 秒 logcat 窗口内 10 条 `ANR in …`（全部 Reason 为 `failed to complete startup`）；
桌面灰屏挂「Nothing Launcher isn't responding」。

## 2. 修法

### 2.1 收编官方 perf2 HAL（`build/vendor/source/vendor.qti.hardware.perf2/`）

承 popsicle-16 的同名子构建（同一栈、同一修法），差异只在闭包按本盘官方 vendor 重算：

- `/vendor/bin/hw/vendor.qti.hardware.perf2-hal-service`（0755）
- 6 个运行库（0644）：`vendor.qti.hardware.perf2-V1-ndk.so`、`libq-perflog.so`、
  `libperfconfig.so`、`libperfgluelayer.so`、`libqti-perfd-client.so`、`libqti-util.so`
- `vendor/etc/init/vendor.qti.hardware.perf2-hal-service.rc`：去掉 QTI 专属 group
  `oem_2907`（本盘 `/vendor/etc/group`、`/system/etc/group` 均无此组）与官方 cgroup 写触发器
- `vendor/etc/vintf/manifest/vendor.qti.hardware.perf2.xml`：`IPerf/default`（AIDL）
- baselines 登记 9 条 SELinux 标签；`inject.py` 的 `STAGE_FILE` 补 rc/xml/lib64 走 0644

### 2.2 sepolicy：perf 客户端清单照官方 gold（`build/vendor/build/sepolicy.py`）

**只收编 HAL 不够**：实测该轮 Settings 冷启动仍 17s，logcat 里 8 条
`avc: denied { find } name=vendor.qti.hardware.perf2.IPerf/default`
（scontext 含 system_app/priv_app/gmscore_app），而 perfservice 的 find 被拒即
`waitForService` 永久挂起 → app 侧照旧卡死。

官方 gold vendor cil 的写法是属性化（`out/tmp/gold-vendor/etc/selinux/vendor_sepolicy.cil`）：

```
(allow vendor_hal_perf_client vendor_hal_perf2_service (service_manager (find)))
(allow vendor_hal_perf_client vendor_hal_perf_server (binder (call transfer)))
(allow vendor_hal_perf_client vendor_hal_perf_server (fd (use)))
```

本段照此实现，客户端成员取「gold 清单里本盘能解析到名字的那些」：9 个 plat 版本化影子
（`system_server/surfaceflinger/platform_app/priv_app/system_app/radio/nfc/bluetooth/
network_stack` + `_202504`）与 **`vendor_perfservice`**。

`vendor_perfservice` 的裸名声明在官方 `/system_ext/etc/selinux/system_ext_sepolicy.cil`
（实测 `(type vendor_perfservice)` 命中 1 次，且客机 `ps -AZ` 显示该服务确实以此域运行）；
运行态 init 把 plat + system_ext + vendor 的 cil 交给**同一次 secilc** 合并，故 vendor 段
可直接引用。构建侧为此新增 `sys_ext_name_set()`（从 `rom_img("system_ext")` 用
`fsck.erofs --path=/etc/selinux` 提取，进程内缓存），仅用于扩 `validate_cil` 的名字全集与
perf 段过滤；gold 属性段仍按原口径过滤，避免把 vendor 段无谓扩到 system_ext 域。
**解析不到的名字自动不写** —— 写错的代价是 secilc 失败 → InitFatalReboot。

## 3. 复测证据（实例 `runtime/3a-16/10`，经 web 控制台 stop/start）

| 指标 | 修前 | 修后 |
| --- | --- | --- |
| `init.svc.perf2-hal-1-0` | 无此服务 | `running`（pid 578，域 `vendor_hal_perf_default`） |
| `service check vendor.qti.hardware.perf2.IPerf/default` | not found | found |
| Settings 冷启动 `am start -W` | 9~25s | **1.06s**（另测 2.7s/7.2s 为开机未沉降期） |
| Gmail 冷启动 | 同类阻塞 | **1.44s** |
| 整轮开机 `ANR in ` / `failed to complete startup` | 53s 窗口内 10 条 | 全量 20808 行 **0 条** |
| 控制台点击（RFB 5928 → 客机） | 应用打不开 | dock 电话 (175,2028) → `GoogleDialtactsActivity`；桌面 Weather (652,1410) → 天气起来并弹权限框；(540,1456)/(540,1279) 两次弹窗按钮全部命中 |
| 残留 perf2 find 拒绝 | — | 仅 gold 也不授的域（`vendor_qtelephony`/`traceur_app`/`logtool_app`/`shell`/`mediaprovider`…），非阻塞 |

注：`dumpsys vendor.perfservice` 仍返回 FAILED_TRANSACTION —— 该 HAL 本就没有 dump 实现，
判据是 app 侧不再 parked（ANR=0、冷启动秒级），不是 dumpsys。
