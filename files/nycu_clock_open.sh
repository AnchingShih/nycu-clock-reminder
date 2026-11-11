#!/bin/zsh
# 打卡提醒開頁：在校外就開 FortiClient 等你連上，連通後自動開簽到頁
# （連線、手機驗證、點簽到由本人操作）
LABEL="${1:-打卡}"
URL="https://portal.nycu.edu.tw/#/redirect/timeclocksign"
ACTIONS="view, 1 開VPN, shortcuts://run-shortcut?name=NYCU-Clock; view, 2 簽到頁, $URL"
# 在校內或已連 VPN 時，對外 IP 會是學校網段（140.113 交大、140.129 陽明）
reachable() { local ip=$(curl -s -m 5 https://api.ipify.org); [[ $ip == 140.113.* || $ip == 140.129.* ]] }
TOPIC="$(cat ~/Scripts/.nycu_ntfy_topic 2>/dev/null)"
VPN="$(cat ~/Scripts/.nycu_vpn_name 2>/dev/null)"; VPN="${VPN:-nycu}"     # FortiClient 裡的 VPN 名稱
# 只有「VPN 沒連上」要跳通知。用 alerter（借終端機的名義，系統有允許）；osascript 的通知會被系統擋掉
warn() {
  "$HOME/Scripts/bin/alerter" --group nycu-info --sender com.apple.Terminal --sound Glass \
    --title "$1" --message "$2" --close-label 好 --timeout 600 >/dev/null 2>&1 &!
}

if ! reachable; then
  # 從選單列 FortiClient 圖示點「Connect to <VPN 名稱>」（使用 FortiClient 已儲存的密碼）
  osascript -e "tell application \"System Events\" to tell process \"FortiTray\"
    set mi to menu bar item 1 of menu bar 1
    click mi
    delay 1
    try
      click menu item \"Connect to $VPN\" of menu 1 of mi
    on error
      key code 53
    end try
  end tell" >/dev/null 2>&1 || open -a FortiClient
  for i in {1..100}; do          # 最多等 5 分鐘
    sleep 3
    reachable && break
  done
  reachable || { warn "NYCU VPN 沒連上" "等了 5 分鐘還沒連上 VPN，${LABEL}頁沒有打開。請手動連 VPN 後再開${LABEL}頁"; exit 1; }
fi
open -a "Google Chrome" "$URL"
