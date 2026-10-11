# Issue: v8 模拟 Wi-Fi 连接不稳定（BEACON-LOSS + 30s 规律性断开）—— 解决方案

- 设备: Xiaomi Redmi Note 13 5G (note13-5G-15)
- 宿主: macOS (darwin, QEMU + hvf)
- 状态: **已解决**（2026-09-22，内核 recipe v23）：guest 内 Wi-Fi（mac80211_hwsim 假扫描/假连接 +
  数据面内核直桥 hvc10）在 CDH-A 上长时间稳定连接，大流量下不再断开。

---

## 1. 问题现象（qemu.log / logcat 证据链）

```
# 现象 A：Wi-Fi 列表为空（v20 之前）
cmd wifi list-scan-results → No scan results

# 现象 B：连接后每 ~30-35s 规律性断开重连（v22 时仍存在，用户反馈「大量网络请求时 wifi 频繁断开」）
wpa_supplicant: wlan0: CTRL-EVENT-DISCONNECTED bssid=02:00:00:00:00:11 reason=4 locally_generated=1
IpClient/wlan0: interfaceLinkStateChanged: ifindex 16 down

# 现象 C：偶发 BEACON-LOSS（v22 修复前）
wpa_supplicant: wlan0: CTRL-EVENT-BEACON-LOSS

# 现象 D：数据面字节流失步，DHCP 拿不到地址（v21 修复前）
cdh-wifi: hvc 帧过长 4294967295，重连      # 回包被 N_TTY 吞字节 → 长度读成 0xFFFFFFFF
cmd wifi status → IP: null                 # DHCP ACK 被破坏
```

## 2. 根因（四个独立问题，按修复顺序）

| # | 事实 | 结论 | 修复 |
|---|---|---|---|
| 1 | hwsim 默认 ops（`mac80211_hwsim_ops`，channels=1/mlo=0）**无 `.hw_scan`**，mac80211 必走软件扫描；原注入点在 `hw_scan_work`，永不执行 | 假扫描从未注入 → 列表空 | v20：注入改到 `sw_scan_start`（`hwsim_fake_inject_sw_scan`，按各假 AP 自身频点一次性注入） |
| 2 | 数据面 `/dev/hvc10` 直接改 `tty->termios`，**未走 `tty_set_termios`** → N_TTY 的 char_map/raw 未重建，接收仍逐字节特殊字符处理，帧内 `0x00/0x0a/0x11/0x13` 被吞 | 回包字节流失步 → 帧长读成 `0xFFFFFFFF` → 数据面瘫、DHCP 失败；另 `filp_open` 被 SELinux 拒（-13） | v21：改用 `tty_set_termios`（触发 `n_tty_set_termios`，`ldata->raw=1` 走 `n_tty_receive_buf_raw`）；补 hvc10 独立标签 `cdh_wifi_data` + `allow kernel` 读写授权 |
| 3 | 假连接后**无持续 beacon**（仅在扫描时注入一次）；mac80211 关联后需周期 beacon，supplicant 收不到 → BEACON-LOSS | 大流量时（扫描繁忙）表现为频繁断开 | v22：assoc 成功后按关联 BSSID **每 100ms 注入一次 beacon**（`delayed_work`，`cdh_bridge_beacon_*`），disassoc/deauth 时停止 |
| 4 | nullfunc keepalive 发往假 AP（`02:00:00:00:00:11`）后 hwsim 广播给其它 radio **无目标匹配 → `ack=false`** → mac80211 `last_ack` 永不更新 → 每 `IEEE80211_CONNECTION_IDLE_TIME`(30s) 触发 `ieee80211_connection_loss` | **每 ~30-35s 一次 `reason=4 locally_generated` 主动断开重连**（规律性，非随机） | v23：`mac80211_hwsim_tx` 对**发往假 AP 的其余帧**（nullfunc/keepalive）伪造 `TX_STAT_ACK` 并走 `ieee80211_tx_status_irqsafe`；附带让速率控制获得样本，link speed 从 1Mbps 升至 18Mbps（消除 MIUI NetworkAccelerateSwitchService 对 1Mbps 的「链路差」判定） |

> 现象 B 与 3 的关联：v22 修掉 BEACON-LOSS（现象 C）后，30s 断开（现象 B）仍在——因为 30s 周期来自
> **连接监控的 last_ack**（管理帧 ack），与 beacon（last_beacon）无关；v23 伪造 ack 后 30s 周期消失。

## 3. 落地方案（内核 mac80211_hwsim 改造，全部已提交）

