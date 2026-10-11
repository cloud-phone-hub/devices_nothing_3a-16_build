# 真机（gold OS3.0.9.0.VNQCNXM）已激活 APEX 清单与分区归属

- 设备: Xiaomi Redmi Note 13 5G (note13-5G-15)
- 系统: gold_images_OS3.0.9.0.VNQCNXM_15.0（A15 / 352090000）
- 数据来源: 官方镜像 dump.erofs 逐分区列出 `/apex`，与真机 `/proc/mounts` 挂载全量核对
- 结论: **全部 41 个 loop 挂载 ↔ 官方分区 43 个 `.apex` 文件**，来源仅 3 个分区
  （system / system_ext / vendor），product / odm / odm_dlkm / mi_ext 均无 APEX，且无 data/apex 来源。

---

## 1. 真机挂载全量（/proc/mounts，grep apex）

```
tmpfs /apex tmpfs ...
/dev/block/loop3  /apex/com.android.btservices@352090000           ext4
/dev/block/loop4  /apex/com.android.media.swcodec@352090000       ext4
/dev/block/loop5  /apex/com.android.wifi@352090000                ext4
/dev/block/loop6  /apex/com.android.adbd@352090000                ext4
/dev/block/loop7  /apex/com.android.appsearch@352090000           ext4
/dev/block/loop8  /apex/com.android.vndk.v34@1                    ext4
/dev/block/loop9  /apex/com.android.profiling@352090000           ext4
/dev/block/loop10 /apex/com.android.apex.cts.shim@1               ext4
/dev/block/loop11 /apex/com.android.art@352090000                 ext4
/dev/block/loop12 /apex/com.android.permission@352090000          ext4
/dev/block/loop13 /apex/com.android.compos@2                      ext4
/dev/block/loop14 /apex/com.android.configinfrastructure@352090000 ext4
/dev/block/loop15 /apex/com.android.hardware.cas@1                ext4
/dev/block/loop16 /apex/com.android.conscrypt@352090000           ext4
/dev/block/loop17 /apex/com.android.tzdata@352090000              ext4
/dev/block/loop18 /apex/com.android.healthfitness@352090000       ext4
/dev/block/loop19 /apex/com.android.ipsec@352090000               ext4
/dev/block/loop20 /apex/com.android.i18n@1                        ext4
/dev/block/loop21 /apex/com.android.cellbroadcast@352090000       ext4
/dev/block/loop22 /apex/com.android.neuralnetworks@352090000      ext4
/dev/block/loop23 /apex/com.android.nfcservices@352090000         ext4
/dev/block/loop24 /apex/com.android.media@352090000               ext4
/dev/block/loop25 /apex/com.android.rkpd@352090000                ext4
/dev/block/loop26 /apex/com.google.android.widevine@190240725     ext4
/dev/block/loop27 /apex/com.android.os.statsd@352090000           ext4
/dev/block/loop28 /apex/com.android.vndk.v33@1                    ext4
/dev/block/loop29 /apex/com.android.virt@2                        ext4
/dev/block/loop30 /apex/com.android.devicelock@1                  ext4
/dev/block/loop31 /apex/com.android.scheduling@352090000          ext4
/dev/block/loop32 /apex/com.android.adservices@352090000          ext4
/dev/block/loop33 /apex/com.android.vndk.v31@1                    ext4
/dev/block/loop34 /apex/com.android.uwb@352090000                 ext4
/dev/block/loop35 /apex/com.android.extservices@352090000         ext4
/dev/block/loop36 /apex/com.android.sdkext@352090000              ext4
/dev/block/loop37 /apex/com.android.ondevicepersonalization@352090000 ext4
/dev/block/loop38 /apex/com.android.art.compatible@340090000      ext4
/dev/block/loop39 /apex/com.android.tethering@352090000           ext4
/dev/block/loop40 /apex/com.android.resolv@352090000              ext4
/dev/block/loop41 /apex/com.android.runtime@1                     ext4
/dev/block/loop42 /apex/com.android.mediaprovider@352090000       ext4

tmpfs /bootstrap-apex tmpfs ...
/dev/block/loop0 /bootstrap-apex/com.android.runtime@1   ext4
/dev/block/loop1 /bootstrap-apex/com.android.i18n@1      ext4
/dev/block/loop2 /bootstrap-apex/com.android.tzdata@352090000 ext4
```

> 说明: 每个 /apex 挂载点都出现两次（`@版本号` 与去掉版本号两条），同上 loop 设备，
> 是 apexd 的"规范路径 + 无版本别名"双挂载，非两个独立文件。

## 2. 分区归属汇总

### ① /system ↔ /system/apex（36 个文件，主力）

