# Issue: scrcpy 触摸/滑动无效（dropping inconsistent event）—— Virtio 输入设备被识别为 STYLUS —— 解决方案

- 设备: Xiaomi Redmi Note 13 5G (note13-5G-15)
- 宿主: macOS (darwin, QEMU + hvf)
- 状态: **已打通**（2026-09-17）：注入两个 idc 覆盖文件后，Virtio 触摸设备按触屏
  （finger）分类，InputDispatcher 不再丢弃注入事件；adb `input tap/swipe` 与 scrcpy
  触摸注入全部恢复。

---

## 1. 问题现象

scrcpy（以及 adb `input`）注入的触摸**全部无效**：

- scrcpy 界面能显示，但点击/左右滑动无任何响应。
- adb `input tap` / `input swipe` 命令返回成功，但界面无变化。

guest 日志关键证据：

```
E AudioRecord...（无关，另一 issue）
W InputDispatcher: channel 88230db com.miui.home/...Launcher (server)~
    dropping inconsistent event: DispatchEntry{... targetFlags=FOREGROUND | SPLIT ...}
W InputDispatcher: channel [Gesture Monitor] miui-gesture (server)~
    dropping inconsistent event: ...
```

注入的 `DOWN` 与 `UP` 都在 InputManager 层正常（`injectMotionEvent` 有日志），但
InputDispatcher 分发给每个 channel 时被 `trackMotion` 拒绝。

## 2. 根因

### 2.1 表象：per-channel motion state 被"幽灵 stylus hover"占住

`dumpsys input` 的 `TouchStatesByDisplay`：

```
0 : name='...Launcher', mDeviceStates=6:[touchingPointers=[], downTimeInTarget=<not set>,
    hoveringPointers=[Pointer(id=0, STYLUS)], pilferingPointerIds=<none>]
1 : name='[Gesture Monitor] miui-gesture', ... hoveringPointers=[Pointer(id=0, STYLUS)], ...
```

每个 channel 的 InputState 里有一个 **STYLUS 悬停指针**（`isHovering=true`）占着 motion
状态。

### 2.2 机制：InputState::trackMotion 拒绝"stylus stream 之后的非 stylus 事件"

AOSP `frameworks/native/services/inputflinger/dispatcher/InputState.cpp`（L99-108）：

```cpp
if (!mMotionMementos.empty()) {
    const MotionMemento& lastMemento = mMotionMementos.back();
    if (isStylusEvent(lastMemento.source, lastMemento.pointerProperties) &&
        !isStylusEvent(entry.source, entry.pointerProperties)) {
        // We already have a stylus stream, and the new event is not from stylus.
        return false;   // → "dropping inconsistent event"
    }
}
```

一旦某 channel 有 stylus 事件流（悬停也算），后续**非 stylus**（TOUCHSCREEN 触摸注入）
事件一律被判 `inconsistent` 丢弃。

### 2.3 源头：QEMU Virtio Tablet 被 InputReader 分类为 STYLUS 且残留悬停

- QEMU 提供 `QEMU Virtio Tablet`（event2，含 `ABS_X/ABS_Y/BTN_TOUCH`）与
  `QEMU Virtio MultiTouch`（event1）两个触摸设备。
- 这两个设备**无 idc 配置**，InputReader 按其能力把 Virtio Tablet 分类为
  `Sources: KEYBOARD | TOUCHSCREEN | STYLUS`，且事件被映射成 `toolType=STYLUS`。
- 设备上有一个悬停指针（内核 `ABS_X=22015 ABS_Y=18567` 非零，来源为 QEMU 初始化 /
  VNC 会话残留）→ InputReader 输出 `isHovering=true` 的 stylus 事件 → 各 channel
  InputState 记录 stylus stream → 注入（TOUCHSCREEN）被拒。

根因链：

```
QEMU Virtio Tablet（无 idc）
 → InputReader 按能力分类为 STYLUS + 残留 isHovering=true 指针
 → channel InputState 记录 stylus hover stream
 → trackMotion(注入 DOWN/UP, TOUCHSCREEN) 判为 "stylus 后的非 stylus" → return false
 → "dropping inconsistent event" → scrcpy/adb 触摸全部无效
```

## 3. 落地方案（设备定制层）

注入两个 idc 覆盖文件（`build/vendor/patch/usr/idc/`，经 vendor_builder 注入
`/vendor/usr/idc/`）：

- `QEMU Virtio Tablet.idc`
- `QEMU Virtio MultiTouch.idc`

内容（强制触屏 finger，杜绝 STYLUS 分类与悬停）：

```
touch.deviceType = touchScreen
touch.toolType = finger
device.internal = 1
```

同时登记 baselines SELinux 标签（`usr/idc/QEMU Virtio Tablet.idc` 与
`usr/idc/QEMU Virtio MultiTouch.idc` → `u:object_r:vendor_file:s0`）。

## 4. 验证结果（实例 66）

| # | 验证点 | 结果 |
|---|---|---|
| 1 | `dumpsys input` 设备 Sources | `KEYBOARD | TOUCHSCREEN`（**STYLUS 消失**） |
| 2 | `adb shell input tap 540 1200` | DOWN+UP 完整注入，**0 次 dropping** |
| 3 | `adb shell input swipe 900 1400 200 1400 300` | DOWN→UP 完整滑动轨迹，**0 次 dropping** |
| 4 | scrcpy 启动 | `[server] INFO: Device: [Xiaomi] Redmi gold`，无 ERROR；触摸注入同路径恢复 |

## 5. 关键文件

- 注入树：`build/vendor/patch/usr/idc/QEMU Virtio Tablet.idc`、
  `build/vendor/patch/usr/idc/QEMU Virtio MultiTouch.idc`
- 基线：`build/baselines/gold_images_OS3.0.9.0...json`（新增 2 条 idc 标签）
- 参考源码：`frameworks/native/services/inputflinger/dispatcher/InputState.cpp`（
  `trackMotion` stylus 分支）、`dispatcher/InputDispatcher.cpp`（`dropping inconsistent event`）
- 运行时证据：`dumpsys input`（TouchStatesByDisplay 的 hoveringPointers）
