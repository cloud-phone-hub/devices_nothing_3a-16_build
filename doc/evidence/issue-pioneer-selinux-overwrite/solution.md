# Issue: vendor_sepolicy.cil 追加段互相覆盖（pioneer 域丢失）—— 解决方案

- 设备: Xiaomi Redmi Note 13 5G (note13-5G-15)
- 宿主: macOS (darwin, QEMU + hvf)
- 状态: **已解决**（2026-09-15）：audio 段追加时若 pioneer 段缺失则一并写回
  （同一份 cil 同时含两段），pioneer 域恢复在线（`daemon` 服务存活、`logcat_dump` 正常启动）。

---

## 1. 问题现象（qemu.log 证据链）

```
# 运行时 pioneer 上下文「不存在」（policy 编译产物里没有该域）：
SELinux:  Context u:object_r:pioneer_exec:s0 is not valid (left unmapped).
init: Could not create socket 'engine': setsockcreatecon("u:r:pioneer:s0") failed: Invalid argument
init: cannot setexeccon('u:r:pioneer:s0') for daemon: Invalid argument
init: cannot setexeccon('u:r:pioneer:s0') for logcat_dump: Invalid argument

# 结果：cph_daemon（宿主通道/属性修整）与 logcat_dump（logcat 串口取证）全部启动即灭，
# init 秒级重试，取证通道不可用。
```

## 2. 根因

| # | 事实 | 结论 |
|---|---|---|
| 1 | `vendor_builder.py` 依次调用 `sepolicy.patch_commands()`（追加 pioneer 段）与 `sepolicy.audio_hal_patch_commands()`（追加 audio 段） | 两条 `rm + write` 回写**同一文件** `vendor/etc/selinux/vendor_sepolicy.cil` |
| 2 | 两个函数都基于**同一原始 cil** 读原始内容再各自 append；命令按顺序执行、**后者覆盖前者** | 最终 cil = 原始 + audio 段，**pioneer 段丢失** |
| 3 | init 无 precompiled_sepolicy → 即时编译 vendor_sepolicy.cil | 编译产物里没有 `pioneer`/`pioneer_exec` 类型 → setexeccon/createsocketcon Invalid（恰好 audio 段放行生效，掩盖了覆盖 bug） |

## 3. 落地方案（`build/vendor/build/sepolicy.py`）

- audio 段追加函数内：先检查原始 cil 是否含 pioneer 段 marker（`MARKER not in merged`），
  缺失则把 `pioneer_cil(vers)` 一并拼进本次写入的 cil，保证最终文件**同时含两段**。
- 写回后立即用 debugfs 从镜像校验两个 marker 数量（`grep -c` = 2）防回归。

## 4. 验证结果（QEMU 实机 qemu.log）

| # | 验证点 | 结果 |
|---|---|---|
| 1 | 最终 cil 双段 | debugfs 校验 marker 计数 2（pioneer + audio-hal） |
| 2 | pioneer 上下文 | 无 `Context u:object_r:pioneer_exec:s0 is not valid`；setexeccon 不再 Invalid |
| 3 | daemon 服务 | `starting service 'daemon' … pid 394` 后持续 running（不再 9ms 被杀） |
| 4 | logcat_dump | `starting service 'logcat_dump'`（sys.boot_progress.systemserver=1 触发）正常 |

## 5. 遗留 / 下一步

1. 抽象：把「多段追加到同一 cil」的写回改为**单次合并写回**（收集所有待追加段 → 一次 rm+write），
   根除同类覆盖风险（health/camera 等后续追加段同样适用）。
2. 构建期加断言：镜像侧校验 marker 集合与期望完全一致。