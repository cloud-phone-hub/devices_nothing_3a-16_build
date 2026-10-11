# Issue: 蜂窝数据框架不发起 SetupDataCall（数据开关正常、PS 域已注册，数据却永远不上来）—— 解决方案

- 设备: Xiaomi Redmi Note 13 5G (note13-5G-15)
- 宿主: macOS (darwin, QEMU + hvf)
- 状态: **已解决**（2026-09-28）：guest 冷启动后框架自动发起 SetupDataCall，ccmni2 取到
  `100.64.155.15/24`，ConnectivityService 默认网络 = `MOBILE[LTE] CONNECTED extra: 3gnet`
  且 `IS_VALIDATED`，业务流量经宿主 `modem-data-gateway` 转出（direct 出口实测；
  socks5 出口现场实测通过）。

---

## 1. 问题现象与证据链

移动数据开关与注册都"看起来正常"，但框架永不建立数据连接：

```
# guest 设置/注册均正常
settings get global mobile_data            -> 1
dumpsys telephony.registry                 -> mServiceState: PS WWAN registrationState=HOME
                                              accessNetworkTechnology=LTE availableServices=[DATA,MMS]
                                              maxDataCalls=16

# 但数据状态恒为关/断，且 ConnectivityService 无默认网络
mUserMobileDataState=false  mIsDataEnabled=false  mDataConnectionState=0
dumpsys connectivity                       -> Active default network: none

# 宿主 modem 控制器日志（controller.log）除 hello 握手中没有过任何数据呼叫
[modem] setupDataCall ...                  -> 无（问题期）
```

同时发现 shell 触发开关失败（见 §5 遗留问题 2）：

```
adb shell svc data enable
cmd: Failure calling service phone: Failed transaction (2147483646)
```

---

## 2. 根因（AOSP15：数据开关 / 默认数据卡都是"按 subId 持久化的 Settings"，gold /data 镜像缺键）

把 radio buffer 抓全后（此前只看到 `E/CapRIL`/`E/SmsRIL` 重试日志，实际数据栈日志也在 radio buffer），
电话进程内数据栈**完全健在**，只是从初始化起就被判定"用户关闭了移动数据"：

```
D/DSMGR-0: DataSettingsManager created.
D/DSMGR-0: mIsDataEnabled=false, prevDataEnabled=false
D/DSMGR-0: notifyDataEnabledChanged: enabled=false, reason=USER, callingPackage=com.android.phone
D/DNC-0:   onDataEnabledChanged: enabled=false
D/DPM-0:   Found 2 data profiles.   （只有 EIMS/IMS，无 default APN）
D/MtkDPM:  DPM-0: isApnQueryable: ignore sim not ready no operator numeric
（注册+carrier config 到位后 default APN 正常选出）
D/DNC-0:   initiateInternetDataConnectionState apnsetting = [ApnSetting] 沃宽带用户连接互联网 … 3gnet …
D/TelephonyNetworkFactory[0]: onNeedNetworkFor [NetworkRequest … id=144 …] shouldApply false
D/DNC-0:   Re-evaluating 0 unsatisfied network requests in 0 groups, due to DATA_PROFILES_CHANGED
```

对照 AOSP15 源码（android15-release，本机用 googlesource 拉取核对）：

- `DataSettingsManager.isUserDataEnabled()`：
  ```java
  if (Settings.Global.getInt(mResolver, Settings.Global.DEVICE_PROVISIONED, 0) == 0) return isProvisioningDataEnabled();
  boolean defaultVal = TelephonyProperties.mobile_data().orElse(true);   // = ro.com.android.mobiledata
  return GlobalSettingsHelper.getBoolean(mPhone.getContext(), Settings.Global.MOBILE_DATA, mSubId, defaultVal);
  ```
  而 `GlobalSettingsHelper.getSettingName()`：
  ```java
  if (TelephonyManager.from(context).getSimCount() > 1 && SubscriptionManager.isValidSubscriptionId(subId))
      return settingName + subId;      // 双卡机型 = mobile_data<subId>
  ```
  本机 DSDS、subId=1 → 键为 **`mobile_data1`**；缺省值取 **`ro.com.android.mobiledata`（gold 真机与
  cloud 机都是 false）**。键缺失 → 默认 false → `mIsDataEnabled=false`。
- 默认数据卡：`SubscriptionManagerService` 的 `mDefaultDataSubId` **构造时**读
  `Settings.Global.MULTI_SIM_DATA_CALL_SUBSCRIPTION`（= `multi_sim_data_call`），缺省 INVALID →
  `PhoneSwitcher.phoneIdForRequest()` 对无 specifier 的请求（CS 的 INTERNET 请求都没有 specifier）
  返回 `INVALID_PHONE_INDEX` → `shouldApplyNetworkRequest()` 返回 false → 请求**永远不会**交给
  `DataNetworkController` → "0 unsatisfied network requests" → 框架不发起 SetupDataCall。
- gold 真机 /data 中存在 `mobile_data0=1, mobile_data1=1, multi_sim_data_call=1`（用户首次开数据/
  首次开机流程写入）；cloud 机的 gold /data 镜像没有这三键，直接命中上述默认分支。
