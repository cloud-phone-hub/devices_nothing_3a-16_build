# Issue:在线视频倍速播放(5~8 倍速)+ 绿块花屏 —— 解决方案

- 设备: Xiaomi Redmi Note 13 5G (note13-5G-15)
- 宿主: macOS (darwin, QEMU + hvf)
- 状态: **已修复并验证**(2026-09-10;2026-09-16 复发修复见 §7)

---

## 1. 问题描述

通过 VNC 观看小米视频等**在线视频**时,播放呈 5~8 倍速:正常 1 秒应推进 1 秒的剧情,
进度条每秒跳 7~8 秒;同时画面下半屏出现大面积**绿色宏块**(花屏)。

关键观察(用于排除误判):

- VNC 画面刷新间隔 **< 1 秒**(显示链路正常,排除"显示抽样"解释)
- 跳变的是**播放位置本身**,不是显示滞后
- **本地视频**(任意播放器:系统媒体查看器、小米视频本地核心)播放速度完全正常
  (实测:host 真实 84 秒 → 播放进度推进 83 秒 ≈ 1.0 倍速)
- 仅**在线播放**(爱奇艺 SDK 核心)复现倍速 + 绿块

## 2. 根因

**QEMU 启动参数从未挂音频设备 → guest 无真实声卡 → 在线播放核心拿不到音频时钟,
失去音视频同步基准后解码/呈现管线 runaway。**

因果链:

1. QEMU 命令行无 `-audiodev`/声卡设备;guest `/dev/snd/` 仅有 `timer`,
   无 `pcmC0D0p` 播放节点,音频走 stub HAL(假硬件)。
2. 播放器音视频同步的通行做法是**以音频时钟为主时钟**:音频消费位置决定
   "当前应呈现哪一帧"。
3. 无音频设备时音频时钟缺失/异常:
   - 本地播放核心有回退逻辑(音频失败改用系统时钟)→ 本地视频正常;
   - 小米视频的**在线播放核心(爱奇艺 SDK)无此回退** → 失去基准后
     解码/呈现全速 runaway → 倍速;追速度丢参考帧 → 绿块花屏。
4. 表现为:画面刷新正常,但播放时间线每秒跳数秒。

## 3. 证据

| # | 证据 | 结论 |
|---|---|---|
| 1 | 在线播放时 `dumpsys media.audio_flinger` 的 `Frames written: 0` | 音频路径完全未工作,播放器无音频时钟 |
| 2 | guest `/dev/snd/` 仅 `timer`,无 pcm 播放节点 | 无真实音频设备 |
| 3 | QEMU 进程命令行无音频设备参数 | 声卡缺失是配置问题 |
| 4 | 本地视频:系统播放器 84s→83s、小米视频本地 72s→72s,均 1.0 倍速 | 本地核心有音频失败回退, guest 时钟/显示均正常 |
| 5 | 在线播放画面下半屏绿色宏块 | 解码追速度丢参考帧的典型花屏 |
| 6 | guest `date` 与 host 一致、间隔准确;`/proc/uptime` 连续 | 排除 guest 系统时钟跳变 |
| 7 | 挂声卡后 `/dev/snd/pcmC0D0p` 出现,`Frames written` 按 48kHz 正常消费 | 音频时钟恢复 |
| 8 | 挂声卡后在线播放速度正常、绿块消失(用户确认) | 因果闭合 |

排查中排除的错误假设(记录备查):

- "VNC 帧率低导致视觉倍速":被"刷新间隔 < 1 秒但进度跳 7~8 秒"推翻;
  且早期帧率测量时视频实际处于暂停态,0fps 是静止画面不推送的正常行为。
- "guest 时钟跳变":date/uptime 采样连续,推翻。
- "网络缓冲追帧":本地高规格视频(H.265 1080p)未复现,且音频证据更直接。

## 4. 解决方案

### 4.1 代码修改

文件:`core/host/daemon/qemu/qemu.go`(makePlatform 设备参数拼装处)

```go
// 声卡:guest 无音频设备时播放器拿不到音频时钟,在线播放核心(爱奇艺 SDK)
// 失去同步基准后解码/呈现管线 runaway——实测小米视频在线 5 倍速 + 绿块花屏,
// 而本地播放器有音频失败回退(系统时钟)故正常。Intel HDA 为 guest 内核内置
// 驱动(CONFIG_SND_HDA_INTEL=y);none 后端按实时速率消费音频、提供正常
// 音频时钟(宿主不出声)。后续若要宿主出声/推流音频再换 coreaudio 或走 M3。
args = append(args,
    "-audiodev", "none,id=aud0",
    "-device", "intel-hda",
    "-device", "hda-output,audiodev=aud0",
)
```

选型说明:

- **intel-hda + hda-output**:guest 内核内置 `CONFIG_SND_HDA_INTEL=y`、
  `CONFIG_SND_HDA=y`,无需额外驱动模块;QEMU darwin/linux 构建均支持。