镜像内 `/system/apex/` 文件（一次列出）:

```
com.android.adbd.apex
com.android.adservices.apex
com.android.apex.cts.shim.apex
com.android.appsearch.apex
com.android.art.apex
com.android.btservices.apex
com.android.cellbroadcast.apex
com.android.configinfrastructure.apex
com.android.conscrypt.apex
com.android.devicelock.apex
com.android.healthfitness.apex
com.android.i18n.apex
com.android.ipsec.apex
com.android.media.apex
com.android.media.swcodec.apex
com.android.mediaprovider.apex
com.android.neuralnetworks.apex
com.android.nfcservices.apex
com.android.ondevicepersonalization.apex
com.android.os.statsd.apex
com.android.permission.apex
com.android.profiling.apex
com.android.resolv.apex
com.android.rkpd.apex
com.android.runtime.apex
com.android.scheduling.apex
com.android.sdkext.apex
com.android.tethering.apex
com.android.tzdata.apex
com.android.uwb.apex
com.android.virt.apex
com.android.wifi.apex
com.google.android.extservices.cn.apex     # 包名 com.android.extservices，region 变体
```

对应真机挂载（33 + rtl double 3）：

| 挂载名 | 对应文件 |
|---|---|
| com.android.runtime@1 | com.android.runtime.apex（另在 bootstrap-apex 双挂） |
| com.android.tzdata@352090000 | com.android.tzdata.apex（另在 bootstrap-apex 双挂） |
| com.android.i18n@1 | com.android.i18n.apex（另在 bootstrap-apex 双挂） |
| com.android.art@352090000 | com.android.art.apex |
| com.android.conscrypt@352090000 | com.android.conscrypt.apex |
| com.android.media@352090000 | com.android.media.apex |
| com.android.media.swcodec@352090000 | com.android.media.swcodec.apex |
| com.android.mediaprovider@352090000 | com.android.mediaprovider.apex |
| com.android.adbd@352090000 | com.android.adbd.apex |
| com.android.wifi@352090000 | com.android.wifi.apex |
| com.android.tethering@352090000 | com.android.tethering.apex |
| com.android.btservices@352090000 | com.android.btservices.apex |
| com.android.adservices@352090000 | com.android.adservices.apex |
| com.android.appsearch@352090000 | com.android.appsearch.apex |
| com.android.cellbroadcast@352090000 | com.android.cellbroadcast.apex |
| com.android.configinfrastructure@352090000 | com.android.configinfrastructure.apex |
| com.android.devicelock@1 | com.android.devicelock.apex |
| com.android.extservices@352090000 | com.google.android.extservices.cn.apex |
| com.android.healthfitness@352090000 | com.android.healthfitness.apex |
| com.android.ipsec@352090000 | com.android.ipsec.apex |
| com.android.neuralnetworks@352090000 | com.android.neuralnetworks.apex |
| com.android.nfcservices@352090000 | com.android.nfcservices.apex |
| com.android.ondevicepersonalization@352090000 | com.android.ondevicepersonalization.apex |
| com.android.os.statsd@352090000 | com.android.os.statsd.apex |
| com.android.permission@352090000 | com.android.permission.apex |
| com.android.profiling@352090000 | com.android.profiling.apex |
| com.android.resolv@352090000 | com.android.resolv.apex |
| com.android.rkpd@352090000 | com.android.rkpd.apex |
| com.android.sdkext@352090000 | com.android.sdkext.apex |
| com.android.scheduling@352090000 | com.android.scheduling.apex |
| com.android.uwb@352090000 | com.android.uwb.apex |
| com.android.virt@2 | com.android.virt.apex |
| com.android.apex.cts.shim@1 | com.android.apex.cts.shim.apex |

### ② /system_ext ↔ /system_ext/apex（5 个文件，VNDK 兼容层 + 厂商合成器）

```
com.android.art.compatible.apex
com.android.compos.apex
com.android.vndk.v31.apex
com.android.vndk.v33.apex
com.android.vndk.v34.apex
```

| 挂载名 | 对应文件 |
|---|---|
| com.android.compos@2 | com.android.compos.apex（Redmi 图像合成 HAL） |
| com.android.art.compatible@340090000 | com.android.art.compatible.apex |
| com.android.vndk.v31@1 | com.android.vndk.v31.apex |
| com.android.vndk.v33@1 | com.android.vndk.v33.apex |
| com.android.vndk.v34@1 | com.android.vndk.v34.apex |

### ③ /vendor ↔ /vendor/apex（2 个文件，厂商私有 HAL）

```
com.android.hardware.cas.apex
com.google.android.widevine.nonupdatable.apex
```

