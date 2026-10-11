# 云机 vendor .so 身份一致方案（App 检测面：名字 + md5）

> 目标：让云机上 **App 进程**经 `/proc/self/maps` 观测到的 `/vendor/lib64/**` 已加载 `.so`
> 集合——**路径名**与**文件字节（md5）**——与真机一致。
> 约束（用户已定）：**只求检测面一致**；不要求云机图形用与真机同一套 GL 栈、不要求真机同款渲染。

- 设备：Xiaomi Redmi Note 13 5G（gold / MT6583，Android 15）
- 云机：lineage-22.0 virtio vendor 为底（当前架构：官方 super 零改动 + vdc patch.img + 内核 getname_flags 重定向）
- 观测口径：device-checker 新检查 `proc.vendor_so` + `proc.vendor_so_md5`（+ `proc.vendor_so_all`，见 §6/§11）

---

## 1. 现状与差异证据（2026-10-08 实测）

同一 `/api/run` 同时下发真机（`07cf5fc1784783d5`）与云机（`45f7d12fa6045270-cloud`）：

| | 已加载 vendor .so | 其中 GPU 相关 |
|---|---|---|
| 真机 | 59 | 24 |
| 云机 | 35 | 22 |

- 同名交集 **20** 个 → **md5 一致仅 1 个**：`/vendor/lib64/libc++.so`；其余 19 个不同。
- 非 GPU 同名 9 个（`android.hardware.common-V2-ndk.so`、`libbase/libcutils/libdmabufheap/libhardware/libhidlbase/libutils/libz.so` 8 个不同 + `libc++.so` 相同）。
- 两机 GL 栈是两套：

| 角色 | 真机 | 云机 |
|---|---|---|
| EGL/GLES 实现 | `libGLES_mali.so`、`libGLES_meow.so`、`libMEOW_{data,gift,qt}.so` | `libEGL_meow.so`、`libGLESv1_CM_meow.so`、`libGLESv2_meow.so`（CDH interposer）、`lib{EGL,GLESv1_CM,GLESv2}_mesa.so` |
| Vulkan | `hw/vulkan.mali.so` | `hw/…mapper@4.0-impl.minigbm.so`/`libminigbm_gralloc.so` |
| gralloc/GPU 辅助 | `libgpud.so`、`libgpu_aux.so`、`libgralloc_extra|metadata.so`、`libgralloctypes_mtk.so`、`arm.graphics-V5-ndk.so`、`libpqhdrmetaparser.so` | `libdrm.so`（lineage 版）、`libgralloctypes.so`（lineage 版） |
| 公共图形库 | `android.hardware.graphics.*`（MTK 版） | `android.hardware.graphics.*`（lineage 版，**同名不同 md5**） |

---

## 2. 机制：App 看到什么由三件事决定

| 观测面 | 决定因素 | 对齐手段 |
|---|---|---|
| maps 里的 **so 路径名** | 谁 `dlopen` 了它 | EGL 走 `libEGL.so` → `${ro.hardware.egl}`/`_meow` 链；Vulkan 走 `/vendor/etc/vulkan/icd.d/*.json` 的 ICD 名；gralloc/mapper 走 `ro.hardware.gralloc` + `…mapper@4.0-impl.<x>.so`。改配置/加壳即可让它 dlopen **真机 soname** |
| so 的 **md5** | 该路径上的**文件字节** | 该路径必须是真机**同一份文件**。（纯靠内核伪造 maps 路径骗不过 md5——除非伪路径上真放了真机字节） |
| **是否出现**在 maps | 是否被 dlopen 且 map 成功 | 需要触发点（跑一次 `hardware.gles` 等会拉起 GL 的检查）；依赖缺失会让 dlopen 失败 |

**关键事实**：云机挂着的**官方 `/vendor`（gold，erofs，零改动 super）里就是真机的字节**。当前不一致，只是因为内核把 `/vendor/lib64`（含 `egl/`、`hw/`）重定向到了 lineage 的 `/mnt/vendor/lib64`。即"真机原文件"已在盘上，不用另造。

**硬约束**：真机 GL 是 MTK/Mali，依赖 MTK 内核驱动（`/dev/mali0`、ion、ged/pq）与 MTK gralloc/composer HAL；云机为 virtio-gpu。因此"名字+md5 一致"与"实际能渲染"在同一进程内很难兼得——本方案只解前者。

---

## 3. 路线对比

| 路线 | 做法 | 优点 | 风险/代价 |
|---|---|---|---|
| **A 收窄重定向** | 对 GL/DRM/Vulkan 子路径**不再重定向**，直接读官方 gold `/vendor/lib64/{egl,hw,…}` | 名字+md5 天然一致（就是同一 ROM）；改动小（内核白名单 + 构建断言） | 重定向是整套 lineage vendor 生效的前提，按路径细分易误伤；gold GL 库依赖被重定向的 lineage 公共库 → 可能符号/ABI 不匹配、dlopen 失败 |
| **B loader 壳（dlopen 注入）** | 保留 mesa/minigbm 渲染；在 CDH 的 EGL/GLES/Vulkan 壳里**额外 dlopen 真机 soname**（文件用官方 gold 原件），使其进入 maps；必要时隐藏 cdh/mesa 条目 | 不依赖 MTK 驱动真 init，**确定性**可控（我们要哪些名字就 dlopen 哪些）；与现有 `*_meow` interposer 同构，改动集中在 CDH 源 | 需保证 dlopen 成功（依赖齐全/RTLD_LAZY）；若不禁用隐藏，maps 会多出 cdh/mesa 条目（真机没有） |
| **C 整体 gold vendor 底** | 以厂商 vendor 为底包（此前已实现又回退） | 最彻底、全量一致 | 风险最大、即"gold 基座"那条线；已回退，不推荐重走 |

**推荐**：见 §10——验收若取"59/59 + 集合完全相等"（本轮用户已选此严格档），则 **B 不可行**，应走 **uid 分流（测得 App 进程的 /vendor 直供官方 gold）**。

**§4 的 B 方案仅在"只要求 GPU 关键条一致"时成立**，原因见 §10.1 的"同名 so 单进程只能映射一份"约束。

---

## 4. 推荐方案（B）详细设计

### 4.1 内容来源（md5 一致的前提）
真机那批 GPU 库的**官方原文件已在本仓库**：`devices/xiaomi/note13-5G-15/roms/gold_images_OS3.0.9.0.VNQCNXM_15.0/vendor_a/`（亦可用官方 super 内 `/vendor`，二者同源）。
- 需覆盖的真机 soname（示例，按 §1 表）：`lib64/egl/{libGLES_mali.so,libGLES_meow.so,libMEOW_data.so,libMEOW_gift.so,libMEOW_qt.so}`、`lib64/hw/vulkan.mali.so`、`lib64/{libgpud.so,libgpu_aux.so,libgralloc_extra.so,libgralloc_metadata.so,libgralloctypes_mtk.so,arm.graphics-V5-ndk.so,libpqhdrmetaparser.so}`、`lib64/hardware.graphics.*`（MTK 版）等。
- 放置位置：patch.img 内**与真机相同路径**（`/vendor/lib64/...`，经内核重定向落到 patch.img 的 `/mnt/vendor/lib64/...`）。项目已有"回填件"先例（`build/vendor/build/inject.py` 的 `STAGE_FILE` 已含 `libGLES_mali.so`/`libMEOW_*`/`libOpenCL.so`），本方案把清单扩到真机全集并**真正让它们被 dlopen**。

