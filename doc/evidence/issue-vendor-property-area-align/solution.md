# Issue: /dev/__properties__ 属性区命名空间对齐真机（vendor 属性上下文 + 策略类型）—— 解决方案

- 设备: Xiaomi Redmi Note 13 5G (note13-5G-15)
- 宿主: macOS (darwin, QEMU + hvf)
- 状态: **已修复**（2026-09-27）：vendor 侧属性区命名空间对齐 gold 后正常启动，
  gold 的 **282 个活动属性类型区域文件全部存在（缺失 0）**，`invalid_context`/`mac_admin`
  拒绝归零，属性 set 拒绝数 **19（优于基线 22）**，lineage/自研保留类型（12 个）全部在位。

---

## 1. 目标与做法

真机（gold MIUI）`/dev/__properties__` 下的区域文件集合由各分区 `*_property_contexts`
表 + 策略中声明的属性类型共同决定。本机 `/system`、`/system_ext` 与 gold 一致，
**仅 vendor 侧是 lineage 版本**（lineage vendor 只贡献 13 个属性类型，gold vendor 贡献
282 个活动类型）。对齐做法（模块 `build/vendor/build/gold_props.py`）：

1. **上下文表替换（预会话真实落盘）**：`/vendor/etc/selinux/vendor_property_contexts`
   = gold 版（681 条）+ 保留 lineage 独有前缀规则（12 条映射，mesa/graphics/hwcomposer/
   fm/… 本 vendor 内容仍在用）；
2. **策略声明补齐（CIL 追加段，sepolicy 链最后一处写入者）**：
   - 为 gold 引用而本策略未声明的属性类型补 `(type X)`/`(roletype object_r X)`
     （本次新增 **281** 个），并按 gold 镜像 `vendor_property_type`/
     `vendor_restricted_property_type` 成员关系归属；
   - 复刻 gold 中**源域在本策略存在**的授权（property_service set / 属性区 file 读），
     源域不存在的（如 MTK 测试域 cittest_app）跳过；合并去重后 **1403** 条 allow；
   - 合成三类必要授权（均为逐类型显式授权，不并入 `property_type`——该属性背多条
     neverallow）：
     a) `init` 对属性区文件 `relabelto` 等权限集（plat 该授权以属性属性为目标，新类型不在内）；
     b) 新类型对 `tmpfs` 的 `filesystem associate`（同因）；
     c) `init` 对新类型的 `property_service set`（本机 /vendor/build.prop 由 init 直接
        加载，avc 实测 `scontext=u:r:init:s0`，无 vendor_init 过渡）。

> 不替换整个 `/vendor/etc/selinux`（见 §3.1：实测会让本 vendor 内容被 gold file_contexts
> 错标、keystore2 注册不出）。`gold_selinux.py` 模块保留但默认关闭（`CDH_GOLD_SELINUX=1`
> 才启用）。

## 2. 固证数据（对齐结果）

| 判据 | 结果 |
|---|---|
| 启动 | `sys.boot_completed=1`（重启后 20s 达成） |
| gold 活动属性类型区域文件 | **存在 282 / 缺失 0**（含 3 个带连字符的 MTK ctl 类型） |
| lineage 独有前缀规则 | **已消除**：`vendor_property_contexts` 全量替换为 gold 版（不再保留），对应 11 个多余区域文件全部消失（见 §3.8） |
| `op=setxattr invalid_context` | **0** |
| `mac_admin` denied | **0** |
| 属性 set 拒绝数（`Do not have permissions to set`） | **20**（基线 22） |
| properties.native（云机实测） | 区域文件 **744** 个、属性 **1197** 条 |
| properties.native 真机 vs 云机 | **完全一致：744 == 744，双向 diff 均为空**（最终在线复核 run-00094：真机 `c7018f1a5b299672` 744 区域文件 / 1868 属性；云机 `e85a924aad7556bf-cloud` 744 / 1198；仅区域文件清单对比，不看属性值） |
| 属性上下文**文件**逐字节比对（md5） | plat / system_ext / product / odm 四个分区表 **md5 全同**；`vendor_property_contexts` 云机多 **3 行刻意偏差**（remote keymint 的 `ro.vendor.boot_security_patch` exact 放行，见 §3.9），其余逐字节一致 |
| device-checker 注册 | 云机 `e85a924aad7556bf-cloud` 在线（停/起 App 可复现上下线） |
| 稳定性 | 12 分钟 / 24 样本全程 `boot=1`，zygote64 PID 479、system_server PID 751 恒定，无新 ANR（原始样本见 log.txt） |

## 3. 失败轮次与根因（逐个有日志实锤）

### 3.1 整目录替换 `/vendor/etc/selinux`（不可用，模块已默认关闭）
gold 策略可加载但 boot 不完成：18 条 `/vendor/build.prop` 属性被拒（含 ABI 锁定属性）、
keystore2 起来了但服务注册不出（无 avc 记录），vold 每 1s 死等
`android.system.keystore2.IKeystoreService/default` → 844s 仍无 `boot_completed`。

### 3.2 前缀过滤 bug：`Duplicate prefix match` → InitFatalReboot
用 `re.finditer(r"^\s*…", text)` 取 gold 前缀时未开 `re.M`，只命中首行 → 几乎所有前缀
被误判为"gold 无此前缀"而保留 lineage 同前缀规则 → init 报
`Duplicate prefix match` → `InitFatalReboot`。修复：逐行匹配（`for ln in splitlines()`）。

### 3.3 init 缺 `relabelto` → `Failed to initialize property area`
plat 对 init 的属性区授权以 **`property_type` 属性**为目标，新类型不在该属性内 →
建区时 relabelto 被拒。修复：逐类型显式授权（权限集取自 plat 同条）。

