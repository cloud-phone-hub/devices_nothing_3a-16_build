# Issue: adb 不可达 → logcat 串口取证通道（guest 侧 Java 崩溃证据）—— 解决方案

- 设备: Xiaomi Redmi Note 13 5G (note13-5G-15)
- 宿主: macOS (darwin, QEMU + hvf)
- 状态: **已落地**（2026-09-15）：`logcat-dump` 服务（pioneer 域）将
  `main/system/events/crash/kernel` 四个 buffer 持续转出到 `/dev/ttyAMA0`
  → 出现在宿主 qemu.log，成功捕获 `system_server` 的 Java FATAL 栈（`zygote_secondary` 根因定位即靠它）。

---

## 1. 问题现象（为什么需要）

- 客机处于周期性重启循环、adbd 未授权/offline，`adb logcat` 不可达；
- console/kmsg 只能覆盖内核与 init 层，`system_server` 的 Java 异常（AndroidRuntime FATAL、
  am_crash、Watchdog）只进 logd 缓冲区——此前拿不到系统层证据，只能猜测崩溃点。

## 2. 根因（取证缺口）

| # | 事实 | 结论 |
|---|---|---|
| 1 | adbd 的 TCP 拉起挂在 `sys.boot_completed=1`（cph_start_adbd.rc），guest 到不了 boot_completed | adb 通道在问题期不可用 |
| 2 | logcat 默认只在 console（ttyAMA0）无输出 | 系统层日志黑洞 |

## 3. 落地方案

### 3.1 `build/vendor/patch/bin/logcat-dump`（脚本）

```
#!/system/bin/sh
# Continuously stream logcat to the serial console (appears in qemu.log)
# 带 events/crash 缓冲，捕获 system_server 被 kill/watchdog/ANR 的关键证据
logcat -v threadtime -b main -b system -b events -b crash -b kernel > /dev/ttyAMA0 2>/dev/null
```

### 3.2 rc 触发（`build/vendor/patch/etc/init/cph_logcat_dump.rc`）

- `logcat_dump` 服务定义：pioneer 域（`u:r:pioneer:s0`）、oneshot；
- 触发点：`on property:sys.boot_progress.systemserver=1`（system_server 起步即转出，早于崩溃窗口）
  + `on property:sys.boot_completed=1`（兜底）。

依赖：pioneer 域可用性（见 issue-pioneer-selinux-overwrite——该 bug 曾让本通道全灭）。

## 4. 验证结果（qemu.log 直接证据）

| # | 验证点 | 结果 |
|---|---|---|
| 1 | 通道可用 | `AndroidRuntime: *** FATAL EXCEPTION IN SYSTEM PROCESS: android.display` 及完整栈被捕获 |
| 2 | crash buffer | `am_crash: [686,0,system_server,-1,java.io.IOException,…,LocalSocketImpl.java,-2,0]`（zygote_secondary 根因判定关键行） |
| 3 | events/kernel | `Watchdog` 块、zygote 重启时间线均齐 |

## 5. 遗留 / 下一步

1. 降噪：logcat 全量转出会刷屏（如 audioserver 等待 'activity' 1s/次），后续可按需挂
   `grep -E 'FATAL|am_crash|Watchdog|E AndroidRuntime'` 过滤或做滚动缓冲。
2. 通道与 adb 并存：adbd 可用后（boot_completed 达标）可关停 dump 或切 filter，避免双路日志冲突。