### 4.2 让它们"被加载"（名字一致）
在 CDH 的 GL 实现（`build/vendor/source/android.hardware.gpu/`，即 `libEGL_meow.so`/`libGLESv*_meow.so` interposer）启动路径里，除现有身份字符串伪造外，新增一步：
- 按**真机 soname 清单**逐个 `dlopen(<真机绝对路径>, RTLD_LAZY | RTLD_GLOBAL)`，把真机库拉进 maps。
- 顺序/触发：在 `eglInitialize`（或 interposer 首次被加载时）执行一次，保证任何跑过 GL 检查的 App 进程都带齐。
- 仅 dlopen 不调用其 API → 不触发 MTK 驱动依赖的实际初始化（`RTLD_LAZY` 延后符号解析），降低崩溃面。

### 4.3 Vulkan ICD（让 `vulkan.mali.so` 上线）
Vulkan loader 由 `/vendor/etc/vulkan/icd.d/*.json` 决定 ICD 库名。需确保云机该目录下的 json（来自官方 gold，未被 lineage 覆盖）指向 `vulkan.mali.so`，且该文件在 patch.img 对应路径存在（§4.1）。若 loader 只加载 ICD 而不 init，`vulkan.mali.so` 即可出现在 maps。

### 4.4 收敛"多出来的条目"（maps 只留真机集合）
B 会额外暴露 `libEGL_meow.so`/`lib*_mesa.so`/`libminigbm_gralloc.so` 等真机没有的条目。可选收敛（二选一，按需）：
- **内核 maps 过滤**：在 `fs/proc/task_mmu.c`（`show_map_vma`）按路径前缀 `SEQ_SKIP` 掉 cdh/mesa/minigbm 条目（与既有 P0-1"挂载隐藏"同思路，仅改 `/proc` 输出，不影响真实加载）。
- **不做**：若验收只要求"真机集合 ⊆ 云机集合且同名 md5 一致"，则无需隐藏。

### 4.5 同名系统库（8/19 个不同）
`libbase/libcutils/libutils/libz/libhardware/libhidlbase/libdmabufheap/android.hardware.graphics.*` 等要 md5 一致，唯一办法是**换成真机（gold）版本**。两条取法：
- 仅这几条：按 §4.1 放进 patch.img 对应路径，并让 dlopen 命中它们（与 4.2 同机制）。风险：连同其依赖链可能牵动 ABI。
- 全量：等于走路线 C，不推荐。
建议先只处理 §4.1 的 GPU 清单，系统库按需逐条追加（见 §7 验收）。

---

## 5. 备选方案（A）要点（若 B 的 dlopen 受挫）
把内核白名单（`kernel/source/fs/namei.c`）中对 `/vendor/lib64/egl`、`/vendor/lib64/hw`、`/vendor/lib64/{libGLES_mali.so,vulkan.mali.so,...}` 的**重定向条目收窄/删除**，让其落到官方 gold `/vendor`（路径名即 `/vendor/...`、字节即真机）。
- 风险：gold GL 的依赖若被重定向到 lineage 公共库，可能加载失败；需按 so 逐个验证（`ldd` 于宿主 + 云机实测 maps）。
- 与 B 的关系：A 提供"真机路径 + 真机字节"的更简捷来源，可与 B 的 dlopen 注入组合使用。

---

## 6. 验证方法（可复现）
观测工具已就绪（`core/development/device-checker/checks/java/proc/`）：
- `proc.vendor_so`：maps 中 `/vendor|/mnt/vendor` 下 .so 去重清单（GPU 单列）
- `proc.vendor_so_md5`：对同一批路径以 App UID `md5sum`
- `proc.vendor_so_all`：枚举 `/vendor/lib64`(+`egl`/`hw`) 全部 .so 并算 md5（**受 App 读取权限限制，仅能覆盖可读子集，见 §11**）

流程：
1. 真机 + 云机分别 `am start -n dev.checker/.MainActivity` 注册，且在两端先跑一次 `hardware.gles`（触发 GL 加载）。
2. 同一 `/api/run` 下发 `['proc.vendor_so','proc.vendor_so_md5']` 到两机。
3. 脚本把云机 `/mnt/vendor/*` 归一成 `/vendor/*` 后比对：**同名集合**、**逐条 md5**、**集合差**。
4. 通过标准：真机 59 条中 GPU 关键条（§4.1 清单）**名字与 md5 全一致**；可选进一步要求"真机集合 ⊆ 云机集合"。

---

## 7. 实施步骤（建议顺序）
1. **spike（先证伪/证实）**：取 3 个代表库（`libGLES_mali.so`、`vulkan.mali.so`、`libMEOW_data.so`）放进 patch.img 对应路径，写最小 dlopen 壳，验证：能进 maps、md5 与真机一致、App 不崩。← 决定 B 是否可行
2. 扩清单到 §1 全部 24 条 GPU 相关；跑 §6 验收。
3. （可选）内核 maps 过滤，收敛多余条目。
4. （可选）逐条补齐 8/19 个同名系统库。
5. 更新 `build/vendor/README.md` 与本文件"实测"章节；按 AGENTS.md 规则在 `build/doc/evidence/` 留六段式证据（log-pass/fail）。

---

## 8. 风险与边界
- **依赖/ABI**：dlopen 真机库若符号缺失会失败（`RTLD_LAZY` 可延后，但强符号仍可能失败）；真机库依赖的 MTK 公共库若被重定向到 lineage 版，可能不兼容。
- **崩溃面**：库被 dlopen 进进程但被调用即可能崩（本方案只 map 不调用，缩小风险）。
- **名字不可伪造部分**：App 若直接用 `System.loadLibrary("GLESv2")`，其解析链由 plat 决定，需确认最终落到我们的壳。
- **md5 不可伪**：只能靠真机原文件字节；任何"只改 maps 显示"的手段对 md5 无效。
- **App 侧读取边界**：App UID 只能读 `/vendor/lib64` 中 136/1684 个 .so（其余 1560 `Permission denied`）→ 全量枚举/算 md5 在 App 侧不可行（详见 §11）。
- **服务端清单内存态**：device-checker 重启后需重跑 `push_checks.py --only proc.vendor_so --only proc.vendor_so_md5`。

---

## 9. 待确认

- 验收精度：只要求 **GPU 关键条一致**，还是要求 **真机 59 条全部**（后者基本等价于路线 C）。
- 是否允许"云机可比真机多出若干条"（多出的是 mesa/minigbm/cdh），还是必须集合完全相等（需要 §4.4 内核过滤）。

> 以上两点已在 §10 定档：**59/59 全一致 + 集合完全相等**。

---

## 10. 严格验收档（59/59 + 集合完全相等）的方案修订

### 10.1 硬约束：同名 so 在一个进程内只能映射一份
Linux 进程对同一 soname/路径只会 `mmap` 一次。若被测 App 进程里已经加载了 lineage 版 `/vendor/lib64/libutils.so`，就**不可能再叠一份** gold 版（同名不同内容）。因此 §4.2 的"dlopen 注入"对 §4.5 那 8~19 个同名库**无解**。
→ 要让它们的 md5 = 真机，只能让该进程**从头就走** gold 那一份，而不是叠加。

### 10.2 关键推论：让被测 App 进程的 /vendor = 官方 gold
真机就是这个 App 在**同一个 gold ROM** 上跑。若云机上该 App 进程的 `/vendor` 也是**官方 gold（零改动 super）**，则：
- App 的依赖链、被 dlopen 的 soname 集合 → **与真机天然一致**（同 App × 同 vendor 字节）；
- 每条文件的字节 → **与真机一致**（同一 ROM 副本）→ md5 一致。

即"59/59 + 集合相等"不需要造数据，只要把**该进程**的 `/vendor` 解析从 lineage（`/mnt/vendor`）切回官方 gold。

