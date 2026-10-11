# Issue: 云机 adb shell / app 看不到 ccmni2（ADB 被关在 adb_net 私有 netns）—— 解决方案

- 设备: Xiaomi Redmi Note 13 5G (note13-5G-15, MTK, MIUI 15 / gold ROM OS3.0.9.0.VNQCNXM)
- 宿主: macOS (darwin, QEMU + hvf)
- 状态: **已解决**（2026-09-26）：管理网卡（net0）不再移入 adb_net 私有命名空间，
  adbd 与 adb shell 回到主 netns，与真机一致——普通 app / shell 都能看到
  `ccmni2`/`ccmni0`/`wlan0` 等网卡；顺带修掉两个连带问题（adbd 属性竞态、
  Android fwmark 规则导致管理通道回程不通）。

---

## 1. 问题现象（修复前证据链）

```
# 直观现象：adb shell 看不到蜂窝网卡（真机 adb shell 能看到 ccmni0..ccmni20）
$ adb shell ip -o link | cut -d: -f2
lo tunl0 gre0 ... eth0                      # 只有管理网卡，无 ccmni2
$ adb shell readlink /proc/self/ns/net
net:[4026532671]                            # adb shell 在 adb_net

# 对照：框架进程与 app 都在主 netns（ccmni2 本来就在主 netns）
init / system_server / netd / com.android.phone → net:[4026531840]
root 通道 ip -o link → lo dummy0 ifb0 ifb1 ccmni2 ... ccmni0 wlan0   # ccmni2 在主 netns
```

结论：`ccmni2` 从未进过私有命名空间，是 **B 方案为了隔离管理网卡（net0），把
adbd 放进了 adb_net**，而 `adb shell` 的 netns 跟随 adbd → shell 落在私有 ns，
于是"看不到"。

## 2. 根因（含两个连带坑）

| # | 事实 | 结论 |
|---|---|---|
| 1 | netmgr 把 eth0（net0 SLIRP 10.0.2.0/24）移入 adb_net，guest-daemon fork adbd 前 `setns(/mnt/run/adb_net)` | adbd 与 adb shell 都被关进私有 ns；app 不受影响（故只有 shell 看不到） |
| 2 | 要把 adbd 放回主 ns，管理网卡必须同在主 ns（adbd 监听 10.0.2.15:5555）；而 guest 的 EthernetService 名字过滤器是 `(usb\|eth)\d+`（`dumpsys ethernet`），eth0 会被框架接管成"以太网网络" | 管理网卡留在主 ns 需改名（非 eth\*），改名 mgmt0 |
| 3 | **坑 1**：adbd 只在**启动时**读 `service.adb.tcp.port` 决定是否开 TCP 监听；原 rc 在 `on boot` 设置，而 daemon 在 ~6.9s 就 fork 了 adbd（AOSP 的 `on property:... → restart adbd` 只作用于 init 的 adbd 服务） | adbd 起来时属性尚未设置 → 永不监听 5555 → 宿主 adb 一直 **offline**（guest 侧 `/proc/net/tcp6` 无 5555 监听） |
| 4 | **坑 2**：Android 主 netns 的 fwmark 规则把无 mark 的本地出包统一导向默认网络表：`31000: from all fwmark 0x0/0xffff iif lo lookup wlan0`；`ip route get 10.0.2.2` → `via 192.168.60.1 dev wlan0 table wlan0` | 入包到 mgmt0、**SYN-ACK 却从 wlan0 出去**，三次握手完不成：guest 侧连接停在 `03 SYN_RECV` + 重传，宿主 adb 报 offline |
| 5 | 补 `ip rule add to 10.0.2.0/24 lookup main pref 15000` 可修（手工验证 adb 立即恢复），但**开机过程中会被 netd 重刷规则清掉** | 该方案不可持久化；改用内核自管的 **local 表**（规则 0 最先匹配，wifi/蜂窝抖动实测存活） |

> 旧方案的 adb_net 之所以"能用"，正因为它绕开了主 netns 的全部策略路由；
> 代价就是 shell 与真机视角不一致（本 issue 要修的正是这一点）。

## 3. 落地方案（全部在 note13 设备构建层）

### 3.1 netmgr：管理网卡留在主 netns（`build/vendor/patch/bin/netmgr_setup` + `etc/init/netmgr.rc`）

- 删除 bootstrap/keeper/finalize/configure-in-netns 全套；`/mnt/run/adb_net` 改为
  指向 **init 主 netns**（`ln -sf /proc/1/ns/net /mnt/run/adb_net`）——guest-daemon
  的 `setns` 逻辑零改动，落点即主 netns。
- `configure`：把 net0 网卡（MAC `52:54:00:12:34:56`，QEMU 首块网卡默认 MAC）改名
  `mgmt0`，配 `10.0.2.15/24`，**不配默认路由**，并补回程路由：

