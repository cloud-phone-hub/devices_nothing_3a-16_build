# Issue: scrcpy 无音频（Cannot create AudioRecord）—— virtual DAC 真实音频通路 —— 解决方案（已完成）

- 设备: Xiaomi Redmi Note 13 5G (note13-5G-15)
- 宿主: macOS (darwin, QEMU + hvf)
- 状态: **已完成**（2026-09-16，实例 60/64）：scrcpy `--audio-source=output`
  在 guest 内成功创建 AudioRecord（REMOTE_SUBMIX 捕获），AudioFlinger 默认输出/
  输入流真实打开（status 0），policy 层 -19 全部消失。根因均为自研 HAL 与
  framework `Hal2AidlMapper`/SELinux 的契约不匹配，见 §5。
  遗留：捕获/播放采样率未对齐（8k mono 读 44.1k stereo DAC），见 §6。
  **2026-09-16 追加**：scrcpy 十几秒闪退 → guest daemon adbd 探针 CNXN
  字节序错误致每 30s 弹跳 adbd，见 §9。

---

## 1. 问题现象

scrcpy（默认 `--audio-source=output`，即 `AudioSource.REMOTE_SUBMIX`）在 note13 上启动即崩：

```
E AudioFlinger: createRecord() getInputForAttr return error -19
E AudioRecord: createRecord_l(0): AudioFlinger could not create record track, status: -19
E android.media.AudioRecord: Error code -20 when initializing native AudioRecord object.
E scrcpy  : java.lang.UnsupportedOperationException: Cannot create AudioRecord
```

根因链条（已确认的部分）：

```
scrcpy AudioRecord(REMOTE_SUBMIX, 48k, IN_STEREO)
  → AudioFlinger.createRecord → policy getInputForAttr
  → （早期）Hal2AidlMapper 找不到 r_submix 模块的 mix port        [已修，见 §3.1/3.2]
  → （中期）policy 把 REMOTE_SUBMIX 映射到 IN_DEFAULT，但无可用输入设备  [已修，见 §3.3]
  → （当前）getInputForAttr 仍 -19（-ENODEV）                      [未决，见 §5]
```

## 2. 方案（virtual DAC：进程内"虚拟扬声器"）

自研 audio HAL（`build/vendor/source/android.hardware.audio/`）从 MVP（流一律
`EX_UNSUPPORTED_OPERATION`）演进为**真实音频通路**，全部收敛在设备定制层，不碰
cuttlefish/上游：

```
App 播放(AudioTrack/MediaPlayer)
  → AudioFlinger 混音
  → IModule.openOutputStream(SPEAKER)   ← 输出流：PCM 写入 VirtualDac（进程内 ring buffer，"虚拟扬声器"）
  → （无人消费时丢最旧帧，等价 DAC 消费，不阻塞写端）
  → REMOTE_SUBMIX 捕获：openInputStream(IN_SUBMIX/IN_DEFAULT)
      ← 从同一 VirtualDac 读回声（scrcpy AudioRecord 数据源）
  → scrcpy OPUS 编码回推客户端
```

## 3. 已落地的实现

### 3.1 virtual DAC 核心（`src/`）

| 文件 | 作用 |
|---|---|
| `VdacCommon.h/.cpp` | `cdh::audio::VirtualDac`：PCM16/48k 环形缓冲（1MiB≈250ms），写端丢最旧帧、读端取回声、帧计数 |
| `VdacStream.h/.cpp` | AIDL 流协议面：command/reply/data 三对 FMQ、`VdacWorker`（高优先级命令循环 + STANDBY/IDLE/ACTIVE/PAUSED 状态机）、`VdacStreamOut`（播放→DAC）、`VdacStreamIn`（读回声）、`VdacStreamCommon`（IStreamCommon 委托） |
| `FmqSupport.cpp` | FMQ 自足支撑：`ashmem_create_region/set_prot_region`、`native_handle_*`、`hardware::details::{logError,check,errorWriteLog}`、`EventFlag` 桩——**不链 libfmq.so/libcutils.so**，NEEDED 保持 6 库 |
| `platform-shim/log/log.h` | 编译垫片：给 AOSP platform 头供 `<log/log.h>` 又不遮蔽 NDK `android/log.h` |