源码：`kernel/source/drivers/net/wireless/virtual/mac80211_hwsim.c`（commit b3a1ccf / d29d601 /
d771cfeb / fbf2191 / 85b463b，detached HEAD；`kernel/build/build-kernel.sh` build_recipe v20→v23）。

### 3.1 假扫描注入改到软件扫描路径（v20）

```c
static void hwsim_fake_inject_sw_scan(struct mac80211_hwsim_data *hwsim)
// 遍历 scan_profile 假 AP：按 ap->frequency 取频道，构造 beacon 注入 mac80211_hwsim_rx
// 调用点：mac80211_hwsim_sw_scan()（sw_scan_start）末尾
```

### 3.2 数据面 tty raw 改走 tty_set_termios + SELinux 授权（v21）

```c
new = tty->termios;
new.c_iflag &= ~(IGNBRK|BRKINT|PARMRK|ISTRIP|INLCR|IGNCR|ICRNL|IXON);
new.c_oflag &= ~OPOST;
new.c_lflag &= ~(ECHO|ECHONL|ICANON|ISIG|IEXTEN);
new.c_cflag &= ~(CSIZE|PARENB);
new.c_cflag |= CS8;
tty_set_termios(tty, &new);   // 触发 ldisc n_tty_set_termios，重建 raw
```

- SELinux（`build/vendor/build/sepolicy.py`）：`/dev/hvc10 → cdh_wifi_data`（dev_type）+
  `(allow kernel cdh_wifi_data (chr_file (getattr ioctl open read write)))`；
  `vfc_missing_hal_lines` 一并补齐该字面标签。

### 3.3 周期 beacon 注入维持假连接（v22）

```c
// assoc 成功后：
cdh_bridge_beacon_start(data, req->bssid);   // 存 hwsim + assoc_bssid，调度 delayed_work
// work 每 100ms：hwsim_fake_find(assoc_bssid) → 按自身频点构造 beacon →
//   mac80211_hwsim_rx() 注入；model 里无该 AP 时停止
// disassoc/deauth（hwsim_fake_tx_handled 拦截）时：cdh_bridge_beacon_stop()
```

### 3.4 假 AP 帧伪造 ack（v23，核心修复）

```c
// mac80211_hwsim_tx() 中，hwsim_fake_tx_handled 之后：
if (hwsim_fake_find(hdr->addr1, NULL)) {          // 发往假 AP 的 nullfunc/keepalive 等
    ieee80211_tx_info_clear_status(txi);
    txi->control.rates[0].count = 1;
    txi->control.rates[1].idx = -1;
    if (!(txi->flags & IEEE80211_TX_CTL_NO_ACK))
        txi->flags |= IEEE80211_TX_STAT_ACK;
    ieee80211_tx_status_irqsafe(hw, skb);
    return;
}
```

## 4. 验证结果（QEMU 实机 qemu.log / cmd wifi）

| # | 验证点 | 修复前 | 修复后（v23） |
|---|---|---|---|
| 1 | 扫描列表 | No scan results | VirtWifi + CDH-A/B/C（host 下发 4 AP） |
| 2 | 连接 | 每 ~30-35s reason=4 断开重连 | 连续 3+ 分钟 **0 次断开** |
| 3 | BEACON-LOSS | 偶发 CTRL-EVENT-BEACON-LOSS | 0 次 |
| 4 | 数据面 | 帧过长 0xFFFFFFFF 重连、DHCP IP=null | drop=0 reset=0、DHCP 192.168.60.100 |
| 5 | Link speed | 1Mbps（MIUI 判链路差） | 18Mbps |
| 6 | 出网 | 不通 | DNS + TCP direct（SYN-ACK 已回） |

## 5. 遗留 / 下一步

1. **CDH-B/C 连接切换未验**：`cmd wifi connect-network CDH-B` 被框架 autojoin 拉回信号最强的 CDH-A；
   host 网关 `currentAP` 也不随 guest 切换（仍按 CDH-A 子网应答 DHCP → IP 与网络不匹配）。
   需 host 侧在指定 CDH-B 时同步 `connect <ssid>` 行协议，或实现 guest 上报 AP 身份。socks5 出口待此打通后验收。
2. link speed 上限 18Mbps（minstrel 用管理帧样本升级到 2.4G legacy 上限）；如需更高显示，可在
   假 beacon/assoc 响应带 HT capabilities IE 让 STA 进入 HT 模式。
3. 内核改动均在内核仓 detached HEAD（未推分支）；后续合入正式分支时需按仓库规范整理提交。