### 10.3 推荐实现：内核重定向"uid 分流"
在 `kernel/source/fs/namei.c` 的 CDH 重定向（`getname_flags` 白名单）中加一层**按 uid 分流**：
- **App 进程（`current_fsuid() >= 10000`，即 AID_APP_START）**：命中白名单的 `/vendor/...` **不重定向** → 落回官方 gold `/vendor`（零改动 erofs）→ 名字+md5 与真机一致。**本轮范围定为"所有 app uid"**（见 §10.4）。
- **系统/root（uid<10000）**：保持现有重定向 → `/mnt/vendor`（CDH lineage vendor）→ 系统照常起（virtio HAL 等仍在）。

> 为什么不是"全局停重定向"：重定向是全进程生效的；全局停会让 system_server/init 也回退到 gold vendor，找不到 virtio HAL → 系统起不来。故必须按进程（uid）分流。
> 项目决策表早前已把"uid 分流（应用走官方、系统走 patch）"列为可行备选，本条即落地它。

### 10.4 副作用与边界
- **该 uid 的 GL 变 MTK/Mali**：`libEGL/libGLESv2` 会选官方 gold 实现，virtio 上渲染大概率失败/崩。本目标**不要求渲染**，但需确认被测 App（device-checker）自身不因 GL 崩。
- **内核只认 uid 不认包名**：App 卸载重装/清数据会变 uid → 每次实测前用 `pm list packages -U` 取当前 uid 配置；或把 uid 白名单做成启动参数。
- **"被测"范围（本轮定档：所有 app uid）**：判定式 `current_fsuid() >= 10000`。含义是**任意第三方 App** 的检测面都与真机一致。代价：
  - 所有 app uid 的 GL 都变 MTK/Mali（virtio 上渲染大概率崩）→ 第三方 App（含 Launcher 等 app uid 进程）的图形受影响；
  - SystemUI/system_server 是 `system`(1000) 等 uid<10000，**不受影响**，仍走 CDH；
  - 若后续要收敛影响面，可退回"仅指定 App 的 uid 白名单"（改判定式即可）。
- **集合是否恰为 59**：同 App × 同 ROM 应天然一致；但**App 版本必须与真机一致**（不同版本依赖不同），实测校准。
- **额外条目**：该进程只加载 gold 库时理论上无多余条目；若实测仍多出（如 CDH 注入物被 dlopen），再加 §4.4 的 `show_map_vma` 内核过滤。

### 10.5 实施顺序（严格档）
> 状态：**暂不动手**（2026-10-08 定档，待启动）。
1. **spike**：先在 `fs/namei.c` 加 uid 分流，仅对被测 App 的 uid 放开 `/vendor/lib64`（先只放 GL 子路径），重编内核 + 打包 boot，验证该 App maps 出现 `libGLES_mali.so`/`vulkan.mali.so` 且 md5 = 真机、App 不崩。← 定可行性
2. 放开该 uid 的**全部** `/vendor` 白名单（覆盖 59 条），跑 §6 双机对比，核对 59/59 + 集合相等。
3. 如有多余条目 → 加内核 maps 过滤；如 App 崩 → 缩小到"仅该 App 且不触发 GL 调用/或仅 maps 读取路径"。
4. 校准 uid 获取方式（写死 vs 运行时取）、更新 README 与证据文档。

### 10.6 验收标准（提高后）
- 被测 App 与真机**同一版本**；
- 同一次 `/api/run` 下：真机 59 条路径集合 == 云机（归一化后）路径集合；
- 逐条 md5 全等；
- 无多余条目（否则内核过滤后复验）。

---

## 11. App 侧读取权限边界（2026-10-08 实测，决定"能否枚举全量"）

### 11.1 结论：App 无法枚举/计算全量 vendor .so
真机 `/vendor/lib64`（含 `egl/`、`hw/`）共 **1684 个 .so**；以 App UID（`run-as dev.checker`）实测：

| | 数量 | 说明 |
|---|---|---|
| App **可读**（能算 md5） | **136**（lib64 顶层 124 + `egl/`6 + `hw/`6） | 见 11.2 |
| App **不可读** | **1560** | `md5sum: …: Permission denied`（SELinux/DAC，非加载问题） |

被拒示例：
```
md5sum: /vendor/lib64/vendor.xiaomi.sensor.citsensorservice@1.1.so: Permission denied
md5sum: /vendor/lib64/vendor.xiaomi.sensor.communicate@1.0.so: Permission denied
md5sum: /vendor/lib64/vpp_hdrmeta_handler_plugin.so: Permission denied
```
→ 即新增的 `proc.vendor_so_all` **拿不到全量**；它对 1560 条只能得到 `Permission denied`。

### 11.2 可读的那 136 条（= App 侧能被检测/算 md5 的全集）
特征：**图形/GPU/MTK 图形栈 + 部分小米相机插件 + RenderScript/OpenCL + 少量系统库**。代表性条目：
- GPU/图形：`egl/libGLES_mali.so`、`egl/libGLES_meow.so`、`egl/libMEOW_{data,gift,qt,trace}.so`、`hw/vulkan.mali.so`、`hw/mapper.mediatek.so`、`hw/gralloc.default.so`、`hw/android.hardware.graphics.allocator-V2-mediatek.so`、`arm.graphics-V5-ndk.so`、`arm.mali.platform-V2-ndk.so`、`libgpud.so`、`libgpu_aux.so`、`libged.so`、`libgralloc_{extra,metadata}.so`、`libgralloctypes(_mtk).so`、`libion(_mtk/_ulit).so`、`libdpframework.so`、`libpqhdrmetaparser.so`、`android.hardware.graphics.*`、`vendor.mediatek.hardware.{pq*,mms,mmagent,composer_ext}*`
- 其它：`libRS*`、`libOpenCL.so`、`libc++.so`、`libbase/libcutils/libutils/libhidlbase/libhardware/libz.so`、`libhidl{base,memory,transport}.so`、`libhwbinder.so`、`com.xiaomi.plugin.*`、`libmialgo*`、`libmorpho_*`、`libtflite_mtk.so`、`libnir_neon_driver_ndk.mtk.vndk.so` 等。

当前 App **已加载**情况（2026-10-08 实测，先跑一次 `hardware.gles` 触发 GL）：
- 136 条里 **已加载 55**，**可读但未加载 81**；
- 另有 **4 条已加载**位于 `mt6833/` 子目录（本次枚举只覆盖 `lib64` 顶层 + `egl/` + `hw/`，未含子目录）：
  `/vendor/lib64/mt6833/{libaiselector.so, libdpframework.so, libmml.so, libpq_sec.so}`；
- 合计 App 已加载 = 55 + 4 = **59**（与 §1 一致）。
→ **"还能再检测/加载"的候选 = 81 条**（均在 136 内）；完整清单见 §12。

### 11.3 对方案的影响（强化 §10 的结论）
- "App 侧枚举全量 vendor .so 并算 md5"**不可行**（上限 136）。因此"让 App 检测到的集合与 md5 和真机一致"**不能靠逐个读文件**去凑，只能按 §10.2/§10.3 **让该进程的 `/vendor` 直供官方 gold**——此时集合与字节天然一致，无需 App 去读全量。
- **全量 md5 基线不靠设备**：宿主 `roms/gold_images_…/vendor_a/` 与设备逐字节相同（已核 59/59 全等），全量 1684 条的 md5 可在**宿主**直接算/比对，作为基线清单。
- `proc.vendor_so_all` 的定位收敛为："App 视角可读的 136 条 + 明确回报 denied 数"，**不是**全量清单工具。

