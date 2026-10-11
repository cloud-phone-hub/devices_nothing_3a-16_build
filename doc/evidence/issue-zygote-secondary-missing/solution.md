# Issue: system_server 周期自杀重启（MIUI bootCompleted 硬连 zygote_secondary）—— 解决方案

- 设备: Xiaomi Redmi Note 13 5G (note13-5G-15)
- 宿主: macOS (darwin, QEMU + hvf)
- 状态: **已解决**（2026-09-15）：将官方 odm 的 abilist 声明改为纯 64 位并落地生效后，
  `ro.product.cpu.abilist32` 运行时为空 → framework 判定纯 64 位设备 → `zygote_secondary`
  连接不再发生 → `android.display` FATAL 归零、zygote 无重启、BOOT_COMPLETED 正常推进。

---

## 1. 问题现象（qemu.log 证据链，修复前）

```
# init 侧：zygote 周期性被杀重启（非 Watchdog，无 *** GOODBYE!）：
init: Service 'zygote' (pid 410) received signal 9 from uid 0 pid 410
init: Service 'netd' (pid 409) received signal 9 from uid 0 pid 409

# system_server 侧（logcat 串口通道捕获）：
E ZygoteProcess: Failed to connect to Zygote through socket zygote_secondary
E AndroidRuntime: android.util.Log$TerribleFailure: Failed to connect to Zygote through socket zygote_secondary
E AndroidRuntime: *** FATAL EXCEPTION IN SYSTEM PROCESS: android.display
E AndroidRuntime:   at android.os.ZygoteProcess.bootCompleted(ZygoteProcess.java:996)
E AndroidRuntime:   at com.android.server.am.ActivityManagerService.finishBooting(...)
E AndroidRuntime:   at com.android.server.wm.WindowManagerService.performEnableScreen(...)
E AndroidRuntime: Caused by: android.os.ZygoteStartFailedEx: Error connecting to zygote
E AndroidRuntime:   at android.os.ZygoteProcess.attemptConnectionToSecondaryZygote(...)
E AndroidRuntime: Caused by: java.io.IOException: No such file or directory
E AndroidRuntime:   at android.net.LocalSocketImpl.connectLocal(Native Method)
I am_crash: [686,0,system_server,-1,java.io.IOException,No such file or directory,LocalSocketImpl.java,-2,0]
```

周期特征：约 68s 一轮，每轮 `enable_screen` 后即 FATAL → system_server 自杀 → zygote 重启。

## 2. 根因

| # | 事实 | 结论 |
|---|---|---|
| 1 | 崩溃发生在 `finishBooting → bootAnimationComplete → performEnableScreen → AMS.bootCompleted`，线程 `android.display` | 系统启动**收尾阶段**的 zygote 连接问题 |
| 2 | 栈底 `attemptConnectionToSecondaryZygote → LocalSocket: ENOENT` | system_server 硬连 `/dev/socket/zygote_secondary`，socket 不存在 |
| 3 | 底座 lineage virtio 为 64-bit only：init 只有 primary zygote，无 32 位 zygote_secondary 服务 | secondary socket 从根上不存在 |
| 4 | **决定性**：init 加载 `/odm/etc/build.prop`（官方 gold，键 `ro.odm.product.cpu.abilist*`）把设备声明为 **32+64 混合**（`abilist32=armeabi-v7a,armeabi`）；而 `/vendor/build.prop`（lineage 纯 64）的同类声明因 `vendor_init` 无 `build_prop` set 权限被 SELinux 拒绝、未锁成 | framework 按 `Build.SUPPORTED_32_BIT_ABIS` 非空判定设备支持 32 位 → ZygoteProcess 期望 secondary → bootCompleted 硬连 → ENOENT FATAL |
| 5 | pack_super_v1 时代正常的原因：/odm 为 lineage 底包（无 32 位列表）+ vendor 声明生效 → abilist32 空 → framework 纯 64 | 变量在「odm 提供什么声明」，不在 vendor_init 权限 |

修复判定：属性层面 `ro.*` 一旦设置即锁定（先到者胜），把 **odm 侧声明**在 init 加载时变成纯 64 即可，无需补 secondary socket、无需 32 位 zygote。