```sh
ip route del 10.0.2.0/24 dev mgmt0 table local 2>/dev/null || true
ip route add 10.0.2.0/24 dev mgmt0 table local     # 内核自管表，netd 不清
```

### 3.2 ADB 属性竞态（`build/vendor/patch/etc/init/cph_start_adbd.rc`）

```rc
on early-init            # 原为 on boot（晚于 daemon fork adbd）
    setprop service.adb.tcp.port 5555
```

### 3.3 host 侧命名与注释（`core/host/daemon`）

`NetnsAdbd` → `AdbHostFwd`（语义变为"平台 adbd + net0 hostfwd"，不再是 netns 架构），
涉及 device.go / qemu.go / run.go；行为不变（hostfwd `<ADBPort>`→5555 照旧）。

### 3.4 ccmni 数据口全集（`fingerprint` 分支）

真机 `ip -o link` 有 `ccmni0..ccmni20` 共 21 个 MTK 数据口，补 dummy 占位对齐
（`ccmni2` 是 net2 改名的真实承载网卡，跳过）：

```sh
index=1
while [ "${index}" -le 20 ]; do
    if [ "${index}" != "2" ] && [ ! -d "/sys/class/net/ccmni${index}" ]; then
        /system/bin/ip link add "ccmni${index}" type dummy
    fi
    index=$((index + 1))
done
```

| 接口（真机） | 真机 flags/mtu/state | 云机实现 |
|---|---|---|
| ccmni0 | NOARP,UP,LOWER_UP / 1400 / UNKNOWN，inet6 240e::/64 + fe80 | dummy，同形（IPv6 为静态 240e::/64） |
| ccmni1、ccmni3..20 | NOARP / 1500 / DOWN（qdisc noop） | dummy，同 mtu/state |
| ccmni2 | NOARP / 1500 / DOWN（数据未激活时） | **真实 virtio 网卡**（net2 改名，UP/1400；netd 按 DataCall 配置，偏差保留） |

残余指纹差异（dummy 固有，真机是 MTK 内核 netdev）：云机多 `BROADCAST` 标志、
`link/ether <随机 MAC>`（真机 `link/none` 无 L2 地址）、qdisc `noqueue`（真机 `mq`）。

## 4. 验证结果（冷启动，无人工干预）

| # | 验证点 | 修复前 | 修复后 |
|---|---|---|---|
| 1 | 宿主 adb | `device offline`（TCP 通、握手不成） | **device** |
| 2 | `adb shell` netns | `net:[4026532671]`（adb_net） | `net:[4026531840]`（主 netns） |
| 3 | `adb shell ip -o link` | 仅 eth0 + 隧道 | `lo dummy0 ifb0 ifb1 mgmt0 ccmni2 ... wlan0 wlan1 hwsim0 ccmni0` |
| 4 | 管理通道回程 | `ip route get 10.0.2.2` → `dev wlan0`；guest 侧 `SYN_RECV` | `10.0.2.2 dev mgmt0 table local src 10.0.2.15` |
| 5 | EthernetService | — | `dumpsys ethernet` 无任何接口被接管（`IP Configurations:` 空） |
| 6 | 默认网络 | — | wlan0 `192.168.60.100/24` 正常；local 表路由在 `svc wifi disable/enable` 抖动后仍存活 |
| 7 | 回归 | — | `gsm.sim.state=LOADED,ABSENT`、`中国联通`、`mDefaultSubId=1` 均不变 |
| 8 | ccmni 全集 | `adb shell` 只见 ccmni0/ccmni2 | `ip -o link \| grep -c ccmni` = **21**（ccmni0..ccmni20）；ccmni1/3..20 = `NOARP / mtu 1500 / DOWN`；ccmni0 = `NOARP,UP,LOWER_UP / 1400` + `240e::/64` + `fe80::`；框架未因此新增任何网络 |

## 5. 取证要点（后续复用）

- `adb shell` 的 netns = adbd 的 netns：判"shell 看不到某网卡"先比
  `readlink /proc/self/ns/net` 与 `/proc/1/ns/net`。
- `/sys/class/net` 是**跨 netns** 的全局列表（会误导）；`ip -o link` 只看当前 netns。
- 宿主 adb `offline` 且 QEMU 端口 ESTABLISHED ⇒ QEMU 已 accept、guest 侧没接住：
  先查 guest `/proc/net/tcp6 | grep 15B3`（5555）：无 LISTEN ⇒ adbd 没开 TCP；
  有 `03 SYN_RECV` ⇒ 回程路由问题（`ip route get <SLIRP 侧地址>` 复现）。
- MTK/框架侧日志在 radio 缓冲区（`logcat -b radio`），HAL 侧日志需落盘（见
  issue-mtk-iccid-indication/solution.md）。