### 3.2 ModuleImpl 角色化与流打开

- `ModuleImpl(Role)`：`Primary`（speaker/mic，端口 1-8）与 `RemoteSubmix`（r_submix 模块，端口 10-13）两套硬编码拓扑；共享进程内 `VirtualDac` 单例。
- `openInputStream/OutputStream`：按 `mInMixPortId/mOutMixPortId` 分流，返回真实流对象 + `StreamDescriptor`（FMQ 描述符）；双流单实例冲突检查。
- `audio_service_main.cpp`：注册 `IModule/default + primary` 与 `IModule/r_submix`；VINTF manifest 补 `r_submix` 声明。

### 3.3 已修复的三个前置坑

| # | 现象 | 根因 | 修复 |
|---|---|---|---|
| 1 | `Hal2AidlMapper: findOrCreateMixPortConfig: mix port ... is not found in the module r_submix` | r_submix 端口 flags 带自定义位（FAST/PLAYBACK/VOICE_TX），与 policy 请求的空 flags 失配 | 所有端口 flags 置 0（`AudioIoFlags{}`），与 MIUI policy 精确匹配；同时把 r_submix 输入 profile 对齐 policy 的 IN_STEREO |
| 2 | policy 层把 REMOTE_SUBMIX 映射到 `IN_DEFAULT`（Hal2AidlMapper prepareToOpenStream 可见），但 primary 无可用输入设备 | MIUI policy 对 REMOTE_SUBMIX 源的默认映射是 IN_DEFAULT；policy 无该设备 | primary 拓扑补 `IN_DEFAULT` 设备端口 + 路由；policy 覆盖文件挂 `Remote Submix In`（attached + devicePort + route，见 §3.4） |
| 3 | （最新根因理论）attached 设备判定失败，engine 找不到输入设备 | AIDL v2 `AudioPolicyConfig.cpp` 判定设备 attached 需 `connection` 为空；`makeDeviceExt` 之前设 `CONNECTION_ANALOG` → 所有设备不 available | `makeDeviceExt` connection 置空（"内置 attached 设备"） |

### 3.4 policy 覆盖（设备定制层，不改 lineage 底包）

`build/vendor/patch/etc/primary_audio_policy_configuration.xml`：在 lineage 基座上新增
`Remote Submix In`（`AUDIO_DEVICE_IN_REMOTE_SUBMIX`，48k stereo）attached + devicePort +
路由到 `primary input` mix。经 vendor_builder 注入树覆盖基座同名文件（`rm`+`write`，
SELinux 标签已登记 `vendor_configs_file`）。注入的 policy 已在 guest 生效（grep 计数 4）。

## 4. 验证过程记录

| 实例 | 变更 | 结果 |
|---|---|---|
| 49 | 初版（VirtualDac 仅 primary） | HAL 注册 OK；scrcpy `-19`（r_submix 无实例） |
| 50 | +`IModule/r_submix` + submix 拓扑 + flags 修正 | Hal2AidlMapper "mix port not found in module default"（IN_DEFAULT 缺失） |
| 51-52 | +`IN_DEFAULT` 端口 + policy XML 挂 Remote Submix In（lineage unpacked，未生效） | policy 层仍 `-19` |
| 53 | policy 覆盖改注入树 `build/vendor/patch/etc/` | policy 生效（grep 4），但仍 `-19` |
| 55 | `makeDeviceExt` connection 置空（attached 判定修复） | boot OK、HAL 正常，scrcpy 仍 `-19` |

## 5. 根因链（实例 56-60 实测定位）与修复

**阻塞已解除（实例 60）**：scrcpy `Cannot create AudioRecord` 消失，
`--audio-source=output` 连跑 8s 无错误、RecordingActivityMonitor 注册
`src:REMOTE_SUBMIX` 会话、HAL 输入流/补丁创建成功。三个根因均为**自定义 HAL
与 framework `Hal2AidlMapper` 的契约不匹配**（非 MIUI policy 约束），逐个修复：

