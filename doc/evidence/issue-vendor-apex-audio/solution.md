# Issue: audioserver SIGSEGV 循环 → system_server Watchdog 重启（audio HAL 缺失）—— 解决方案

- 设备: Xiaomi Redmi Note 13 5G (note13-5G-15)
- 宿主: macOS (darwin, QEMU + hvf)
- 状态: **已打通**（2026-09-15）：audio HAL 自研服务注册成功、audioserver 崩溃清零、
  `IModule/IConfig/IFactory` 均被 audioserver 发现；AudioService 对 MVP（空流 + 免 XML）的
  接受度与 effect 服务稳定性待 boot 通路后的复测项见 §5。

---

## 1. 问题现象（修复前 qemu.log 证据链）

```
# audioserver SIGSEGV 循环（开机即崩，~5s 一次）：
F libc : Fatal signal 11 (SIGSEGV), code 1 (SEGV_MAPERR), fault addr 0x0 in tid 462 (audioserver)
E AudioUtilmtk: Unable to read config file '/vendor/etc/audio_framework_configuration.xml'. XML_ERROR_FILE_NOT_FOUND
init: Command 'restart vendor.audio-hal-aidl' ... failed: service vendor.audio-hal-aidl not found

# system_server 卡死在 AudioService 构造 → 60s Watchdog 处决：
Watchdog: - locked <0x0ba9e7ee> at com.android.server.audio.AudioDeviceInventory.<init>
Watchdog: *** GOODBYE!
Process : Sending signal. PID: 662 SIG: 9

# zygote 连锁自杀重启：
init: Service 'zygote' (pid 402) received signal 9 from uid 0 pid 402
```

阶段 2（初版「从 apex 普通服务化」后）：

```
F linker  : CANNOT LINK EXECUTABLE "/vendor/bin/hw/android.hardware.audio.service-aidl.example":
            library "libandroidicu.so" not found: needed by /system/lib64/libxml2.so in namespace (default)
init: Service 'vendor.audio-hal-aidl' (pid 1706) exited with status 1
init: process with updatable components 'vendor.audio-hal-aidl' exited 4 times before boot completed
```

## 2. 根因

| # | 事实 | 结论 |
|---|---|---|
| 1 | `/vendor/apex` 是官方（cas/widevine），lineage 的 audio HAL 在 `com.android.hardware.audio.apex`（apexd 扫不到） | audio HAL 服务（IModule/IConfig/IFactory）缺失 → audioserver 拿不到 HAL → 启动期空指针 SIGSEGV |
| 2 | `audioserver` 是 MIUI/MTK 定制（AudioUtilmtk），依赖官方 vendor 的 `audio_framework_configuration.xml` 与 MTK HAL 服务名（`vendor.audio-hal` / `vendor.audio-hal-4-0-msd`）；而本架构 /vendor 为 lineage virtio 底座（/vendor/bin·etc·lib64 全量重定向到 /mi_ext） | 音频栈「MIUI 消费端 ↔ lineage/cuttlefish 服务端」错配，崩在启动期 |
| 3 | lineage 的 audio HAL 服务二进制从 apex 提取后，其依赖 `libxml2.so` 属 VNDK/公共库，vendor 命名空间解析到 `/system/lib64/libxml2.so`（MIUI 版）；该库依赖 `libandroidicu.so`（system-only，vendor 命名空间不可达）→ CANNOT LINK | apex 命名空间自带自洽 `libxml2.so`（只依赖 libc/m/dl），vendor 命名空间不认 payload 那份；**从 apex「抽取」路线先天带链接雷** |

## 3. 落地方案

核心策略：**放弃从 apex 抽取二进制，改为在宿主直接编出自研/自洽 audio HAL 服务**，
依赖闭包为 `libc++ + libbinder_ndk + liblog`（彻底不引 lineage 的 libxml2），
NEEDED 闭包内无 `/system/libxml2.so`，CANNOT LINK 从源头消失。

### 3.1 audio HAL 自研实现（`build/vendor/source/android.hardware.audio/`）

- `aidl/src/`：从 lineage-22 同步 **121 个 .aidl** 接口（`android.hardware.audio.{core,common,effect,core.sounddose}` +
  `android.media.audio.common` 类型集 + `android.hardware.common`/`fmq`），构建期用 aidl
  (build-tools 36.1.0) NDK 后端生成 121 cpp/363 hdr，本机 clang 22 + NDK r29 全量编译通过。
- `src/`：自研实现
  - `ConfigImpl`（IConfig，2 方法）
  - `ModuleImpl`（IModule，40 方法：primary 端口路由 + module/route 配置 + 音量静音，硬编码免 XML 解析）
  - `EffectFactoryImpl`（IFactory，空实现）
  - MVP 边界：`openInput/OutputStream` 暂回 `EX_UNSUPPORTED_OPERATION`（无 stub 流），其余接口全实现。
- 两个 `main` 分别以 `vendor.audio-hal-aidl` / `vendor.audio-effect-hal-aidl` 注册
  `IModule/default+primary / IConfig/default / IFactory/default`。
- 产物（`out/dist/vendor`）：`bin/hw/android.hardware.audio.service-aidl.example`(7.3MB) +
  `bin/hw/android.hardware.audio.effect.service-aidl.example`(7.3MB)；
  `etc/init/*.rc`（/vendor/bin/hw 路径改写）、`etc/vintf/manifest/android.hardware.audio.service-aidl.xml`
  只声明真实实例。
- **硬验证**：AArch64 PIE、`/system/bin/linker64`、RELRO/BIND_NOW；
  `NEEDED = libc++/libbinder_ndk/liblog/libdl/libm/libc`——**无 libxml2**。
- 构建 harness：`build.py`（codegen→compile→link→rc/vintf→SELinux labels，工具链 env 可覆盖）。

### 3.2 SELinux audio 放行（`build/vendor/build/sepolicy.py`）

`hal_audio_default` → `hal_audio_service_{vers}`/`default_android_service_{vers}` `service_manager add/find`；
`audioserver_{vers}` → `default_android_service_{vers}`/`hal_audio_service_{vers}` `service_manager find`
（兜底 contexts 未生效场景）。方括号内追加写回 vendor_sepolicy.cil（幂等 marker 判定）。

## 4. 验证结果（QEMU 实机 qemu.log）

| # | 验证点 | 结果 |
|---|---|---|
| 1 | audioserver SIGSEGV 循环 | **0 次**（`Fatal signal 11.*audioserver` 计数 0） |
| 2 | audio HAL 服务拉起 | `started service 'vendor.audio-hal-aidl'` / `'vendor.audio-effect-hal-aidl'` 成功 |
| 3 | VINTF | audioserver 持续 `Found IModule/default、IFactory/default in device VINTF manifest` |
| 4 | 服务注册 / SELinux | `IModule/*` 无 `avc denied {add}`；audioserver find 放行 |
| 5 | 依赖闭包 | NEEDED 无 libxml2，CANNOT LINK 从源头消失 |

## 5. 遗留 / 下一步（audio 自身）

1. **AudioService 行为确认**：MVP 空流 + 免 XML 的 IModule 是否被 MIUI AudioService 接受
   （openInput/OutputStream 的 `EX_UNSUPPORTED_OPERATION`），待 boot 通路后复测。
2. **effect 服务 SIGKILL 链路**复核（早前 ~5s 自杀），确认 IFactory 注册后的稳定性。
3. **boot_completed 后音频实际功能**：播放/音效/AudioFlinger 与 MIUI 会话逐项验证。