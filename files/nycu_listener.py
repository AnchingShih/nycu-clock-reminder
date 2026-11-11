#!/usr/bin/env python3
"""Mac 背景收聽 ntfy
  主頻道「NYCU 該簽退了」 → 在 Mac 右上角詢問簽退
  指令頻道 signed_out     → 手機按了「✅ 已簽退」，收掉 Mac 的詢問並結束這一段
  指令頻道 snooze_in      → 手機按了簽到的「60 分鐘後」
  指令頻道 today_done     → 手機按了「今天不用了」：這段算簽退，今天不再提醒
  指令頻道 pause_month    → 舊版通知的「本月不用了」（新通知已換成今天不用了）
Mac 睡眠或關機期間的指令，醒來後會依序補處理（簽退提醒超過 15 分鐘就不補跳）
"""
import json, os, subprocess, sys, threading, time, urllib.request, datetime as dt
S = os.path.expanduser("~/Scripts")
sys.path.insert(0, S)
from nycu_push import TOPIC, CMD, MAC, post, cancel_signouts, scheduled_signouts, H, TEST
import nycu_flow, nycu_push
FLOW, ALERTER, SINCE = f"{S}/nycu_flow.py", f"{S}/bin/alerter", f"{S}/.nycu_listener_since"

def remove_alerts(group="nycu-clock"):
    """收掉 Mac 右上角的提醒；alerter --remove 偶爾會卡住，最多等 5 秒"""
    try: subprocess.run([ALERTER, "--remove", group], capture_output=True, timeout=5)
    except Exception: subprocess.run(["pkill", "-f", f"alerter --remove {group}"])
MAX_AGE = 15 * 60

def flow(*args, wait=False):
    p = subprocess.Popen([sys.executable, FLOW, *args]); wait and p.wait()

