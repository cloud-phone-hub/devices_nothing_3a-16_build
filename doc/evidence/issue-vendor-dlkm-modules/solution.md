# Issue: /vendor/lib/modules 误读厂商 ko（Exec format error）—— 解决方案

- 设备: Xiaomi Redmi Note 13 5G (note13-5G-15)
- 宿主: macOS (darwin, QEMU + hvf)
- 状态: **已解决**（2026-09-15）：patch 镜像 `/vendor/lib/modules` 由「软链接到厂商目录」改为
  **如实模块目录**（内容 = `vendor_dlkm/lib/modules` 副本），`dlkm_loader` 从 `/vendor_dlkm/lib/modules`
  加载 8 个自编译内核模块成功，不再尝试厂商 ko。

---

## 1. 问题现象（qemu.log 证据链）

```
# dlkm_loader 强行加载厂商旧内核 ko（arch 不匹配 / 符号不匹配）→ Exec format error：
dlkm_loader: Failed to insmod '/vendor/lib/modules/mtk-pwm.ko' with args '': Exec format error
dlkm_loader: LoadWithAliases was unable to load mtk_pwm, tried: mtk_pwm
```

背景：本机内核为**自编译**（cloud-droid-hub CDH 内核，GKI 基线 + virtio 驱动），
厂商/旧底座的 ko 二进制与之不 ABI/版本兼容。

## 2. 根因

| # | 事实 | 结论 |
|---|---|---|
| 1 | `/vendor/lib/modules` 在 patch（/mi_ext）里是**软链接**，指向厂商声明的模块目录（保留官方 vendor 的模块布局） | 读取 `/vendor/lib/modules/*.ko` 实际命中**厂商旧内核模块** |
| 2 | 厂商旧内核模块与自编译内核不匹配（Exec format error：模块版本/符号校验失败） | dlkm_loader 每轮反复尝试失败，模块缺失雨点 |
| 3 | 真正可用的内核模块放在 `vendor_dlkm/lib/modules`（dlkm 分区语义）；内核 namei.c CDH 重定向表把 `/vendor/lib/modules` 指到 `vendor_dlkm/lib/modules` | 模块来源应该统一走 vendor_dlkm，厂商路径不应被读到 |

## 3. 落地方案

### 3.1 patch 镜像：`/vendor/lib/modules` 改为如实目录（已完成）

- 不再用软链接：把 `vendor_dlkm/lib/modules` 的模块目录**复制**到 patch 镜像 `/vendor/lib/modules`
  做成真实目录（含 109 个 ko，目录 mode 40755）。
- 与内核 namei.c 的 CDH 重定向表保持同义（`/vendor/lib/modules → vendor_dlkm/lib/modules`），
  vendor 镜像与内核重定向两者一致，避免软链接/厂商路径读到旧 ko。

### 3.2 内核重定向（`kernel/source/fs/namei.c`）

保留 CDH 重定向表项把 `/vendor/lib/modules` 指向 `vendor_dlkm/lib/modules`；patch 侧已实体化为
同内容目录，两条路径语义二选一即可（当前以「实目录 + vendor_dlkm 副本」为准，不再经过厂商）。

## 4. 验证结果（QEMU 实机 qemu.log）

| # | 验证点 | 结果 |
|---|---|---|
| 1 | /vendor/lib/modules 属性 | 实目录（40755），109 个 ko，无软链接指向厂商 |
| 2 | 模块加载 | `dlkm_loader: Loaded virtio_console.ko … Load count is 8`（virtio 全家桶） |
| 3 | 厂商 ko 泄漏 | 无 `Failed to insmod '…/mtk-pwm.ko' … Exec format error` |

## 5. 遗留 / 下一步

1. 模块清单核对：8 个已加载模块覆盖 virtio（console/fs/net/pci/gpu）即可满足本机制取；
   若后续需要厂商协助模块（如 WiFi firmware 依赖），按「vendor_dlkm 提供 + 显式 insmod 白名单」扩展。
2. 与 issue-vendor-apex-audio 无耦合，可独立回归。