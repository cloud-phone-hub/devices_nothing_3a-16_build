# Issue: Remote KeyMint HAL 集成（android.hardware.security.keymint）—— 解决方案

- 设备: Xiaomi Redmi Note 13 5G (note13-5G-15, MTK MT6833, MIUI 15 / lineage-22.0 virtio)
- 宿主: macOS (darwin, QEMU + hvf)
- 状态: **已解决**（2026-09-16）：Remote KeyMint（Cuttlefish 远程 keymint，经 /dev/hvc3 与 host
  secure_env 收发 keymaster 消息）完整注册，keystore2 正常访问，BOOT_COMPLETED + Launcher
  + adb shell 全部可用；`cuttlefish/source` 已回上游（git 干净），全部定制收敛到设备构建层。

---

## 1. 问题现象（修复前证据链）

```
# keymint HAL 启动 ~38ms 后 SIGABRT（signal 6），wire 停在 28~40B（第 3 条消息前）：
init: Service 'vendor.keymint-default' (pid X) received signal 6 ... （qemu.log）

# keystore2 等不到 keymint HAL，循环吊销：
E keystore2: Failed to find keymint service ... （qemu.log）
I keystore2: Waited one second for android.system.keystore2.IKeystoreService/default ... could not find
→ vold 永远等 keystore2 -> /data FBE 锁死 -> 卡 boot（system_server 反复重启）
```

早期还有两个旁支假象：
1. `wait /dev/hvc3 10`（init.virt.rc wait_for_file → stat）连续 812 次 avc denied getattr → 10s 超时 fail；
2. keymint 的 LOG(FATAL)/CHECK 写 `/dev/kmsg_debug`（label 为 tmpfs、无读取端）被吞 → 崩溃原文取证日志全丢。

## 2. 根因（按发现顺序的关键链）

| # | 事实 | 结论 |
|---|---|---|
| 1 | rc 首版门控 `on property:init.svc.vendor.dlkm_loader=stopped`（+ disabled）：dlkm_loader 加载完 8 个
     内核模块后卡在 `SetProperty`（SELinux 拒）**永不退出** → stopped 永不触发 → keymint 永不启动 | 门控依赖 dlkm_loader 退出，死锁 |
| 2 | 移入 `/etc/init/hw/init.virt.rc` `on post-fs`：`start dlkm_loader` + `wait /dev/hvc3` + `start vendor.keymint-default`；
     而 `on late-fs` 在 vendor rc 里实测不触发 | 必须放已证实触发的机制里（post-fs） |
| 3 | **决定性命中**：`ConfigureVendorPatchlevel` 请求值 = `GetVendorPatchlevel()` → 读
     `ro.vendor.build.security_patch`：note13 lineage vendor **该属性缺失**（基座值 2024-11-05 运行时
     set 被 SELinux 拒，或根本没设）→ 返回 **0** → host secure_env 拒绝 → `Initialize()==false` →
     LOG(FATAL) → SIGABRT（崩在第 3 条 CONFIGURE_BOOT_PATCHLEVEL 之前，wire 停在 cmd18+cmd32） | vendor_patchlevel=0 被 host 拒 |
| 4 | 紧随其后 `ConfigureBootPatchlevel` 用 `std::stoi(getProperty("ro.vendor.boot_security_patch",""))`：
     属性缺失 → `stoi("")` **抛 std::invalid_argument** → terminate（与 3 同为缺属性派生） | 空属性 stoi 崩溃 |
| 5 | wire logfile 只记录 **guest→host 单向**（解码器按 `[cmd31|resp1][size][payload]` 切分 11290B
     无任何 is_response 消息）→ 前期"host secure_env 静默不回包"是误判；pixel6 正常、note13 前 2 条
     消息字节级一致、第 3 条前崩溃 → 差异在**请求方配置值**而非 host 通道 | 取证方向纠偏 |
| 6 | host 对 pixel6 接受 vendor/boot patchlevel（20250505），对 note13 的 0 拒绝 | 补属性为正道 |

## 3. 落地方案（定制全部在设备构建层，source 回上游）

### 3.1 属性补齐（三件套，核心修复）
1. **baselines 声明**：`build/baselines/gold_images_*.json` → `vendor.build-prop-sets`（对齐 pixel6
   `vendor.build-prop-sets / build-prop-unsets` 结构）：
   `["ro.vendor.build.security_patch","2025-05-05"]`、`["ro.vendor.boot_security_patch","2025-05-05"]`
   （连同图形后端 mesa / 64-bit only 口径一并迁入）。
2. **props.py 改为从 baselines 驱动**：删除硬编码 VENDOR_PROPS，读 `build-prop-sets/unsets`
   统一渲染 `/vendor/build.prop`。
