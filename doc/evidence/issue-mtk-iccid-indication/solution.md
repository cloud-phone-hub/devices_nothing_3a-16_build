# Issue: MTK radioex SIM 指示（iccidChanged）oneway 语义不匹配 —— 解决方案

- 设备: Xiaomi Redmi Note 13 5G (note13-5G-15, MTK MT6833, MIUI 15 / gold ROM OS3.0.9.0.VNQCNXM)
- 宿主: macOS (darwin, QEMU + hvf)
- 状态: **已解决**（2026-09-26）：自研 radio HAL 的 mtkradioex SIM indication 按真机语义
  全部改为 oneway 后，MTK 框架 `MtkUiccController.mIccid[]` 正常写入
  （`EVENT_ICCID_CHANGED_IND, index: 0/1`），云机 SIM 与订阅行为与真机一致；
  调试用落盘日志（DbgLog）与配套 SELinux 授权已同步收敛移除。

---

## 1. 问题现象（修复前证据链）

```
# HAL 侧落盘日志（DbgLog → /data/misc/radio/cdh-radio.log，已随收敛移除）：
mtkex sim slot 0 setResponseFunctionsMtk response=1 indication=1
mtkex sim slot 0 iccidChanged(89860100000000059105) -> exc=-129
mtkex sim slot 1 setResponseFunctionsMtk response=1 indication=1
mtkex sim slot 1 iccidChanged(N/A) -> exc=-129

# -129 可稳定复现：kill -9 $(pidof com.android.phone) 触发 MtkRIL 重握手，
# 新一轮 setResponseFunctionsMtk 后仍是 exc=-129（两槽一致）。
```

`-129 = EX_TRANSACTION_FAILED`，由 NDK 生成代码统一映射（`AStatus_fromStatus`）。

## 2. 根因（按发现顺序）

| # | 事实 | 结论 |
|---|---|---|
| 1 | ROM（gold）`IMtkRadioExSimIndication$Stub$Proxy.iccidChanged` 字节码为 `transact(2, data, null, 1)`：reply 传 null、第 4 参 = 1 = `FLAG_ONEWAY` | 真机接口是 **oneway**：只发不收，无 reply |
| 2 | ROM `$Stub.onTransact` 的 15 个业务 case 全部直接 `return true`，**无 `writeNoException()`**（仅特殊码 16777214/16777215 写回） | 服务端确实不写 reply |
| 3 | 我们的 AIDL 原声明为双向方法 → NDK 生成代码在 `AIBinder_transact` 后执行 `AParcel_readStatusHeader` 读空 reply 失败 → `_aidl_error` 映射为 -129 | 空 reply 解析失败 = -129 的唯一来源 |
| 4 | 已排除：事务码错位（ROM Proxy 实测 1..15 与本地 AIDL 逐一一致）、`FLAG_PRIVATE_LOCAL`/stability、SELinux 域内 `hal_radio`→radio 的 binder 拒绝、`setResponseFunctionsMtk` 反序列化（服务端可正常进入方法体） | 差异只在 **oneway 语义**，其余全对齐 |

## 3. 落地方案

AIDL（`build/vendor/source/android.hardware.radio/aidl/mtk/vendor/mediatek/hardware/mtkradioex/sim/IMtkRadioExSimIndication.aidl`）：

```aidl
// 15 个方法全部按真机声明为 oneway（方法顺序 = ROM transaction code 顺序，不变）
oneway void iccidChanged(int slotId, String iccid);
...
```

生成代码随之变为 `AIBinder_transact(..., FLAG_ONEWAY)` 且不再读 reply；
HAL 侧 `RadioMtkExSim::NotifyIccid()` 简化为「发完即返回」（去掉了调试期的异常码日志）。

调试代码收敛（同批完成）：`DbgLog`（radio_sim.cpp/radio_service.h/radio_mtkex_sim.cpp 全部调用点）
与 `build/vendor/build/sepolicy.py` 中 `radio_core_data_file` 的临时写授权一并移除，
重建后核对注入 CIL 已无该授权、guest 内 `cdh-radio.log` 不再产生。

## 4. 验证结果（新构建，实例 36）

| # | 验证点 | 修复前 | 修复后 |
|---|---|---|---|
| 1 | HAL 调用结果 | exc=-129（稳定复现） | exc=0（oneway 投递成功） |
| 2 | ROM RILJ（`logcat -b radio`） | 无 | `[UNSL]< MTK: RIL_UNSOL_ICCID_CHANGED 898601000[****] [PHONE0]` / `... N/A [PHONE1]` |
| 3 | MtkUiccController | 无 | `Received EVENT_ICCID_CHANGED_IND, index: 0/1`（mIccid[] 已写入） |
| 4 | 框架 SIM 状态 | gsm.sim.state 无卡 | `gsm.sim.state=LOADED,ABSENT`、`gsm.sim.operator.alpha=中国联通`、`numeric=46001` |
| 5 | 订阅 | — | `mDefaultPhoneId=0 / mDefaultSubId=1`；siminfo 单行（icc_id 89860100000000059105、number +8613215555910） |
| 6 | 服务状态 | — | `notifyServiceStateForSubscriber: subId=1 phoneId=0 ... IN_SERVICE`，LTE 中国联通 460/01，availableServices=[VOICE,SMS,VIDEO] / [DATA,MMS]，rRplmn=46001；phoneId=1（空槽）OUT_OF_SERVICE |
| 7 | 调试残留 | DbgLog 落盘 | 收敛构建后 `/data/misc/radio/cdh-radio.log` 不再产生；CIL 无 `radio_core_data_file` 授权 |

## 5. 取证要点（后续复用）

- HAL 在 `hal_radio_default` 域无 logd 权限（logcat 看不到其日志），调试期用落盘
  `/data/misc/radio/cdh-radio.log`（radio 组可写，需临时 SELinux 授权）。
- **MTK 框架 telephony 日志在 radio 缓冲区**：`Rlog.d` 写 `LOG_ID_RADIO`，
  必须 `adb shell logcat -b radio -d`（默认 `logcat -d` 看不到 RILJ / MtkUiccCtrl）。
- `persist.vendor.log.tel_dbg` 只影响个别 MTK 类（MtkImsPhoneCallTracker / MtkAdnRecordCache /
  MtkUsimPhoneBookManager），RILJ 日志无需该属性。
- 复现手法：root 下 `kill -9 $(pidof com.android.phone)` → MtkRIL 重握手 →
  HAL 在 `setResponseFunctionsMtk` 重发 iccidChanged（幂等，可反复触发）。

## 6. 遗留 / 下一步

1. HIDL V3_0 `vendor.mediatek.hardware.mtkradioex`（另一条 `iccidChanged` 入口）本云机不实现；
   SmsRIL 侧有 `getMtkRadioExSmsProxy getServiceV3_0: NoSuchElementException` 降级日志，
   基础 SIM/SMS 不受影响（走标准 `android.hardware.radio.*` + 本 AIDL 桩）。
2. 其余 14 个 indication 方法本次一并按真机 oneway 对齐，尚未逐一构造场景实测
   （如需用到，可直接按 `logcat -b radio` 复核）。