| # | 现象（实例 56-59 qemu.log 实测） | 根因 | 修复 |
|---|---|---|---|
| 4 | `Hal2AidlMapper: initialize: module default ... output -1`；`openOutput` 在 `findOrCreateMixPortConfig ... flags output:2 未找到` 失败 | 端口 `flags` 用 `AudioIoFlags{}`（AIDL union 默认 tag=**input**）→ 输出端口全被当输入，默认输出口判不出（-1）；且 out mix 口 flags 与 policy 请求 `output:2`（PRIMARY，`1<<PRIMARY`）不等 | `ModuleImpl`：`AudioIoFlags::make<output>/make<input>` 显式 tag；out mix 口 flags= `1<<PRIMARY`；仅 speaker 留默认设备位（其余 flags=0，避免 default id 被 out_submix 覆盖） |
| 5 | `openOutput/Input` 在 `mix port for config 44100/8000 ... `is not found` 失败 | mix/device 端口 profile 只有 `{48000}`、且 out 无 MONO；policy 默认输出请求 **44100** stereo、默认输入请求 **8000** mono（XML 声明 8000~48000） | 端口 profile 全量对齐 policy：rates `{8000,11025,16000,32000,44100,48000}`，out/in 均 mono+stereo |

| # | 现象 | 根因 | 修复 |
|---|---|---|---|
| 6 | `cdh_fmq: mmap failed: 9`（EBADF）→ `stream descriptor` 携带 **-1 fd**（`Passing -1 file descriptor`）→ openOutput/Input 在 framework 侧 `Failed` | SELinux 拒 `hal_audio_default open /dev/ashmem`（lineage 基座音频 HAL 在 APEX 内，基座策略缺该授权）→ 自研 `FmqSupport::ashmem_create_region` 返回 -1 | `build/vendor/build/sepolicy.py` 增 `audio_hal_cil_v2`：`allow hal_audio_default ashmem_device_202404:chr_file {ioctl read write getattr map open}`（v2 段独立标记，幂等增量追加） |

**实例 60 实证（qemu.log）**：
```
AudioStreamOut::open(), HAL returned stream 0xb4..., status 0        // 默认输出 44.1k 成功
openInput_l() openInputStream returned input 0xb4..., status 0        // 默认输入 8k 成功
[MTK_APM_Input]getInputForAttr() source 8(REMOTE_SUBMIX), 48000, 0xc // scrcpy 请求
getInputForAttr() returns input 22 selectedDeviceId 5                 // 捕获路由到 submix 口
RecordingActivityMonitor: rec update ... src:REMOTE_SUBMIX not silenced
```
policy 层 `listAudioPorts -19` 高频刷屏消失（仅剩开机初期 1 次），
`Cannot open output/input`、`does not declare a primary output`、
`Default device unreachable` 全部清零，AudioFlinger 出现真实
`AudioOut_D`（PRIMARY 输出线程，Total writes 10）。

## 6. 遗留项（音频质量）

未做采样率/声道重采样：默认输出流 44.1k stereo 写入 VirtualDac（固定 48k 语义），
而 MIUI 经"建议配置"回退后 REMOTE_SUBMIX 捕获按 **8k mono**（设备端口最低 profile）
从同一环形缓冲读取——无重采样即错位，捕获声需后续处理。候选：
a) primary 输入 profile 收敛到 48k（配合 policy XML 同步），强制捕获 48k；
b) 在 `VdacStreamOut/In` 加线性插值重采样（44.1k↔48k）；
c) 对 REMOTE_SUBMIX 直连 `in_submix`（device 5）走 48k stereo（`r_submix` 模块已是 48k stereo，policy captured 端最干净）。

## 7. 当前阻塞与下一步（历史，已被 §5 解除）

### 7.1 已修复的 vdac 副作用：AudioFlinger Overrun 刷屏（2026-09-17）

**现象**：实例启动后 `AudioFlinger_Threads: Overrun! RsmpInBuffer drop data ...`
疯狂刷屏（实测 10s ≈ 2 万条）。来自 **RecordThread（录音路径）**——某 app
（`AudioIn_16`，`AUDIO_DEVICE_IN_BUILTIN_MIC`）持续录音，读 virtual DAC 输入流。

**根因**：`VdacStreamIn::transfer` 在 VirtualDac 空时**补零返回满帧**（`memset` +
`*actualFrames = frames`）→ AudioFlinger 录音 resampler 认为持续有数据 → 输入缓冲
持续溢出 → Overrun。

**修复**（`build/vendor/source/android.hardware.audio/src/VdacStream.cpp`）：
`transfer` 只返回实际读到的帧数（空时 0），由 AudioFlinger 自行补静音。

**验证**（实例 68）：Overrun 2 万条/10s → **1 条/15s**（稳定）；`dumpsys media.audio_flinger`
真实 dropping = 0，全部修复完成；触摸/图标点击正常（点击图标打开 app）。

## 8. scrcpy 十几秒闪退修复记录（2026-09-16，实例 63/64）

### 现象
scrcpy 跑十几秒即闪退（`adb push` EOF / Server connection failed）。

### 根因
guest 侧守护进程（`core/guest/daemon/rust`）的 adbd 健康探针每 30s 对
`127.0.0.1:5555` 发 CNXN 握手，连续 2 次无回包就 `stop/start adbd` 强制弹跳。
探针的 CNXN 帧头 **command 字段字节序写反**：
```rust
let cmd: u32 = 0x434e584e;   // 经 to_le_bytes() 输出 4e 58 4e 43（错）
```
ADB 帧头 wire 上应是大端文本 `"CNXN"`（`43 4e 58 4e`），即内存值应为
`0x4e584e43`。发错命令字后 adbd 不识别、不回包 → 探针永远失败 →
每 ~30s 强制弹跳 adbd → 杀掉活跃 scrcpy 连接（弹跳即 `stop adbd`）→ scrcpy 闪退。
实例 62 qemu.log 实证：197 次 `CNXN 无响应，强制弹跳`（~33s 一次）。

### 修复
`core/guest/daemon/rust/src/main.rs` `adbd_handshake_ok()`：
- `cmd` 改 `0x4e584e43`（to_le_bytes 后恰为 `43 4e 58 4e`）；
- 去掉自定义 payload，改发空 payload CNXN（`data_length=0, data_crc32=0`），
  adbd 必回 CNXN（实测回 `device::ro.product.name=...`）。

### 验证（实例 64）
```
scrcpy --time-limit=120 完整跑满 Time limit reached（此前 ~15s 闪退）
qemu.log 强制弹跳计数: 0（此前实例 62 为 197 次）
adbd 稳定、audio/REMOTE_SUBMIX 捕获正常
```
重建：`build/core/guest-main build` → `daemon/build.py` → vendor/patch/pack → 重启实例。

## 9. 关键文件
- 实现：`build/vendor/source/android.hardware.audio/src/{VdacCommon,VdacStream,FmqSupport}.{h,cpp}`、
  `ModuleImpl.{h,cpp}`（端口 flags/address/profile + cdh_audio 诊断日志）、
  `ConfigImpl.cpp`、`audio_service_main.cpp`、`build.py`
- sepolicy：`build/vendor/build/sepolicy.py`（`audio_hal_cil_v2`：FMQ `/dev/ashmem` 授权）
- policy 覆盖：`build/vendor/patch/etc/primary_audio_policy_configuration.xml`
- 基线：`build/baselines/gold_images_OS3.0.9.0...json`（新增
  `etc/primary_audio_policy_configuration.xml` 标签 `vendor_configs_file`）
- 相关框架源码（参考）：`AudioPolicyManager.cpp`（`getInputForAttr` L2932-3152、
  `getInputForDevice` L3154-）、`Hal2AidlMapper.cpp`（`initialize` L685、
  `findPort` L550/577、`findOrCreateMixPortConfig` L367、`prepareToOpenStream` L807）、
  `DeviceHalAidl.cpp`（`openOutputStream` L477）、`StreamHalAidl.h`
  （`StreamContextAidl::isValid` L88）、`AudioPolicyConfig.cpp`（`aidl2legacy_AudioHwModule_HwModule` L70）