- 三个值都**只在电话进程初始化时读取一次**（DSMGR 的 SettingsObserver 只观察 provisioning 两个键；
  SMS 的 mDefaultDataSubId 构造时读一次）→ 运行期 `settings put` 不会自动生效，必须让电话进程重读。

---

## 3. 修复（device 构建层，随 patch.img 下发）

`build/vendor/patch/bin/setup_display.sh`（`on property:sys.boot_completed=1` 由 root 执行的 oneshot
`auto_provision`）新增一段幂等补齐：

```sh
seeded=0
if [ "$(settings get global multi_sim_data_call)" = "null" ]; then
    settings put global multi_sim_data_call 1 2>/dev/null && seeded=1
fi
for k in mobile_data0 mobile_data1; do
    if [ "$(settings get global $k)" = "null" ]; then
        settings put global $k 1 2>/dev/null && seeded=1
    fi
done
if [ "$seeded" = "1" ]; then
    kill -9 "$(pidof com.android.phone)"     # 持久进程由 AMS 自动拉起，重读设置
    ...
fi
```

构建落盘（patch.img 内容来自 vendor.img，须先跑 vendor 再跑 patch）：

```
python3 build/main.py --only vendor      # build/vendor/patch 注入 vendor.img
python3 build/main.py --only patch        # rdump vendor.img -> out/dist/rom/patch.img
# 安装到实例盘（pack-roms 的 patch.img 步骤等价物，原子替换避免影响在跑的 /mi_ext 挂载）：
cp -p out/dist/rom/patch.img out/device/note13-5g/15/os3.0.9.0.vnqcnxm/patch.img.new && mv ...
```

校验：`debugfs -R "stat /vendor/bin/setup_display.sh" patch.img` → Size 2659（含修复），旧版 1098。

---

## 4. 验证（删键模拟全新 /data，冷启动，无人工干预）

```
# 播种类：三键均由脚本写入
settings get global multi_sim_data_call / mobile_data1 / mobile_data0 -> 1 / 1 / 1

# guest 框架状态
mUserMobileDataState=true  mIsDataEnabled=true  mDataConnectionState=2   # CONNECTED

# 宿主 modem 控制器（真实数据呼叫）
[modem] setupDataCall slot=0 cid=1 apn="3gnet" protocol="IPV4V6" -> status=0 dc=id=1 state=2
        iface="ccmni2" addrs=[100.64.155.15/24] dns=[100.64.155.3] gw=[100.64.155.2] mtu=1400

# ConnectivityService
Active default network: 100
NetworkAgentInfo … ni{MOBILE[LTE] CONNECTED extra: 3gnet} … VALIDATED …
lp{InterfaceName: ccmni2 LinkAddresses:[100.64.155.15/24] DnsAddresses:[/100.64.155.3]
   Routes:[0.0.0.0/0 -> 100.64.155.2 ccmni2 mtu 1400, 100.64.155.0/24 -> 0.0.0.0 ccmni2 mtu 0]}

# 宿主网关（direct 出口）真实业务流量
[rx] 52:54:00:12:34:58 -> 02:cd:5a:11:00:07 type=0x0800 …
[udp] 新会话 10.0.2.15:62871 → 114.114.114.114:53
[tcp] SYN 10.0.2.15:47828 → 198.18.0.67:443 / 出口 direct 已连 … 已回 SYN-ACK

# socks5 出口现场验证（宿主自建最小 socks5 服务端 /tmp/socks5srv）：
#   modem-data.json backend=socks5://127.0.0.1:1080 → 实例重启 → socks5 服务端日志：
CONNECT 198.18.0.19:443 / 198.18.0.28:443 ×4 / 198.18.0.22:80 / 198.18.0.67:443 / 198.18.0.66:443
#   （验证后已恢复 backend="" 的 direct 配置并重启实例回到默认状态）
```

---

## 5. 遗留 / 已知问题（另开 issue）

1. **guest 内 reboot 后 adb 失联**（本次两次复现）：串口日志显示 `adbd started`
   （`cph_start_adbd.rc` 于 boot_completed 拉起），但 hostfwd 5911→5555 握手失败、设备长期 offline；
   靠 daemon stop/start 恢复。疑似 adbd 与 netmgr `adb_net` 指向主 netns 的启动时序。
2. **phone 进程（`u:r:radio:s0`）拒收"带 fd 的 binder 事务"**：`dumpsys phone` / `svc …` /
   `cmd <phone 服务>` 全部 FAILED_TRANSACTION，内核日志：
   `binder: <pid>:<tid> translate fd failed` +
   `binder: <pid>:<tid> transaction call to <phone pid>:0 failed …/-1`；
   不带 fd 的事务正常（content query、dumpsys 其它服务可用）。影响 shell 观测/触发手段
   （如 `svc data enable`），本次改用 `settings put` + 电话进程重启 / `device_provisioned 0→1` 绕过。
3. `multi_sim_data_call` 与 `mobile_data<slot>` 是"按 subId/双卡形态"的键：本机单卡（slot0 → subId=1）
   播种 `mobile_data1` + `multi_sim_data_call=1`；若后续引入第二张卡/不同 subId 分配，需同步扩展脚本。
4. 运行期给 `modem-data-gateway` 换进程不可行：QEMU `-netdev stream,server=on` 在首个客户端断开后
   不再向新客户端转发帧（实测新网关 rx=0 恒定）→ 网关必须由 daemon 在实例启动时拉起。