## 3. 落地方案（官方 odm abilist 64 位化 + 内核白名单）

### 3.1 `build/scripts/build_odm.py`（新建）

- 输入：官方 `vendor_a.img`（erofs，`roms/gold_images_*/…/vendor_a.img`）。
- 流程：
  1. `fsck.erofs --extract=` 解包到 `out/tmp/odm`（缓存，`--force` 重解）；
  2. 读 `/odm/etc/build.prop`（官方 1202B，含 `ro.odm.product.cpu.abilist=arm64-v8a,armeabi-v7a,armeabi` 等）；
  3. 改写 abilist 行为纯 64：`abilist=arm64-v8a`、`abilist32=`（空）、`abilist64=arm64-v8a`；
  4. 产出 `out/dist/odm/etc/build.prop`。

### 3.2 `build/scripts/build-patch.py`（扩展）

- 收编 `out/dist/odm` 子树：patch.img 内新增 `/odm/etc/build.prop`
  （meta 固定：0644 / 0:0 / `vendor_file`；目录 `/odm`、`/odm/etc` 固定目录 meta）。
- 与 vendor/vendor_dlkm 子树同套 mke2fs -d + debugfs 回填流程。

### 3.3 `kernel/source/fs/namei.c`（白名单 +1 项）

- 在 CDH 重定向白名单新增 `/odm/etc/build.prop`（文件精确，false）：
  init 打开该路径时被改写为 `/mi_ext/odm/etc/build.prop` → 读到 64 位覆盖件。
- 生效依赖**重新真实编译内核**：build-kernel.sh 默认走 prebuilt 缓存（kernelrelease/recipe
  + SHA256SUMS 全过即原样复用 Image，git 改动不进产物），须
  `CDH_REFRESH_KERNEL=1 python3 build/scripts/build-kernel.py` 强制编译，
  并先 `commit` namei 改动（脚本从 git HEAD 归档源码，拒绝未提交工作树）。

### 3.4 过渡性尝试（失效，记录不采用）

- ramdisk `ro.zygote=zygote64`（build-boot 注入）：init 布局切为纯 64（init 侧不再尝试起
  zygote_secondary），但 MIUI `ZygoteProcess.bootCompleted` 纯粹依赖 abilist 判定、不受
  ro.zygote 影响，FATAL 依旧 → 证明正解在 abilist 声明而非 zygote 模式。

## 4. 验证结果（QEMU 实机 qemu.log，实例 6）

| # | 验证点 | 结果 |
|---|---|---|
| 1 | 运行内核 | `#1 SMP PREEMPT Tue Sep 15 08:08:45 UTC 2026`（含新白名单） |
| 2 | Image 白名单串 | `odm/etc/build.prop` 存在于 Image（strings 命中），旧串仍在 |
| 3 | init 最终 abilist | `ro.product.cpu.abilist=arm64-v8a` / `abilist32=`（空） / `abilist64=arm64-v8a` |
| 4 | `android.display` FATAL | **0**（修复前每轮 1 次） |
| 5 | zygote 重启 | **0 次**（zygote 仅启动 1 次；修复前 68s 一轮 signal 9） |
| 6 | 系统推进 | `BOOT_COMPLETED` 广播 41 条持续完成，system_server 稳定 |

## 5. 遗留 / 下一步

1. 该 issue 已闭环；进入 boot_completed 后常规验证队列：
   - AudioService 对 MVP（空流 + 免 XML 的 IModule）的接受度、openInput/OutputStream 的
     `EX_UNSUPPORTED_OPERATION` 行为（见 issue-vendor-apex-audio §5）；
   - effect 服务（IFactory）注册后稳定性复核；
   - launcher/首屏/GPU/编解码/网络等逐项冒烟。
2. build_odm.py 依赖官方 vendor_a.img erofs 解包；本机 fetch 官方固件后在任一轮 build 前
   保持 `out/dist/odm` 产物就位（build-patch 见不到产物时自动跳过 /odm 子树，不阻塞）。
3. 若未来需要保留 32 位 app 兼容，需另起 32 位 zygote 环境（不在本 issue 范围）。