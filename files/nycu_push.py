#!/usr/bin/env python3
"""ntfy 推播共用函式。
主頻道 TOPIC：手機訂閱，收提醒。指令頻道 CMD：手機按鈕回報給 Mac（手機不訂閱，不會跳通知）。
MAC 頻道：設定頁關掉「手機跳通知」時，原本要給手機的訊息改送這裡；手機不訂閱，只有 Mac 收聽（簽退計時照常）。
CLI：nycu_push.py test               推一則測試通知（標明是測試）
     nycu_push.py signin <第幾段>     立刻推「該簽到了」
     nycu_push.py signout             立刻推「該簽退了」（測試用）
"""
import json, os, sys, urllib.request

S = os.path.expanduser("~/Scripts")
TOPIC = open(f"{S}/.nycu_ntfy_topic").read().strip()
CMD = TOPIC + "-cmd"
MAC = TOPIC + "-mac"
URL = "https://portal.nycu.edu.tw/#/redirect/timeclocksign"
DEFAULTS = {"first_prompt": "09:00", "hours": 4, "break_min": 60, "sessions_per_day": 2, "days_per_week": 5,
            "workdays": [0, 1, 2, 3, 4, 5], "snooze_in_min": 60, "snooze_out_min": 30,
            "phone_pause_button": True, "phone_notify": True, "phone_sync": True, "renotify_min": 30, "test_minutes": 0}

def load_config():
    """設定檔 nycu_config.json（設定頁面會改它）；缺的欄位用預設值"""
    try: c = json.load(open(f"{S}/nycu_config.json"))
    except Exception: c = {}
    return {**DEFAULTS, **c}

CFG = load_config()
TEST = f"{CFG['test_minutes']}m" if CFG["test_minutes"] else None   # 測試模式：倒數改成幾分鐘
H = f"{CFG['hours']:g}"
VPN_PAGE = "shortcuts://run-shortcut?name=NYCU-Clock"   # 捷徑：沒連 VPN 才連 → 開 Authenticator → 開簽到頁

def post(m, force=False):
    """送 ntfy。手機通知關閉時（每次即時讀設定），主頻道的訊息改送 MAC 頻道；force=True（測試通知）一律送手機"""
    if not force and m.get("topic") == TOPIC and not load_config().get("phone_notify", True):
        m = {**m, "topic": MAC}
    req = urllib.request.Request("https://ntfy.sh/", data=json.dumps(m, ensure_ascii=False).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=10))

def scheduled_with_title(prefix):
    """ntfy 上還沒送出、標題以 prefix 開頭的排程訊息（主頻道與 MAC 頻道），依排入順序"""
    import time
    with urllib.request.urlopen(f"https://ntfy.sh/{TOPIC},{MAC}/json?poll=1&scheduled=1&since=all", timeout=10) as r:
        ms = [json.loads(l) for l in r if l.strip()]
    return [m for m in ms if m.get("event") == "message" and m["time"] > time.time()
            and m.get("title", "").startswith(prefix)]

def scheduled_signouts(): return scheduled_with_title("NYCU 該簽退了")
def scheduled_signins(): return scheduled_with_title("NYCU 該簽到了")

def delete_msg(m):
    req = urllib.request.Request(f"https://ntfy.sh/{m['topic']}/{m['id']}", method="DELETE")
    urllib.request.urlopen(req, timeout=10)

def cancel_signouts(keep_id=None):
    """取消排好的簽退提醒（keep_id 那一個留著）。同一時間只保留一個提醒"""
    ids = []
    for m in scheduled_signouts():
        if m["id"] == keep_id: continue
        req = urllib.request.Request(f"https://ntfy.sh/{m['topic']}/{m['id']}", method="DELETE")
        urllib.request.urlopen(req, timeout=10); ids.append(m["id"])
    if ids:   # 記下來，背景程式就不會在取消後又把它當成新的簽退提醒
        import nycu_flow
        with nycu_flow.State() as st:
            st["cancelled"] = (st.get("cancelled", []) + ids)[-50:]
    return len(ids)

def http_btn(label, msg):
    # 按鈕裡包的下一則訊息用最精簡的 JSON，整則推播才不會超過 iPhone 推播大小上限
    return {"action": "http", "label": label, "url": "https://ntfy.sh/", "method": "POST",
            "body": json.dumps(msg, ensure_ascii=False, separators=(",", ":")), "clear": True}

def cmd(text):
    return {"topic": CMD, "message": text}

def signin_mini(session, delay):
    """精簡版簽到提醒：給手機按「✅ 已簽退」時直接排下一段用。✅ 已簽到只排最簡單的簽退提醒，才塞得進通知大小上限。
    Mac 開著時會換成完整版。is_mini() 靠 message 認它，改文字要一起改。"""
    return {"topic": TOPIC, "title": f"NYCU 該簽到了（第 {session} 段）", "priority": 4,
            "message": "休息時間到", "delay": delay,
            "actions": [{"action": "view", "label": "開VPN＋簽到頁", "url": VPN_PAGE},
                        http_btn("✅ 已簽到", signout_mini())]}