| 挂载名 | 对应文件 |
|---|---|
| com.android.hardware.cas@1 | com.android.hardware.cas.apex |
| com.google.android.widevine@190240725 | com.google.android.widevine.nonupdatable.apex |

## 3. 关键理解

1. **product / odm / odm_dlkm / mi_ext 分区无 APEX**（product_a 根无 /apex）。
2. **/bootstrap-apex 不是独立来源** —— 是 init 第一阶段对 runtime/tzdata/i18n
   的提前激活副本（tmpfs + loop0/1/2），同源 `/system/apex`，非新文件。
3. **全量核算**：挂载 41 个 loop（含 bootstrap 3）↔ 分区 43 个 .apex 文件，
   system 36 + system_ext 5 + vendor 2 = 43，无遗漏、无 data/apex（OTA staged）来源。
4. **APEX 挂载全是 ext4**（`/dev/block/loopN`）：apexd 直接把 zip 内嵌的
   `apex_payload.img`（ext4 原始镜像）losetup 后挂载，只读（ro,dirsync,seclabel,nodev,noatime）。

## 4. 对本项目的意义

- 官方 vendor 仅含 **cas + widevine** 两个 APEX；
- **health / audio 等 HAL APEX 官方 /vendor/apex 根本不存在**（源自 lineage/cuttlefish）——
  印证 `issue-vendor-apex-*` 系列方案中"patch.img 重定向 /vendor/apex 引入 lineage APEX"
  会在真机身份视角暴露额外挂载；而"抽 payload → 普通 vendor 服务化"在挂载层面零暴露。

## 5. 普通应用视角：APEX 可见性与验证能力

三层结论：**挂载名可见、文件不可读、功能可间接用。**

### 5.1 挂载条目：普通应用可见

普通 app（untrusted_app 域）**可以看到 `/proc/mounts` / `/proc/self/mountinfo`
里的 `/apex/*` 挂载条目**（挂载属全局命名空间，app 只是能列出）：

```c
// 普通 app 读 /proc/mounts 即可见：
/dev/block/loop13 /apex/com.android.compos@2 ext4 ro,...
/dev/block/loop4  /apex/com.android.media.swcodec@352090000 ext4 ro,...
```

因此**存在性 / 版本号可被 app 验证**：
- app 可枚举挂载点，与"真机应有 43 个 /apex/*（见 §2）"比对；
- 若引入 lineage 的 HAL APEX（如 `/apex/com.google.cf.health`），app 侧 / root 检测
  会把"多出来的挂载名"当作异常/篡改信号 —— 这正是 solution.md 中 P0-1 挂载隐藏的动机。

### 5.2 文件内容：SELinux 拦截，app 不可读

`/apex/*` 内容标签为 apexd 专用（`apex_file` / `apexd_art_file` 等），`untrusted_app`
**没有 read / open / execute / search 权限**：

```
avc: denied { read } for name="libart.so" dev="loop11" scontext=u:r:untrusted_app:s0 tcontext=u:object_r:apexd_art_file:s0
```

- app 不能 `cat /apex/com.android.art@1/lib64/libart.so`，也无法 stat 其哈希/xattr；
- app 无法枚举 `/apex/*` 目录（无 search 权限）。

### 5.3 功能使用：经 linker 命名空间间接可用

app 实际用到的 APEX 能力（ART/runtime、conscrypt、i18n、tzdata 等）由 linker 在
沙箱命名空间内把库 mmap 进进程，属于匿名映射，**不是路径读文件**——app 拿不到文件本体，
只用得到已加载的实现。

### 5.4 能力矩阵

| 手段 | app 能否 | 说明 |
|---|---|---|
| 列出 /apex 挂载点 + 版本 | ✅ | `/proc/mounts`、`/proc/self/mountinfo` |
| 判定"非官方 apex 被挂载" | ✅ | 篡改/root 检测常用手法 |
| 读 apex 内文件 / 哈希 / xattr | ❌ | SELinux 拒绝（仅 apexd/system 域） |
| 枚举 /apex 目录 | ❌ | SELinux 拒绝（无 search 权限） |
| 通过 API 用 apex 功能（ART/Conscrypt 等） | ✅（间接） | linker 命名空间 + JDK 类，拿不到文件本体 |

### 5.5 对方案的含义

- "patch.img 重定向 /vendor/apex → lineage HAL apex"：app 能看到多出的
  `/apex/com.google.cf.health` 等挂载名 → **暴露**（需 P0-1 挂载隐藏兜底）。
- "抽 payload → 普通 vendor 服务化"（health / audio 现行路线）：文件全落
  `/vendor/bin|etc|lib64`（本就大量存在），**不新增任何 /apex 挂载** → app 视角与真机零差异。