3. **property_contexts 放行**：`sepolicy.keymint_hal_patch_commands` 追加 vendor_property_contexts 一条
   `ro.vendor.boot_security_patch → vendor_security_patch_level_prop exact string`
   （对齐 pixel6 transforms.go）。⚠️ 踩坑：基座对 `ro.vendor.build.security_patch` **已有** exact 规则，
   再追加会 `Duplicate exact match → init InitFatalReboot`（实例 44 实测 23 次 fatal signal 6），
   **只补 boot 一条**。

### 3.2 HAL 启动门控 / 依赖（build/vendor/source/android.hardware.keymint/build.py）
- rc：`on late-fs` 内 `wait /dev/hvc3 30` + `start vendor.keymint-default`（去 `class early_hal`，
  hvc3 依赖 virtio_console.ko 晚加载，须等 ueventd 创建设备节点）。
- deps.py（`build/vendor/build/deps.py` 新建，接入 vendor_builder.py）：从 **note13 自身**
  lineage-22.0 super 组装 6 个链接期运行库（`libc++.so/libbase.so/libbinder_ndk.so/libcrypto.so
  /libkeymaster_messages.so/liblog.so`），build.py 经 `--runtime-lib-dir` 参数消费。
- compat 垫片 `standalone/keymaster_tags_module_hash.cpp`（`--extra-source`）：note13 旧
  `libkeymaster_messages.so` 未导出 `keymaster::TAG_MODULE_HASH`，静态初始化引用 → 强定义补齐；
  运行期重链自身 guest 库。

### 3.3 SELinux（sepolicy.py，随 vendor 重打包写回）
- keymint v1 段：`/dev/hvc3 → keymaster_device`（file_contexts 字面行遮蔽正则）+
  hal_keymint_default 对 hvc3 chardev 的 getattr/ioctl/open/read/write；
- keymint v2 段：vendor_init getattr（消 hvc3 wait 超时噪音）+ kmsg_debug(tmpfs) write（取证日志可见性）；
- 版本化影子类型（plat_pub_versioned `_202404`）引用，validate_cil 全量命名空间校验。

### 3.4 `cuttlefish/source` 回上游
- 修复期曾改 3 处：`remote_keymaster.cpp`（vendor 兜底 + boot ParseInt）、`service.cpp`（kmlog 探针）、
  `keymaster_configuration.cpp`（os_patchlevel clamp 202605→202505）。
- 属性/门控/垫片在设备层成立后**逐处撤销**（`git checkout`），实例 46 实测：**原生
  `os_patchlevel=202605`（0x03176d）host 接受** —— 证伪"host 拒过新 patchlevel"假设，
  clamp 不是必须；根因从头到尾是 vendor/boot patchlevel=0。

## 4. 验证结果（QEMU 实机）

| # | 验证点 | 实例 | 结果 |
|---|---|---|---|
| 1 | keymint 不再 SIGABRT / host wire 活跃 | 41/43/45/46 | 0 signal 6；wire 14.9~15.6KB 完整对话；host secure-env.log 有 tpm_keymaster/key_blob 处理 |
| 2 | keymint/keystore2 服务注册 | 45/46 | `ps` 见 keymint-service、keystore2 running；dumpsys -l 见 keymint HAL |
| 3 | vendor/boot 属性运行时值 | 45 | `ro.vendor.build.security_patch=2025-05-05`、`ro.vendor.boot_security_patch=2025-05-05` |
| 4 | wire cmd32/33 对齐 pixel6 | 43/45 | `89ff3401` 与 pixel6 参考实现字节一致 |
| 5 | daemon/netsimd 无阻塞 | 41+ | QEMU 正常组装（BluetoothHCI=false，关 netsimd 见 device.go 注释） |
| 6 | boot 到桌面 | 41+ | `sys.boot_completed=1`、BOOT_COMPLETED 广播、Launcher 运行、adb shell 可用 |

## 5. 遗留 / 下一步

1. adb 链路（netns adbd：连接可达、shell 偶发 `error: closed`）单独收尾；VNC console 5910 可用。
2. camera 反复退出、IMTService/IRadioConfig 缺失等常规冒烟队列继续。
3. `ro.vendor.build.security_patch` 的运行时 set 依赖基座既有 property_context；若换官方 gold
   为基座（vendor-fstype=erofs）需复核 base 中的 contexts 表，避免 build.security_patch 回归。
4. remote keymint 密钥材料/证明能力在 host secure_env；进一步 P3（`FactoryTEEVerifiedBootHash`
   fe049d… 与 grub `androidboot.vbmeta.digest` 一致性）未强制，不影响当前链路。