def signout_mini():
    """電腦沒開時第 2 段的簽退提醒：只有開頁按鈕；已簽退要等電腦打開才能回報"""
    return {"topic": TOPIC, "title": "NYCU 該簽退了", "priority": 4, "message": f"已滿{H}小時",
            "delay": TEST or f"{H}h", "actions": [{"action": "view", "label": "開VPN＋簽退頁", "url": VPN_PAGE}]}

def is_mini(m):
    """這則簽到提醒是不是手機按「✅ 已簽退」排的精簡版。看內容判斷，不靠預排紀錄（紀錄和 ntfy 可能不同步）"""
    return m.get("message") == "休息時間到"

def signout_msg(hours=H, start="", delay=None, slim=False, text=None, session=1):
    # 還有下一段：手機按「✅ 已簽退」就直接排好休息後的簽到（不靠 Mac，時間是實際的）；最後一段：通知 Mac
    last = session >= CFG["sessions_per_day"] or TEST
    done_btn = (http_btn("✅ 已簽退", cmd("signed_out")) if last else
                http_btn("✅ 已簽退", signin_mini(session + 1, f"{CFG['break_min']}m")))
    nested = slim and not last     # 包在第 1 段簽到按鈕裡、又包著下一段：要最精簡，整則才不會超過 iPhone 推播上限
    m = {"topic": TOPIC, "title": "NYCU 該簽退了", "priority": 4,
         "message": f"已滿{hours}小時。✅ 按一次就好" if nested else
                    (text or f"{start + ' 簽到，' if start else ''}已滿 {hours} 小時") + "。✅ 按一次就好，畫面不會變但已收到",
         "actions": [{"action": "view", "label": "開VPN＋簽退頁", "url": VPN_PAGE}, done_btn]}
    if CFG["phone_pause_button"] and not nested: m["actions"].append(http_btn("今天不用了", cmd("today_done")))
    if not slim: m.update(tags=["alarm_clock"])
    if delay: m["delay"] = delay
    return m

SIGNED_SHORTCUT = "shortcuts://run-shortcut?name=NYCU-%E5%B7%B2%E7%B0%BD%E5%88%B0"   # 捷徑「NYCU-已簽到」

def signed_url(hours=None):
    """給 iPhone 捷徑「NYCU-已簽到」用的網址：打開它就排好 N 小時後的簽退提醒（含按鈕）"""
    import urllib.parse
    h = f"{float(hours or CFG['hours']):g}"
    acts = (f"view, 開VPN＋簽退頁, {VPN_PAGE}; "
            f"http, ✅ 已簽退, https://ntfy.sh/{CMD}, body=signed_out, clear=true"
            + (f"; http, 今天不用了, https://ntfy.sh/{CMD}, body=today_done, clear=true" if CFG["phone_pause_button"] else ""))
    q = urllib.parse.urlencode({"title": "NYCU 該簽退了", "message": f"已滿 {h} 小時", "priority": 4,
                                "tags": "alarm_clock,手機排的", "delay": f"{h}h", "actions": acts})
    return f"https://ntfy.sh/{TOPIC}/publish?{q}"

def signin_msg(session, delay=None, snooze=True):
    actions = [{"action": "view", "label": "開VPN＋簽到頁", "url": VPN_PAGE},
               http_btn(f"✅ 已簽到（{H} 小時）", signout_msg(H, delay=TEST or f"{H}h", slim=True, session=int(session)))]   # 手機直接排簽退（Mac 沒開也會響）
    if snooze:   # 交給 Mac：N 分鐘後兩邊再提醒一次（訊息才夠小，iPhone 收得到）
        actions.append(http_btn(f"{CFG['snooze_in_min']} 分鐘後", cmd("snooze_in")))
    m = {"topic": TOPIC, "title": f"NYCU 該簽到了（第 {session} 段）", "priority": 4,
         "message": "簽完到按「✅ 已簽到」一次就好：畫面不會有反應，但已經收到了", "actions": actions}
    if snooze: m.update(tags=["alarm_clock"])   # 點通知只打開 ntfy，不自動跑捷徑
    if delay: m["delay"] = delay
    return m

if __name__ == "__main__":
    if sys.argv[1] == "signin":
        print(post(signin_msg(sys.argv[2] if len(sys.argv) > 2 else "1"), force=True)["id"])
    elif sys.argv[1] == "signout":
        print(post(signout_msg(), force=True)["id"])
    elif sys.argv[1] == "signed_url":
        print(signed_url(sys.argv[2] if len(sys.argv) > 2 else None))
    elif sys.argv[1] == "test":   # 測試手機收不收得到：明確標示是測試，按鈕只有不會改狀態的「試捷徑」
        print(post({"topic": TOPIC, "title": "🧪 測試通知（不用處理）", "tags": ["test_tube"], "priority": 3,
                    "message": "看到這則，代表手機收得到打卡提醒。長按可以試「NYCU-Clock」捷徑。",
                    "actions": [{"action": "view", "label": "試捷徑 NYCU-Clock", "url": VPN_PAGE}]}, force=True)["id"])