### 11.4 待办（可选）
- 若需要"任意 App 也能报全量 vendor 目录"：只能换主体上下文（如 system/root 进程、或宿主读取官方 super），App 侧做不到。
- 把宿主 gold ROM 全量 md5 导出为基线文件，供 §10 验收时对照。

---

## 12. 真机可读 136 条基线清单（md5 + 加载标记，2026-10-08 实测）

口径：`/vendor/lib64` + `/vendor/lib64/egl` + `/vendor/lib64/hw`（不含 `mt6833/` 等子目录）。
标记：`[✓]`＝App 已加载（55 条）；`[ ]`＝可读但未加载（81 条）。md5 为真机实测值（与宿主 gold ROM 同名文件一致）。

```text
[✓] 4bfb6cf0a8ceffa2b69ebe822d400075  /vendor/lib64/android.hardware.common-V2-ndk.so
[ ] 95216735c745710cffe08f8d8d237a49  /vendor/lib64/android.hardware.common.fmq-V1-ndk.so
[✓] 1c36db414ee465932f0189e165b07d61  /vendor/lib64/android.hardware.graphics.allocator-V2-ndk.so
[✓] 14d385bb625e22e44a980af0012eaf57  /vendor/lib64/android.hardware.graphics.common-V5-ndk.so
[✓] 43c69c6dfe096b168b60843d3ad10d83  /vendor/lib64/android.hardware.graphics.common@1.0.so
[✓] f2034d764b549bc180702ae12878d0c5  /vendor/lib64/android.hardware.graphics.common@1.1.so
[✓] a8384f17d9d1da9f7b634855a6a60372  /vendor/lib64/android.hardware.graphics.common@1.2.so
[✓] ab67799ebc09260ac23b322f359a90ed  /vendor/lib64/android.hardware.graphics.mapper@2.0.so
[✓] 9644c738c8545677e228022d3ac2830b  /vendor/lib64/android.hardware.graphics.mapper@2.1.so
[✓] f8159113011c68f7e996a8a5b98396b7  /vendor/lib64/android.hardware.graphics.mapper@3.0.so
[✓] 2c70d6eb9c5846acee78fab7f5d345d9  /vendor/lib64/android.hardware.graphics.mapper@4.0.so
[ ] 498f43d54f107ac12ed1315f23876144  /vendor/lib64/android.hardware.renderscript@1.0.so
[ ] 9d1cb9d6c8526dc013fddc0687c37f86  /vendor/lib64/android.hidl.allocator@1.0.so
[✓] cf60e83c13f2f6b9dfda1d2aa0de7d80  /vendor/lib64/android.hidl.memory.token@1.0.so
[✓] 3dd0ed1125902b600beaaad49d317285  /vendor/lib64/android.hidl.memory@1.0.so
[ ] 091e501b329e50ff54414cb18991649f  /vendor/lib64/android.hidl.safe_union@1.0.so
[✓] e5e01ecca7f3677adfbe2d2ec855165e  /vendor/lib64/arm.graphics-V5-ndk.so
[ ] f8691b0e55ac13a82f747da7aaf2d519  /vendor/lib64/arm.mali.platform-V2-ndk.so
[ ] 45e3307bfd36f40fd0de55cf57bdc07c  /vendor/lib64/com.xiaomi.plugin.capbokeh.so
[ ] 3f08ca46f7530f112ae9a1a104ac7eb2  /vendor/lib64/com.xiaomi.plugin.ldc.so
[ ] 997a29f39e2a473c22ebb12548cdace6  /vendor/lib64/com.xiaomi.plugin.memcpy.so
[ ] 21cd26a28cef9bc4072185923762d26a  /vendor/lib64/com.xiaomi.plugin.mibokeh.so
[ ] cf1c26563d085561e4603612eb719cd9  /vendor/lib64/com.xiaomi.plugin.morphohdr.so
[ ] 9a55ebcb2c1644171fdf367de8667c9c  /vendor/lib64/com.xiaomi.plugin.skinbeautifier.so
[ ] 44fed3469061cb86aeeb0b8cb9326fa2  /vendor/lib64/com.xiaomi.plugin.superlowlight.so
[ ] b9ad46d3b4e08c567852566ae8c204de  /vendor/lib64/com.xiaomi.plugin.swflip.so
[✓] a067235ebc44b2d3c66b1022207c21e5  /vendor/lib64/egl/libGLES_mali.so
[✓] e5884bc660bd75888f69209c9503325e  /vendor/lib64/egl/libGLES_meow.so
[✓] 8402fe5f0e9a881fcb6169168be885f7  /vendor/lib64/egl/libMEOW_data.so
[✓] 164bb94079397981a22f371aa5439ec1  /vendor/lib64/egl/libMEOW_gift.so
[✓] 4bdd1326e7aa751fcedf09a74f19d892  /vendor/lib64/egl/libMEOW_qt.so
[ ] 8841a47252c9c0e75f3ed045b4f5d756  /vendor/lib64/egl/libMEOW_trace.so
[ ] 521ffd5d41b8afea1b1a9fa7f6dadba2  /vendor/lib64/hw/android.hardware.graphics.allocator-V2-mediatek.so
[ ] 784068bc2e301ab97e33747df09f2826  /vendor/lib64/hw/android.hardware.renderscript@1.0-impl.so
[ ] ba2bd21828ad156c67a8fd0e984d075c  /vendor/lib64/hw/android.hidl.memory@1.0-impl.so
[ ] 67a4ce0d39220b4df9236834a253a1c6  /vendor/lib64/hw/gralloc.default.so
[✓] 38f1edc2da209058dfa7a31faae62262  /vendor/lib64/hw/mapper.mediatek.so
[✓] 36ab352cddadd3017a8efdd3a456c423  /vendor/lib64/hw/vulkan.mali.so
[✓] b1a467eec14c236182e0d14ee411ebf9  /vendor/lib64/libDefaultFpsActor.so
[✓] 197702d0c100f6bb78ef1c71a9c09238  /vendor/lib64/libFrameRecord.so
[✓] ff9e35ccb9dead36965fff27c7558273  /vendor/lib64/libNoFpsActor.so
[ ] 6282e2fe2a5a5945d38f369ebcedddc5  /vendor/lib64/libOpenCL.so
[ ] 64984ea75070aaf6dbe90b96c8c3e07e  /vendor/lib64/libRSCpuRef.so
[ ] 087333198a2877b36f1b11098e25a7cb  /vendor/lib64/libRSDriver.so
[ ] bea49f093ef8ff50fed6b8f736240691  /vendor/lib64/libRS_internal.so
[ ] 12b4722cccaccad94a638453474160f8  /vendor/lib64/libTrueSight.so
[✓] 261d754391010a3301d401b9008714cc  /vendor/lib64/libaal_sec.so
[✓] 0641d868965f4fc22c72e839f83f916e  /vendor/lib64/libaedv.so
[ ] 264d1869caad375adaad50646bdc1467  /vendor/lib64/libaiselector.so
[ ] 2883b48e357f3f6d219e51ba1841c4b0  /vendor/lib64/libanc_dc_base.so
[ ] 2a410d70fe658bb4377ab8800a09d46a  /vendor/lib64/libanc_fullhdr.so
[ ] 370d8da5d434f89288afe7c8ac9ee03a  /vendor/lib64/libanc_night.so
[ ] ac07bb9316aa32c342faed51d84350a4  /vendor/lib64/libanc_single_bokeh.so
[ ] 60ca9b75ba1766b700791925a7ad2396  /vendor/lib64/libarm_egl_properties_sysprop.so
[ ] 13e09c47a4fe8ce8d78c87508f2508cc  /vendor/lib64/libarm_gralloc_properties_sysprop.so
[ ] cb68b1a5e34e75935d66ace2d75fad1b  /vendor/lib64/libarm_mali_config_sysprops.so
[ ] 064a9ce6eeed61e7120c54c55e566f47  /vendor/lib64/libarmnn_ndk.mtk.vndk.so
[✓] 0a193c59b95859a32cb83394062ed652  /vendor/lib64/libbase.so
[ ] d79d783a3121ae9e93001e810412ee55  /vendor/lib64/libbcinfo.so
[ ] a3ab6f1275b82b697f7f7ae8de82f13c  /vendor/lib64/libblas.so
[✓] ce7f5940bb1f2b8083cb1e0ade97bea4  /vendor/lib64/libc++.so
[ ] 2424aeb2d567eeafa0c2217327e9eded  /vendor/lib64/libc++_shared.so
[ ] 3a3f81d36c6b614a7783e1534f9c7f42  /vendor/lib64/libcmdl_ndk.mtk.vndk.so
[ ] 03fc96bcbdd66432fa4c34c3131014c5  /vendor/lib64/libcom.xiaomi.metadatautils.so
[ ] ae73b8624e8c1e8b4a507a70e56320c1  /vendor/lib64/libcom.xiaomi.pluginutils.so
[ ] fe3e7c2793aa2a55467584226d9a94c3  /vendor/lib64/libcompiler_rt.so
[✓] c42688a85b7e6ca242e5b058be3cd50f  /vendor/lib64/libcutils.so
[✓] c76d24d8576f9b946621fe50606423d6  /vendor/lib64/libdmabufheap.so
[ ] d1a53835435307914cd8458f0ed8152a  /vendor/lib64/libdpframework.so
[✓] 10e2de0de029b412db1472dc905f155c  /vendor/lib64/libdrm.so
[✓] 434676053f3c4cd5d18eecabe8f225de  /vendor/lib64/libged.so
[✓] b41e0c96a5901d66273416ed58e43f92  /vendor/lib64/libgpu_aux.so
[✓] 2e136edb1609c8117a4d1d1055dc4bd4  /vendor/lib64/libgpud.so
[ ] 03276ea72fc1b5c73b1745516e735bb7  /vendor/lib64/libgpudataproducer.so
[✓] 70029ab64d9ce9ce085f73c6fa3108be  /vendor/lib64/libgralloc_extra.so
[✓] 0cbfd7bdd374a9328732e3df8268e025  /vendor/lib64/libgralloc_metadata.so
[✓] a8699368e5e3ae4b99e5f19c9620f008  /vendor/lib64/libgralloctypes.so
[✓] 8572908508b481ad8ebb81128f278324  /vendor/lib64/libgralloctypes_mtk.so
[✓] 0237872887b2f2b272fc7e5835b740a5  /vendor/lib64/libhardware.so
[ ] 302537c465d07924fa49f5369e546481  /vendor/lib64/libhdrvideo.so
[✓] d2a3830f981db7f4934b4739b13bfb33  /vendor/lib64/libhidlbase.so
[✓] 38af7923397145efa17f0977f4be5ffa  /vendor/lib64/libhidlmemory.so
[ ] f0fd301f79ac1c58bc494f346abef4f4  /vendor/lib64/libhidltransport.so
[ ] 3184595083e82fb3b32c1c64ba8890c3  /vendor/lib64/libhwbinder.so
[✓] 825fd5e6a607e1c2df44fdaadc2a0999  /vendor/lib64/libion.so
[ ] fb6efabf9c7687a126222d83bbae917f  /vendor/lib64/libion_mtk.so
[ ] 1c0168b9276653eab95f385e4cabb1ef  /vendor/lib64/libion_ulit.so
[ ] 4f3ea1e0f2854c9ae4e7423747bf26bc  /vendor/lib64/libjsoncpp.so
[✓] d502457afc9bd78edcfc3a6648c530fa  /vendor/lib64/libladder.so
[✓] 11ac58b61f5fd4bd9ca42296643589ee  /vendor/lib64/liblzma.so
[ ] f138418954d53b4a2e9a568ce5c89265  /vendor/lib64/libmialgo_ai_vision.so
[ ] e66f4c9755d57268fd615f7a133cf94a  /vendor/lib64/libmialgo_utils.so
[ ] 182f1170c8d6643f06756961fd327da5  /vendor/lib64/libmialgoengine.so
[ ] ec72e320c8d607dfca4f0ca07ca9c3e9  /vendor/lib64/libmialgoengine2.so
[ ] 50b7c7ac47dfb244364fa9ff6f44df4a  /vendor/lib64/libmml.so
[ ] b10d6a29c06ac7eded59567dc01b34fd  /vendor/lib64/libmorpho_ImageRefiner.so
[ ] 5f1c2d090236d6c3855c365c9a1c5cf2  /vendor/lib64/libmorpho_ImageRefinerLite.so
[ ] 027b3276528dfebce9bbb87de97a3d09  /vendor/lib64/libmorpho_Ldc.so
[ ] 6527143b8cecdc15cf419a8312eb9399  /vendor/lib64/libmpbase.so
[ ] c53369fde2e47056203ec42789f2bf3d  /vendor/lib64/libnir_neon_driver_ndk.mtk.vndk.so
[ ] 0292404e6c759c97b0784217ff9f0bd7  /vendor/lib64/libpng.so
[✓] 0d1232bfecbe40b438405ca61aca5cd7  /vendor/lib64/libpq_cust_base.so
[ ] 622ca89ca4df9ca817c94357918744b3  /vendor/lib64/libpq_sec.so
[✓] ccc3d89e0749c982e3314fa2cecd64b8  /vendor/lib64/libpqhdrmetaparser.so
[✓] 98c138277051a2254e8fe38882b04809  /vendor/lib64/libpqsharememory.so
[✓] 49f763ed8d6ee362f9ae6bdda2544d37  /vendor/lib64/libpqtuning.so
[ ] 722648b38096a17f1ba6bf33c3faaba8  /vendor/lib64/libprocessgroup.so
[ ] 40c635d18575bf983e4b3ce543c9d0b9  /vendor/lib64/libscltm.so
[✓] e66acb4c0a904667bd367ae795d6d5d4  /vendor/lib64/libsdrparser.so
[ ] 0ce14c174d63d914c58665f4d9f1cee4  /vendor/lib64/libtflite_mtk.so
[✓] c48283e637e82bbc4af13ee5508383b3  /vendor/lib64/libunwindstack.so
[✓] 0973ac1aa4214719c6e2cdb91c142a11  /vendor/lib64/libutils.so
[✓] 39116bbe89c4a3ff4fe002f80e92e171  /vendor/lib64/libutilscallstack.so
[ ] ce95c08731f1e2a3dda4575f8c7dc49d  /vendor/lib64/libwa_dof.so
[✓] b24c8ffd7105ac74063f4c41d4f79cf1  /vendor/lib64/libz.so
[ ] 048cd1ad19816f15e291b3b74e3ac70d  /vendor/lib64/vendor.mediatek.hardware.composer_ext-V1-ndk.so
[✓] ba0a55271e4928c489e612651d75e7d8  /vendor/lib64/vendor.mediatek.hardware.mmagent-V1-ndk.so
[ ] 9501d4dc75d2a79aad1f3ccbc4ce5987  /vendor/lib64/vendor.mediatek.hardware.mmagent@1.0.so
[ ] c9c8b7afdef68d77dfba6174d4fbaeae  /vendor/lib64/vendor.mediatek.hardware.mmagent@1.1.so
[ ] 60a1e3b5913937a51c611b79fede3df3  /vendor/lib64/vendor.mediatek.hardware.mmlpq-V3-ndk.so
[✓] 71c7e6fd1645ca32b601b13d0301a3f0  /vendor/lib64/vendor.mediatek.hardware.mms-V1-ndk.so
[ ] b3e3d32f38e2767d57827007da80b746  /vendor/lib64/vendor.mediatek.hardware.pq@2.0.so
[ ] 974b73166dac2f91369ca7172405894e  /vendor/lib64/vendor.mediatek.hardware.pq@2.1.so
[ ] c6a3f174d08197136b140e2bf0a86715  /vendor/lib64/vendor.mediatek.hardware.pq@2.10.so
[ ] 0672a6fae4711f4a81f9e13190b5110c  /vendor/lib64/vendor.mediatek.hardware.pq@2.11.so
[ ] 430cf24ef0ab8d01e419d0a96e21f435  /vendor/lib64/vendor.mediatek.hardware.pq@2.2.so
[ ] 20d261876d65c8b13d7a5b832f0fd85e  /vendor/lib64/vendor.mediatek.hardware.pq@2.3.so
[ ] 96191a745b074b30a328498384a7e647  /vendor/lib64/vendor.mediatek.hardware.pq@2.4.so
[ ] 7f24c6efff20171b6521eb1be7058366  /vendor/lib64/vendor.mediatek.hardware.pq@2.5.so
[ ] 8326a609144bb014aae22318f99f8439  /vendor/lib64/vendor.mediatek.hardware.pq@2.6.so
[ ] 59a948cd6722df6dee7aa954e8e77272  /vendor/lib64/vendor.mediatek.hardware.pq@2.7.so
[ ] 007cf42da2339a44466fa139e68a708b  /vendor/lib64/vendor.mediatek.hardware.pq@2.8.so
[ ] bcaaf7e060034e8e30a22ca50be57322  /vendor/lib64/vendor.mediatek.hardware.pq@2.9.so
[✓] 059a287527577ff214f67018967ad117  /vendor/lib64/vendor.mediatek.hardware.pq_aidl-V2-ndk.so
[ ] a910abbeb08b3859daaae81d0a5faef1  /vendor/lib64/vendor.mediatek.hardware.pq_aidl-V4-ndk.so
[ ] cc9aefdaf8e54143c9f8332867794373  /vendor/lib64/vendor.mediatek.hardware.pq_aidl-V7-ndk.so
```