- **`none` 后端**:QEMU 音频子系统按实时速率混音消费,提供正确音频时钟;
  宿主不出声、无设备依赖,headless daemon 下最稳。
  darwin 构建可用后端:`none / coreaudio / wav`( `-audiodev help` )。

### 4.2 生效方式

重编译 daemon(`build/core/host-main build darwin`)并重启实例(声卡属 QEMU
启动参数,需重建虚拟机进程)。

## 5. 验证

1. QEMU 进程命令行含 `-audiodev none,id=aud0 -device intel-hda -device hda-output,audiodev=aud0`。
2. guest:`ls /dev/snd/` → `controlC0 pcmC0D0p timer`。
3. `dumpsys media.audio_flinger`:`Frames written` 随播放持续增长(修复前恒 0)。
4. 小米视频在线播放:速度正常(用户确认)、绿块消失。
5. 回归:本地视频播放、VNC 点击操作均正常。

## 6. 遗留 / 后续

1. **宿主出声 / 音频推流**:`none` 后端只修时钟不出声。需要听声音时:
   - 调试场景可换 `-audiodev coreaudio,id=aud0`(宿主扬声器);
   - 产品场景应把音频纳入推流通道(M3 WebRTC 音轨)。
2. **VNC 看视频帧率低**(0~4 fps)是独立问题:VNC 走 CPU 全帧回读
   (readback),60Hz 满帧需 ~590MB/s,定位"仅调试用"。与倍速无关。
   视频观看的正路:恢复 cloudhub-cast 投屏旁路,或推进 M3
   (dmabuf 零拷贝 → 硬编 H.264 → WebRTC)。见 `core/host/daemon/Display.md`。

---

## 7. 复发修复记录(2026-09-16,实例 61/62)

### 现象
在线视频再次倍速/卡顿(症状同 §1)。排查发现**音频时钟再次死掉**:
`dumpsys media.audio_flinger` 的默认输出线程 `Frames written: 0`(健康时应持续增长)、
`Total writes` 卡在 10;播放器报 `play() called on uninitialized AudioTrack` 与
`AudioTrack: not all data copied -3/256`。

### 根因:自研 audio HAL 的 FMQ **eventflag 是纯桩 + FMQ 未配置 efword**
之前(issue-scrcpy-audio-vdac)为解决 REMOTE_SUBMIX 捕获,把 audio HAL 换成自研
`android.hardware.audio`(VirtualDac)。其 FMQ 支撑(`FmqSupport.cpp`)的
`EventFlag` 是**纯桩**(wait 恒 TIMED_OUT、wake 空操作),且
`VdacStreamChannel` 构造 FMQ 时 `configureEventFlagWord=false`。后果:
- HAL 工作线程 `cmdMQ()->readBlocking()` **立即失败退出** → START/BURST 命令无人消费;
- 流描述符不带 efword → framework 侧 `StreamOutHalAidl::sendCommand` 的
  `writeBlocking/readBlocking` 同样失败;
- → AudioFlinger 混音器拿不到流推进 → `Frames written: 0` → 播放器无音频时钟
  → 在线播放核心失同步 runaway(倍速 + 花屏)。**与 §2 的因果链同构**,
  但这次是 HAL 层缺 EventFlag,而非宿主缺声卡。

### 修复
| 文件 | 变更 |
|---|---|
| `src/FmqSupport.cpp` | `EventFlag` 纯桩 → **真实 futex 实现**(对齐 libfmq `EventFlag.cpp`):`wake` 置位 + `FUTEX_WAKE_BITSET`;`wait` 用 `FUTEX_WAIT_BITSET` + CLOCK_MONOTONIC 绝对超时;跨进程共享同一 efword |
| `src/VdacStream.cpp` | `VdacStreamChannel` 三个 FMQ(command/reply/data)改为 `configureEventFlagWord=true`;burst 数据搬运后对 data FMQ efword `wake`(FMQ_NOT_EMPTY/FMQ_NOT_FULL);worker 退出仍写 `halReservedExit` 并 wake |

### 验证(实例 62)
```
cdh_fmq 日志: 0 条(此前每流 open 必报 readBlocking failed)
AudioStreamOut::open() ... status 0
Frames written: 368640(此前恒 0),Total writes: 180(此前卡 10)
1 Tracks of which 0 are active(轨道正常创建/移除)
scrcpy --audio-source=output 回归正常(REMOTE_SUBMIX 捕获不受影响)
```

### 关键文件
- `build/vendor/source/android.hardware.audio/src/FmqSupport.cpp`(EventFlag futex 实现)
- `build/vendor/source/android.hardware.audio/src/VdacStream.cpp`(FMQ efword + worker)
- 参考:`system/libfmq/EventFlag.cpp`、`MessageQueueBase.h`
  (`readBlocking`/`writeBlocking` 的 efword 契约 L896-1113)