def notify_both(title, text, tag):
    """手機（ntfy）和 Mac 右上角各跳一則"""
    try: post({"topic": TOPIC, "title": title, "message": text, "tags": [tag]})
    except Exception as e: print("推手機失敗", e, flush=True)
    subprocess.Popen([ALERTER, "--group", "nycu-info", "--sender", "com.apple.Terminal", "--sound", "Glass",
                      "--title", title, "--message", text, "--close-label", "好", "--timeout", "20"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def handle_signed_in(m):
    """手機按了「✅ 已簽到」：Mac 排簽退（從按下的時間算），並立刻回報開始計時"""
    parts = m["message"].split(); hours = parts[1] if len(parts) > 1 else H
    t = dt.datetime.fromtimestamp(m["time"])
    subprocess.run(["pkill", "-f", "nycu_flow.py tick"])                     # 收掉 Mac 上還在問的「該簽到了」
    remove_alerts()
    try: pending = scheduled_signouts()
    except Exception: pending = []
    if pending:                                                              # 已經在計時：不重排
        due = dt.datetime.fromtimestamp(pending[0]["time"])
        with nycu_flow.State() as st:
            st.update(stage="working", signout_at=due.isoformat(timespec="seconds"))
        notify_both("⏳ 已經在計時", f"維持原本的時間，{due:%H:%M} 提醒簽退", "hourglass_flowing_sand"); return
    args = ["--in", TEST] if TEST else [t.strftime("%H:%M"), "--hours", hours]
    r = subprocess.run([sys.executable, f"{S}/nycu_signed.py", *args], capture_output=True, text=True)
    if r.returncode != 0:
        notify_both("⚠️ 沒有排到簽退提醒", (r.stderr or r.stdout).strip()[-120:], "warning"); return
    with nycu_flow.State() as st:
        due = st.get("signout_at") or ""
        st["confirmed_signout"] = due                                       # 讓確認迴圈不要再推一次
    notify_both("✅ 已開始計時", f"{t:%H:%M} 簽到，{due[11:16]} 提醒簽退", "white_check_mark")

def stop_signout_prompt():
    subprocess.run(["pkill", "-f", "nycu_flow.py signout"])
    remove_alerts()

LAST_POLL = {"signins": []}

def scheduled():
    """排定中的簽退提醒；順便記下排定中的簽到提醒（LAST_POLL）給 phone_signout_check 用"""
    try:
        with urllib.request.urlopen(f"https://ntfy.sh/{TOPIC},{MAC}/json?poll=1&scheduled=1", timeout=10) as r:
            ms = [json.loads(l) for l in r if l.strip()]
        ms = [m for m in ms if m.get("event") == "message" and m["time"] > time.time()]
        LAST_POLL["signins"] = [m for m in ms if m.get("title", "").startswith("NYCU 該簽到了")]
        return [m for m in ms if m.get("title", "").startswith("NYCU 該簽退了")]
    except Exception:
        return None

def phone_signed_out(m, delivered=False):
    """看到精簡版「該簽到了」＝手機按了「✅ 已簽退」：結束這一段，並把精簡版換成完整版"""
    if not nycu_push.is_mini(m): return False
    with nycu_flow.State() as st:
        mine = {x[:16] for x in st.get("presched", [])}
        stage = st.get("stage")
    key = dt.datetime.fromtimestamp(m["time"]).isoformat(timespec="minutes")
    if key in mine or stage not in ("working", "asking_out"): return False
    brk = nycu_flow.load_config()["break_min"]
    at = m["time"] - brk * 60
    print(time.strftime("%F %T"), "手機回報：已簽退（手機已排下一段簽到", key[11:], "）", flush=True)
    stop_signout_prompt(); flow("finish", str(at), wait=True)
    if not delivered:                                      # 還沒送出：撤掉精簡版，由 Mac 排完整版（有按鈕）
        try: nycu_push.delete_msg(m)
        except Exception as e: print("撤精簡版失敗", e, flush=True)
    flow("plan" if not delivered else "tick")              # 已經送出了（Mac 剛開機）：直接問簽到，手機會收到完整版
    return True

def check_interval():
    """提醒跳出後 30 分鐘內手機可能正要按 ✅：快速檢查（6 秒）；其他時間慢慢檢查（60 秒）。
    ntfy 免費額度約每 5 秒 1 次，這裡是唯一會頻繁查詢的地方，別再加其他輪詢"""
    try:
        with nycu_flow.State() as st:
            pushed = [st.get("signin_pushed_at"), st.get("signout_pushed_at")] if st.get("stage") in ("asking_in", "asking_out", "wait_in") else []
        for p in pushed:
            if p and (dt.datetime.now() - dt.datetime.fromisoformat(p)).total_seconds() < 30 * 60: return 6
    except Exception: pass
    return 60

def confirm_loop():
    """發現新排的簽退提醒（不論是手機按 ✅ 或 Mac 端排的），就推確認通知到手機"""
    seen = {m["id"] for m in (scheduled() or [])}
    while True:
        time.sleep(check_interval())
        ms = scheduled()
        if ms is None:                                     # 查詢失敗（含超過額度）：等久一點，不要一直撞
            time.sleep(60); continue
        for si in LAST_POLL["signins"]:                    # 手機按了「✅ 已簽退」？
            if si["id"] not in seen:
                seen.add(si["id"])
                try: phone_signed_out(si)
                except Exception as e: print("處理手機簽退失敗", e, flush=True)
        new = [m for m in ms if m["id"] not in seen]
        if not new: continue
        seen.update(m["id"] for m in ms)
        keep = ms[0]                                  # 已經在計時就留原本那個（設定頁改時間會先取消舊的，所以只會剩新的）
        if len(ms) > 1:
            try: print(time.strftime("%F %T"), "已經在計時，取消多排的簽退提醒", cancel_signouts(keep_id=keep["id"]), "個", flush=True)
            except Exception as e: print("取消失敗", e, flush=True)
            if keep not in new:                       # 已經在計時又按了 ✅：告訴你維持原本的時間
                notify_both("⏳ 已經在計時", f"維持原本的時間，{dt.datetime.fromtimestamp(keep['time']):%H:%M} 提醒簽退", "hourglass_flowing_sand")
        already = False
        try:
            with nycu_flow.State() as st:
                if keep["id"] in st.get("cancelled", []):     # 檢查途中被取消了，不要加回來
                    continue
                already = st.get("confirmed_signout") == dt.datetime.fromtimestamp(keep["time"]).isoformat(timespec="seconds")
                st["stage"] = "working"; st["signout_at"] = dt.datetime.fromtimestamp(keep["time"]).isoformat(timespec="seconds")
        except Exception as e: print("更新狀態失敗", e, flush=True)
        for m in [keep]:
            if keep not in new or already: continue   # 舊的那個或手機按 ✅ 時已經回報過
            if "手機排的" in (m.get("tags") or []): continue   # 捷徑已經在手機上顯示過了
            due = dt.datetime.fromtimestamp(m["time"])
            print(time.strftime("%F %T"), "已開始計時，簽退提醒", f"{due:%H:%M:%S}", flush=True)
            notify_both("✅ 已開始計時", f"{due:%H:%M} 提醒簽退", "white_check_mark")   # 手機和 Mac 右上角都跳
            subprocess.Popen([sys.executable, FLOW, "plan"])     # 手機簽到後當下預排第 2 段

threading.Thread(target=confirm_loop, daemon=True).start()
last_signout = 0
offline = None                     # 斷線時只記第一次和恢復，不要每 10 秒記一行
since = open(SINCE).read().strip() if os.path.exists(SINCE) else str(int(time.time()))
while True:
    try:
        with urllib.request.urlopen(f"https://ntfy.sh/{TOPIC},{MAC},{CMD}/json?since={since}", timeout=90) as r:
            if offline:
                print(time.strftime("%F %T"), f"連線恢復（從 {offline} 斷線）", flush=True); offline = None
            for line in r:
                m = json.loads(line)
                if m.get("event") != "message": continue
                since = m["id"]; open(SINCE, "w").write(since)
                age = time.time() - m["time"]
                if m["topic"] in (TOPIC, MAC) and m.get("title", "").startswith("NYCU 該簽退了") and age < MAX_AGE:
                    busy = subprocess.run(["pgrep", "-f", "nycu_flow.py signout"], capture_output=True).returncode == 0
                    if busy or m["time"] - last_signout < 300:
                        print(time.strftime("%F %T"), "重複的簽退提醒，略過", flush=True)
                        continue
                    last_signout = m["time"]
                    print(time.strftime("%F %T"), "簽退提醒", flush=True)
                    flow("signout")
                elif m["topic"] in (TOPIC, MAC) and m.get("title", "").startswith("NYCU 該簽到了") and nycu_push.is_mini(m):
                    try: phone_signed_out(m, delivered=True)       # 精簡版沒有 ✅，一定是手機按「已簽退」排的
                    except Exception as e: print("處理手機簽退失敗", e, flush=True)
                elif m["topic"] == CMD and m.get("message", "").startswith("signed_in"):
                    print(time.strftime("%F %T"), "手機回報：已簽到", m["message"], flush=True)
                    handle_signed_in(m)
                elif m["topic"] == CMD and m.get("message") == "signed_out":
                    print(time.strftime("%F %T"), "手機回報：已簽退", flush=True)
                    stop_signout_prompt(); flow("finish", str(m["time"]), wait=True); flow("plan")   # 當下改好第 2 段簽到
                elif m["topic"] == CMD and m.get("message") == "snooze_in":
                    print(time.strftime("%F %T"), "手機回報：簽到 60 分鐘後", flush=True)
                    subprocess.run(["pkill", "-f", "nycu_flow.py tick"])
                    remove_alerts()
                    flow("snooze", str(m["time"]), wait=True)
                elif m["topic"] == CMD and m.get("message") == "today_done":
                    print(time.strftime("%F %T"), "手機回報：今天不用了", flush=True)
                    stop_signout_prompt(); flow("today", wait=True); flow("plan")
                elif m["topic"] == CMD and m.get("message") == "pause_month":
                    print(time.strftime("%F %T"), "手機回報：本月不用了", flush=True)
                    stop_signout_prompt(); flow("pause", wait=True)
    except Exception as e:
        if "timed out" in str(e): pass                     # 長時間沒訊息的正常逾時，馬上重連就好
        elif not offline:
            offline = time.strftime("%H:%M"); print(time.strftime("%F %T"), "連不上 ntfy，持續重試：", e, flush=True)
        time.sleep(10)