另有 4 条已加载（不在上表，因在 `mt6833/` 子目录，本次未枚举）：
```text
[✓] /vendor/lib64/mt6833/libaiselector.so
[✓] /vendor/lib64/mt6833/libdpframework.so
[✓] /vendor/lib64/mt6833/libmml.so
[✓] /vendor/lib64/mt6833/libpq_sec.so
```
（其 md5 未在本轮取值；需要时把 `mt6833/` 纳入枚举即可补齐。）

---

## 13. 真机 136 ↔ 云机 72 逐条对照表（2026-10-08 实测）

口径：两侧均取 `/vendor/lib64` + `egl/` + `hw/`、**App UID 可读**集合；云机路径经内核重定向实为 `/mnt/vendor/...`（已归一化为 `/vendor/...`）。真机 136、云机 72。

**汇总**：同名 **35**（md5 一致 **7** / 不同 **28**）｜仅真机 **101**｜仅云机 **37**。

### 13.1 两机可读且同名（35）
| 路径（/vendor/lib64/…） | 真机 md5 | 云机 md5 | 判定 |
|---|---|---|---|
| android.hardware.common-V2-ndk.so | 4bfb6cf0a8ceffa2b69ebe822d400075 | c1fb15e45a8be16e579a709100a5f548 | ❌ |
| android.hardware.common.fmq-V1-ndk.so | 95216735c745710cffe08f8d8d237a49 | 316cf00106f40c5a5b668f5c61c05569 | ❌ |
| android.hardware.graphics.allocator-V2-ndk.so | 1c36db414ee465932f0189e165b07d61 | ffb913c37bc78c29ad959c57da57c2f9 | ❌ |
| android.hardware.graphics.common-V5-ndk.so | 14d385bb625e22e44a980af0012eaf57 | 8c4ab62bbe76d9b19d8ed7611713d28d | ❌ |
| android.hardware.graphics.common@1.0.so | 43c69c6dfe096b168b60843d3ad10d83 | 2ec315c2a24f2fdac4d9ed3e51e152b5 | ❌ |
| android.hardware.graphics.common@1.1.so | f2034d764b549bc180702ae12878d0c5 | b5130514db27b87b95524afc11a58904 | ❌ |
| android.hardware.graphics.common@1.2.so | a8384f17d9d1da9f7b634855a6a60372 | 615cedc12a8ea8fb4053c8b3df57edbf | ❌ |
| android.hardware.graphics.mapper@2.0.so | ab67799ebc09260ac23b322f359a90ed | c871187e8c99db94282455cf85d23b65 | ❌ |
| android.hardware.graphics.mapper@2.1.so | 9644c738c8545677e228022d3ac2830b | 513f60b3bd0064cd73f009c1c4bc7fc4 | ❌ |
| android.hardware.graphics.mapper@3.0.so | f8159113011c68f7e996a8a5b98396b7 | 7dc4714d983390dccc875d812dcf233a | ❌ |
| android.hardware.graphics.mapper@4.0.so | 2c70d6eb9c5846acee78fab7f5d345d9 | 431abcb26b0dfec044e21a2f385271a5 | ❌ |
| android.hidl.memory.token@1.0.so | cf60e83c13f2f6b9dfda1d2aa0de7d80 | 07f4e85fd17215f8fd7659005a7b9590 | ❌ |
| android.hidl.memory@1.0.so | 3dd0ed1125902b600beaaad49d317285 | e2c3166c9fde888a4295f9f848e5139b | ❌ |
| egl/libGLES_mali.so | a067235ebc44b2d3c66b1022207c21e5 | a067235ebc44b2d3c66b1022207c21e5 | ✅ |
| egl/libMEOW_data.so | 8402fe5f0e9a881fcb6169168be885f7 | 8402fe5f0e9a881fcb6169168be885f7 | ✅ |
| egl/libMEOW_gift.so | 164bb94079397981a22f371aa5439ec1 | 164bb94079397981a22f371aa5439ec1 | ✅ |
| egl/libMEOW_qt.so | 4bdd1326e7aa751fcedf09a74f19d892 | 4bdd1326e7aa751fcedf09a74f19d892 | ✅ |
| egl/libMEOW_trace.so | 8841a47252c9c0e75f3ed045b4f5d756 | 8841a47252c9c0e75f3ed045b4f5d756 | ✅ |
| hw/android.hidl.memory@1.0-impl.so | ba2bd21828ad156c67a8fd0e984d075c | f74fe2b3b118d5b256b76826f777936f | ❌ |
| hw/gralloc.default.so | 67a4ce0d39220b4df9236834a253a1c6 | f6e6478ccd6621d080809d5aa3eaeb53 | ❌ |
| hw/vulkan.mali.so | 36ab352cddadd3017a8efdd3a456c423 | 2035164e1a72b9e8a7b97612c41d513f | ❌ |
| libOpenCL.so | 6282e2fe2a5a5945d38f369ebcedddc5 | 6282e2fe2a5a5945d38f369ebcedddc5 | ✅ |
| libbase.so | 0a193c59b95859a32cb83394062ed652 | bb9b46417fb74bd3bfc41834ebcb7af8 | ❌ |
| libc++.so | ce7f5940bb1f2b8083cb1e0ade97bea4 | ce7f5940bb1f2b8083cb1e0ade97bea4 | ✅ |
| libcutils.so | c42688a85b7e6ca242e5b058be3cd50f | fb1737a2fa88868aa4dacdce6dbbcc4c | ❌ |
| libdmabufheap.so | c76d24d8576f9b946621fe50606423d6 | c33b25e0d2d1067873ed9fa91c23c7a5 | ❌ |
| libdrm.so | 10e2de0de029b412db1472dc905f155c | 306c9fd04588c1038957d3c3d21b0880 | ❌ |
| libgralloctypes.so | a8699368e5e3ae4b99e5f19c9620f008 | c4232a1509e3827bed6a5172705a8fbb | ❌ |
| libhardware.so | 0237872887b2f2b272fc7e5835b740a5 | 00d176b79d1016e903c417917aa884e7 | ❌ |
| libhidlbase.so | d2a3830f981db7f4934b4739b13bfb33 | b234c531c70ba9c53cfb29f1c139f30d | ❌ |
| libhidlmemory.so | 38af7923397145efa17f0977f4be5ffa | 299a4ccd9ea0f1e91f246c8acada8338 | ❌ |
| libion.so | 825fd5e6a607e1c2df44fdaadc2a0999 | 3f2b418053d7691ba4e46b95900abce2 | ❌ |
| libprocessgroup.so | 722648b38096a17f1ba6bf33c3faaba8 | 71d8f738c672fe0a5c1167690a3cdea8 | ❌ |
| libutils.so | 0973ac1aa4214719c6e2cdb91c142a11 | 2e631e822c01b5240fe1d59004487b34 | ❌ |
| libz.so | b24c8ffd7105ac74063f4c41d4f79cf1 | 7cf7e050be1adaa99ad21bfbc876d5bd | ❌ |