### 3.4 缺 `tmpfs filesystem associate` → 同前
plat 为 `property_type` 授 `(allow property_type tmpfs (filesystem (associate)))`，
新类型不在内。修复：逐类型显式授权；**不能**并入 `property_type`（其背多条 neverallow）。

### 3.5 连字符类型被正则静默丢弃 → `invalid_context` → InitFatalReboot（本次主因）
类型提取正则 `u:object_r:([A-Za-z_0-9]+):s0` 不含 `-`，而 gold 有 3 个连字符类型
（`vendor_mtk_ctl_ril-proxy_prop`、`vendor_mtk_ctl_ril-daemon-mtk_prop`、
`vendor_mtk_ctl_muxreport-daemon_prop`）→ 未声明 → init 建区 setxattr 报
`op=setxattr invalid_context="u:object_r:vendor_mtk_ctl_…:s0"` →
`Failed to initialize property area` → **guest 连续重启 63 次**（`invalid_context`
3×63 条、`mac_admin` denial 222 条与之一一配对，均为失败调用的伴随记录）。
gold 自身 CIL 就是无引号书写连字符类型（`(type vendor_mtk_ctl_ril-proxy_prop)`），
secilc 接受；修复：三处字符集补 `-`（gold_props 类型/前缀正则、sepolicy
`_TYPE_DECL_RE` 自声明类型解析，后者漏配会把自声明类型误报为未知名字）。

### 3.6 注释行被计入 → 假缺失
`gold_types()` 起初不剔注释行，被注释规则引用的 `vendor_cct_prop`
（gold 表中该行为 `#persist.vendor.trigger.cct …`）被当成"应存在"→ 校验报假缺失。
修复：仅对有效规则行提取类型。

### 3.7 属性 set 拒绝回归 36（基线 22）→ 补 init `property_service set`
属性前缀改由 gold 表归属后，18 条属性（`ro.vendor.mtk_*`、`vendor.camera.*`、
`persist.vendor.wfc.*` 等）映射到 gold MTK 类型，init 无 set 授权 → 设不进去。
逐类型补 `(allow init_202404 <type> (property_service (set)))` 后降至 **19**。
**neverallow 安全核验**：vendor 侧 neverallow 主体 `base_typeattr_234_202404`
与 plat 侧 `base_typeattr_223` 成员均不含 init，且无属性级 neverallow。

### 3.8 云机比真机多出 11 个属性区文件（lineage 独有前缀保留所致）
首版对齐为兼容本 vendor 内容保留了 lineage 独有前缀映射，结果云机属性区比真机多出
**11 个区域文件**（`vendor_mesa_prop`、`vendor_graphics_prop`/`_config_prop`、
`vendor_hwcomposer_prop`、`vendor_audio_prop`、`vendor_minigbm_debug_prop`、
`vendor_fm_radio_app_prop`、`vendor_wifi_mac_prefix`、`vendor_preinstall_check_prop`、
`vendor_create_partition_table_prop`、`vendor_recovery_ethernet_dhcp_prop`）。
按"与真机完全一致"口径消除：上下文表**全量替换为 gold 版**（不留任何 lineage 规则），
重建后 11 个文件全部消失、总数 744 == 真机，属性 set 拒绝数 20（基线 22，无回归）。
被移除规则清单见 log.txt §F.1。

### 3.9 唯一保留的刻意偏差：keymint 的 3 行放行（用户决策：保留）
属性上下文文件逐字节比对后，云机 `vendor_property_contexts` 相对真机（= gold 基包，
md5 `e0a72697418d…`）只多 3 行：

```
# cdh note13-5G-15: remote keymint boot security_patch set
ro.vendor.boot_security_patch u:object_r:vendor_security_patch_level_prop:s0 exact string
```

该规则是 remote keymint 的核心修复（见 issue-remote-keymint-hal）：HAL
`ConfigureBootPatchlevel` 用 `std::stoi(getProperty("ro.vendor.boot_security_patch",""))`
取值，值由 patch.img 的 `/vendor/build.prop`（`2025-05-05`）注入，需此 exact 映射才能
被 init set 进去；摘掉则属性无映射 → 取空 → `stoi("")` 抛异常、HAL 崩。
**决策（2026-09-27）**：保留这 3 行——文件级允许这处刻意偏差，属性区文件清单仍与真机
100% 一致（744 == 744）。真机本身对 `ro.vendor.boot_security_patch` 无映射、取值为空，
故此处不构成"对齐缺口"，属本机功能必需的差异。

## 4. 方法与判据（教训）

- **校验类型文件用 `[ -e /dev/__properties__/u:object_r:<type>:s0 ]`**：`/dev/__properties__`
  权限 0711，`ls` 需要 r 权限会 Permission denied，但 `-e` 只需 x。
- 提取类型/前缀/自声明类型时**字符集必须含 `-`**；`^` 锚点跨行匹配必须开 `re.M`
  或改逐行匹配。
- 上下文表里**注释行不参与编译**，提取前先剔注释。
- 启动成功仍按老口径判定：`boot_completed=1` + **稳定观察 ≥10~12 分钟**。

## 5. 落位

- 模块：`build/vendor/build/gold_props.py`（vendor_builder 预会话落盘上下文表；
  `sepolicy.hal_cil_chain` 末尾追加 CIL 段）。
- 开关：`gold_selinux.py` 默认关闭（整目录替换实测不可用，§3.1）。
- 产物：`out/device/note13-5g/15/os3.0.9.0.vnqcnxm/patch.img`（含对齐后 vendor 内容）。