> ✅ 一致的 7 个均为 **CDH 回填的官方 gold 原件**：`libc++.so`、`egl/libGLES_mali.so`、`egl/libMEOW_{data,gift,qt,trace}.so`、`libOpenCL.so`。
> 注意 `hw/vulkan.mali.so` 云机 md5 与真机**不同**：云机该文件是 CDH 自编 Vulkan interposer，非官方原件。

### 13.2 仅真机可读（101，云机无此文件/不可读）
```text
/vendor/lib64/ 前缀，仅列 basename + md5：
android.hardware.renderscript@1.0.so            498f43d54f107ac12ed1315f23876144
android.hidl.allocator@1.0.so                   9d1cb9d6c8526dc013fddc0687c37f86
android.hidl.safe_union@1.0.so                  091e501b329e50ff54414cb18991649f
arm.graphics-V5-ndk.so                          e5e01ecca7f3677adfbe2d2ec855165e
arm.mali.platform-V2-ndk.so                     f8691b0e55ac13a82f747da7aaf2d519
com.xiaomi.plugin.capbokeh.so                   45e3307bfd36f40fd0de55cf57bdc07c
com.xiaomi.plugin.ldc.so                        3f08ca46f7530f112ae9a1a104ac7eb2
com.xiaomi.plugin.memcpy.so                     997a29f39e2a473c22ebb12548cdace6
com.xiaomi.plugin.mibokeh.so                    21cd26a28cef9bc4072185923762d26a
com.xiaomi.plugin.morphohdr.so                  cf1c26563d085561e4603612eb719cd9
com.xiaomi.plugin.skinbeautifier.so             9a55ebcb2c1644171fdf367de8667c9c
com.xiaomi.plugin.superlowlight.so              44fed3469061cb86aeeb0b8cb9326fa2
com.xiaomi.plugin.swflip.so                     b9ad46d3b4e08c567852566ae8c204de
egl/libGLES_meow.so                             e5884bc660bd75888f69209c9503325e
hw/android.hardware.graphics.allocator-V2-mediatek.so  521ffd5d41b8afea1b1a9fa7f6dadba2
hw/android.hardware.renderscript@1.0-impl.so    784068bc2e301ab97e33747df09f2826
hw/mapper.mediatek.so                           38f1edc2da209058dfa7a31faae62262
libDefaultFpsActor.so                           b1a467eec14c236182e0d14ee411ebf9
libFrameRecord.so                               197702d0c100f6bb78ef1c71a9c09238
libNoFpsActor.so                                ff9e35ccb9dead36965fff27c7558273
libRSCpuRef.so                                  64984ea75070aaf6dbe90b96c8c3e07e
libRSDriver.so                                  087333198a2877b36f1b11098e25a7cb
libRS_internal.so                               bea49f093ef8ff50fed6b8f736240691
libTrueSight.so                                 12b4722cccaccad94a638453474160f8
libaal_sec.so                                   261d754391010a3301d401b9008714cc
libaedv.so                                      0641d868965f4fc22c72e839f83f916e
libaiselector.so                                264d1869caad375adaad50646bdc1467
libanc_dc_base.so                               2883b48e357f3f6d219e51ba1841c4b0
libanc_fullhdr.so                               2a410d70fe658bb4377ab8800a09d46a
libanc_night.so                                 370d8da5d434f89288afe7c8ac9ee03a
libanc_single_bokeh.so                          ac07bb9316aa32c342faed51d84350a4
libarm_egl_properties_sysprop.so                60ca9b75ba1766b700791925a7ad2396
libarm_gralloc_properties_sysprop.so            13e09c47a4fe8ce8d78c87508f2508cc
libarm_mali_config_sysprops.so                  cb68b1a5e34e75935d66ace2d75fad1b
libarmnn_ndk.mtk.vndk.so                        064a9ce6eeed61e7120c54c55e566f47
libbcinfo.so                                    d79d783a3121ae9e93001e810412ee55
libblas.so                                      a3ab6f1275b82b697f7f7ae8de82f13c
libc++_shared.so                                2424aeb2d567eeafa0c2217327e9eded
libcmdl_ndk.mtk.vndk.so                         3a3f81d36c6b614a7783e1534f9c7f42
libcom.xiaomi.metadatautils.so                  03fc96bcbdd66432fa4c34c3131014c5
libcom.xiaomi.pluginutils.so                    ae73b8624e8c1e8b4a507a70e56320c1
libcompiler_rt.so                               fe3e7c2793aa2a55467584226d9a94c3
libdpframework.so                               d1a53835435307914cd8458f0ed8152a
libged.so                                       434676053f3c4cd5d18eecabe8f225de
libgpu_aux.so                                   b41e0c96a5901d66273416ed58e43f92
libgpud.so                                      2e136edb1609c8117a4d1d1055dc4bd4
libgpudataproducer.so                           03276ea72fc1b5c73b1745516e735bb7
libgralloc_extra.so                             70029ab64d9ce9ce085f73c6fa3108be
libgralloc_metadata.so                          0cbfd7bdd374a9328732e3df8268e025
libgralloctypes_mtk.so                          8572908508b481ad8ebb81128f278324
libhdrvideo.so                                  302537c465d07924fa49f5369e546481
libhidltransport.so                             f0fd301f79ac1c58bc494f346abef4f4
libhwbinder.so                                  3184595083e82fb3b32c1c64ba8890c3
libion_mtk.so                                   fb6efabf9c7687a126222d83bbae917f
libion_ulit.so                                  1c0168b9276653eab95f385e4cabb1ef
libjsoncpp.so                                   4f3ea1e0f2854c9ae4e7423747bf26bc
libladder.so                                    d502457afc9bd78edcfc3a6648c530fa
liblzma.so                                      11ac58b61f5fd4bd9ca42296643589ee
libmialgo_ai_vision.so                          f138418954d53b4a2e9a568ce5c89265
libmialgo_utils.so                              e66f4c9755d57268fd615f7a133cf94a
libmialgoengine.so                              182f1170c8d6643f06756961fd327da5
libmialgoengine2.so                             ec72e320c8d607dfca4f0ca07ca9c3e9
libmml.so                                       50b7c7ac47dfb244364fa9ff6f44df4a
libmorpho_ImageRefiner.so                       b10d6a29c06ac7eded59567dc01b34fd
libmorpho_ImageRefinerLite.so                   5f1c2d090236d6c3855c365c9a1c5cf2
libmorpho_Ldc.so                                027b3276528dfebce9bbb87de97a3d09
libmpbase.so                                    6527143b8cecdc15cf419a8312eb9399
libnir_neon_driver_ndk.mtk.vndk.so              c53369fde2e47056203ec42789f2bf3d
libpng.so                                       0292404e6c759c97b0784217ff9f0bd7
libpq_cust_base.so                              0d1232bfecbe40b438405ca61aca5cd7
libpq_sec.so                                    622ca89ca4df9ca817c94357918744b3
libpqhdrmetaparser.so                           ccc3d89e0749c982e3314fa2cecd64b8
libpqsharememory.so                             98c138277051a2254e8fe38882b04809
libpqtuning.so                                  49f763ed8d6ee362f9ae6bdda2544d37
libscltm.so                                     40c635d18575bf983e4b3ce543c9d0b9
libsdrparser.so                                 e66acb4c0a904667bd367ae795d6d5d4
libtflite_mtk.so                                0ce14c174d63d914c58665f4d9f1cee4
libunwindstack.so                               c48283e637e82bbc4af13ee5508383b3
libutilscallstack.so                            39116bbe89c4a3ff4fe002f80e92e171
libwa_dof.so                                    ce95c08731f1e2a3dda4575f8c7dc49d
vendor.mediatek.hardware.composer_ext-V1-ndk.so 048cd1ad19816f15e291b3b74e3ac70d
vendor.mediatek.hardware.mmagent-V1-ndk.so      ba0a55271e4928c489e612651d75e7d8
vendor.mediatek.hardware.mmagent@1.0.so         9501d4dc75d2a79aad1f3ccbc4ce5987
vendor.mediatek.hardware.mmagent@1.1.so         c9c8b7afdef68d77dfba6174d4fbaeae
vendor.mediatek.hardware.mmlpq-V3-ndk.so        60a1e3b5913937a51c611b79fede3df3
vendor.mediatek.hardware.mms-V1-ndk.so          71c7e6fd1645ca32b601b13d0301a3f0
vendor.mediatek.hardware.pq@2.0.so              b3e3d32f38e2767d57827007da80b746
vendor.mediatek.hardware.pq@2.1.so              974b73166dac2f91369ca7172405894e
vendor.mediatek.hardware.pq@2.10.so             c6a3f174d08197136b140e2bf0a86715
vendor.mediatek.hardware.pq@2.11.so             0672a6fae4711f4a81f9e13190b5110c
vendor.mediatek.hardware.pq@2.2.so              430cf24ef0ab8d01e419d0a96e21f435
vendor.mediatek.hardware.pq@2.3.so              20d261876d65c8b13d7a5b832f0fd85e
vendor.mediatek.hardware.pq@2.4.so              96191a745b074b30a328498384a7e647
vendor.mediatek.hardware.pq@2.5.so              7f24c6efff20171b6521eb1be7058366
vendor.mediatek.hardware.pq@2.6.so              8326a609144bb014aae22318f99f8439
vendor.mediatek.hardware.pq@2.7.so              59a948cd6722df6dee7aa954e8e77272
vendor.mediatek.hardware.pq@2.8.so              007cf42da2339a44466fa139e68a708b
vendor.mediatek.hardware.pq@2.9.so              bcaaf7e060034e8e30a22ca50be57322
vendor.mediatek.hardware.pq_aidl-V2-ndk.so      059a287527577ff214f67018967ad117
vendor.mediatek.hardware.pq_aidl-V4-ndk.so      a910abbeb08b3859daaae81d0a5faef1
vendor.mediatek.hardware.pq_aidl-V7-ndk.so      cc9aefdaf8e54143c9f8332867794373
```

### 13.3 仅云机可读（37，真机无此文件/不可读）
```text
/vendor/lib64/ 前缀，仅列 basename + md5：
android.hardware.graphics.allocator@2.0.so      8d540c71698f2ff7776a813dfce4789b
android.hardware.graphics.allocator@3.0.so      c896525c95f2be4cf9031e98bcb7c18b
android.hardware.graphics.allocator@4.0.so      400a0f6a4d047ad2122341ec1e2bd355
android.hardware.graphics.bufferqueue@2.0.so    8a5dd533ffabb323a6797dce992004c9
android.hardware.media.bufferpool2-V2-ndk.so    1f9e100908ec70c6f820f0c28aaf6ad0
android.hardware.media.bufferpool@2.0.so        b07ca2a68decf8481fc444b475ef7faf
android.hardware.media.c2-V1-ndk.so             07b873d8a00f487870720d57d3722b59
egl/libEGL_meow.so                              61da6842e154904b90d6102b3db3b6ef
egl/libGLESv1_CM_meow.so                        20e0b4a186d0c3d37dd780610e4bd440
egl/libGLESv1_CM_mesa.so                        410c5dda8ea83d04bd40f9c183c451fd
egl/libGLESv2_meow.so                           591994c6cee073bd77df70ec9c3cd9dc
egl/libGLESv2_mesa.so                           f334323999f51bdba3bba249f48bb25e
hw/android.hardware.graphics.mapper@4.0-impl.minigbm.so  7835bb91a09992f9e050b4e9428f068e
hw/gralloc.minigbm.so                           254eadf6acae0a9ce86e5306e4f90308
hw/mapper.minigbm.so                            f3b8b90244036e8b9bbbb02b1505a6a6
hw/vulkan.pastel.so                             9d7badfacc60da4fbbbb8ea9bb1f1766
hw/vulkan.virtio.so                             279930f04439843094ae5694389f7cc2
libaconfig_storage_read_api_cc.so               d94eaa07dc64fb17d5f620f0a0a0a9e7
libbinder.so                                    f228a63fd938260423731522ca9b0b24
libc2plugin_store.so                            c32ffa189b0373ced691c92e57973849
libchrome.so                                    45fe3a17b39136a3a1ed9263e5c3fb12
libcodec2.so                                    23e8a5bc779752b28c13e2308bfd091f
libcodec2_aidl.so                               09e211374164317a66096d8023adb139
libcodec2_hal_common.so                         a21949628077a2c8d6a482d837c6bf2f
libcodec2_vndk.so                               301e004ecc4bcd54b89be282fb41640d
libevent.so                                     b38df5d83396c1163f3b523599675a2c
libfmq.so                                       1382ae833dbc20a6375dcc18b8460d57
libgallium_dri.so                               2a70d8071cb8b414391f45a0aec467db
libglapi.so                                     051e8391a7ea34c2a8424f659c1476cf
libminigbm_gralloc.so                           e8e79b4bd2645b36d6d0eaa75b01038b
libminigbm_gralloc4_utils.so                    aaa06e5759f5afc1c196daae9ca9ae87
libsfplugin_ccodec_utils.so                     267b1f0207cc93cd3f7e813f13db5c9c
libstagefright_aidl_bufferpool2.so              49705113add7864f1b48232e25e55a7d
libstagefright_bufferpool@2.0.1.so              085bb47b70f71df108c153d68c2f142a
libstagefright_foundation.so                    6aef0bf68bdc6fd9783fab53e9c9c82f
libui.so                                        49d2dca3bd3d839ab3949295816045e9
server_configurable_flags.so                    22d395879b78be3227bece49d5242db2
```

> 注："仅真机 / 仅云机"包含两种情形：该侧**无此文件**，或该侧 App **不可读**。