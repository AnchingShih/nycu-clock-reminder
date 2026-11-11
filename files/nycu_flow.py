#!/usr/bin/env python3
"""NYCU 兼任差勤提醒流程（Mac 與手機同時提醒，任一邊回覆即可）
  tick            每 2 分鐘由 launchd 呼叫：該簽到就同時推手機、在 Mac 右上角詢問
  signout         背景收聽程式收到「該簽退了」時呼叫
  finish [epoch]  手機按「✅ 已簽退」時由背景收聽程式呼叫
  pause           手機按「本月不用了」時由背景收聽程式呼叫
  snooze <epoch>  手機按簽到的「60 分鐘後」時由背景收聽程式呼叫
  resume          取消「本月不用了」
  plan            預先把接下來 3 天的簽到提醒排在 ntfy 上（tick 每次也會做）
  status          顯示今天/本週狀態
規則：週一到週六 9:00 起提醒，每週做滿 5 天就停；每天 2 段，中間休息 1 小時
"""
import json, os, sys, fcntl, subprocess, time, urllib.request, datetime as dt

S = os.path.expanduser("~/Scripts")
sys.path.insert(0, S)
from nycu_push import post, signin_msg, signout_msg, TOPIC, MAC, TEST, CFG, H, load_config, scheduled_signouts, scheduled_signins, delete_msg, is_mini

STATE, LOCK = f"{S}/.nycu_state.json", f"{S}/.nycu_state.lock"
ALERTER = f"{S}/bin/alerter"
REMIND_SEC = 60                                # 沒回覆時再提醒的間隔＝renotify_min × 60 秒
REMIND_MAX = 3                                 # 同一則提醒最多再跳 3 次（使用者指定，2026-11-04）

def remove_alerts(group="nycu-clock"):
    """收掉 Mac 右上角的提醒；alerter --remove 偶爾會卡住，最多等 5 秒"""
    try: subprocess.run([ALERTER, "--remove", group], capture_output=True, timeout=5)
    except Exception: subprocess.run(["pkill", "-f", f"alerter --remove {group}"])
# 以下都來自 nycu_config.json（打卡設定頁面可改）
FIRST_PROMPT = dt.time(*map(int, CFG["first_prompt"].split(":")))
WORKDAYS = set(CFG["workdays"])
DAYS_PER_WEEK, SESSIONS_PER_DAY = CFG["days_per_week"], CFG["sessions_per_day"]
_m = lambda real: dt.timedelta(minutes=CFG["test_minutes"] if TEST else real)   # 測試模式一律縮成測試分鐘數
BREAK, SNOOZE_IN, SNOOZE_OUT = _m(CFG["break_min"]), _m(CFG["snooze_in_min"]), _m(CFG["snooze_out_min"])
SNOOZE_IN_LABEL, SNOOZE_OUT_LABEL = f"{CFG['snooze_in_min']} 分鐘後", f"{CFG['snooze_out_min']} 分鐘後"
SIGNED_LABEL = f"已簽到（{H} 小時）"

class State:
    def __enter__(self):
        self.lf = open(LOCK, "w"); fcntl.flock(self.lf, fcntl.LOCK_EX)
        self.d = json.load(open(STATE)) if os.path.exists(STATE) else {}
        today = dt.date.today(); y, w, _ = today.isocalendar()
        if self.d.get("week") != f"{y}-W{w:02d}":
            self.d.update(week=f"{y}-W{w:02d}", days_done=[])
        if self.d.get("date") != today.isoformat():
            self.d.update(date=today.isoformat(), session=1, stage="wait_in",
                          next_prompt=dt.datetime.combine(today, FIRST_PROMPT).isoformat(timespec="seconds"))
            self.d.pop("phone_scheduled", None); self.d.pop("signout_at", None); self.d.pop("force_today", None)
            # 新的一天：本週已經做滿就直接變「本週已完成」（每次即時讀設定的天數）
            cfg = load_config()
            if len(self.d["days_done"]) >= cfg["days_per_week"] and not self.d.get("paused_until", "") > today.isoformat():
                nxt = today + dt.timedelta(days=7 - today.weekday())
                while nxt.weekday() not in set(cfg["workdays"]): nxt += dt.timedelta(days=1)
                self.d.update(paused_until=nxt.isoformat(), paused_kind="week", stage="done")
        return self.d
    def __exit__(self, *a):
        # 一天只要有簽到（進入上班中），不管做幾段都算一天
        if self.d.get("stage") in ("working", "asking_out") and self.d["date"] not in self.d["days_done"]:
            self.d["days_done"].append(self.d["date"])
        json.dump(self.d, open(STATE, "w"), ensure_ascii=False, indent=1); self.lf.close()

def log(*a): print(time.strftime("%F %T"), *a, flush=True)

def scheduled():
    """ntfy 上還沒送出的排程訊息（手機按了 ✅ 已簽到 或 60 分鐘後 就會出現）"""
    try:
        with urllib.request.urlopen(f"https://ntfy.sh/{TOPIC},{MAC}/json?poll=1&scheduled=1", timeout=10) as r:
            ms = [json.loads(l) for l in r if l.strip()]
        return [m for m in ms if m.get("event") == "message" and m["time"] > time.time()]
    except Exception:
        return []

def ask(title, msg, actions, close, timeout=None, reply=None, watch=None, remind=None):
    """右上角詢問。watch() 回傳非 None 時（別處回覆了）就收掉通知並回傳 ('@PHONE', 結果)。
    remind：太久沒回就重新跳一次通知，並呼叫 remind() 再推手機，最多 REMIND_MAX 次（設定 renotify_min＝0 就不再跳）"""
    cmd = [ALERTER, "--group", "nycu-clock", "--sender", "com.apple.Terminal", "--sound", "Glass",
           "--title", title, "--message", msg, "--close-label", close]
    if actions: cmd += ["--actions", ",".join(actions), "--dropdown-label", "選擇"]
    if reply: cmd += ["--reply", reply]
    if timeout: cmd += ["--timeout", str(timeout)]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
    last = since = time.time(); times = 0; day = dt.date.today()
    every = load_config().get("renotify_min", 0) * REMIND_SEC if remind else 0
    while p.poll() is None:
        time.sleep(2)
        if watch and time.time() - last >= 10:
            last = time.time(); w = watch()
            if w is not None:
                p.kill(); remove_alerts()
                return ("@PHONE", w)
        if every and times < REMIND_MAX and dt.date.today() == day and time.time() - since >= every:
            times += 1; since = time.time()
            p.kill(); p.wait(); remove_alerts()
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
            log(f"太久沒回，再提醒一次（第 {times} 次）：{title}")
            try: remind()
            except Exception as e: log("再提醒手機失敗", e)
    r = p.stdout.read().strip()
    return "好" if r == "@CONTENTCLICKED" and actions else r

def open_page(label):
    subprocess.run([f"{S}/nycu_clock_open.sh", label], env={**os.environ, "NO_PUSH": "1"})

def watch_in():
    """只讀本機狀態，不連網：手機回覆由背景程式（listener）查到後改狀態，這裡看到就收掉詢問"""
    def w():
        with State() as st: stage = st.get("stage")
        if stage in ("working", "asking_out"): return ("signed", None)
        if stage != "asking_in": return ("changed", None)
    return w

def ask_hours():
    """跳輸入框問幾小時後提醒簽退，回傳 "3"、"2.5"；看不懂就回 None"""
    h = ask("幾小時後提醒簽退？", "輸入數字，例如 3 或 2.5", None, "取消", reply="小時數")
    try: return f"{float(h):g}" if 0 < float(h) <= 12 else None
    except ValueError:
        if h not in ("@CLOSED", "@TIMEOUT", "取消"):
            ask("沒有排簽退提醒", f"看不懂「{h}」。請在終端機打：已簽到 --hours 小時數", None, "好", timeout=60)
        return None

def schedule_signout(hours):
    subprocess.run([sys.executable, f"{S}/nycu_signed.py", "--hours", hours] + (["--in", TEST] if TEST else []),
                   capture_output=True)

def plan_signins():
    """把接下來 3 天內會響的簽到提醒先排在 ntfy 上（Mac 關機手機也收得到），不需要的撤掉。
    今天：等簽到時排 next_prompt。之後：每個提醒日的第一段。下一段由按「已簽退」那一刻排（手機或 Mac）。"""
    if TEST: return
    cfg = load_config()
    now = dt.datetime.now(); today = now.date()
    first = dt.time(*map(int, cfg["first_prompt"].split(":")))
    workdays, brk = set(cfg["workdays"]), dt.timedelta(minutes=cfg["break_min"])
    with State() as st: s = json.loads(json.dumps(st))
    pause = s.get("paused_until") or ""
    def allowed(d, forced=False):
        if pause and d.isoformat() < pause: return False
        if forced: return True
        y, w, _ = d.isocalendar()
        same_week = f"{y}-W{w:02d}" == s["week"]
        others = [x for x in s["days_done"] if x != d.isoformat()]
        return d.weekday() in workdays and not (same_week and len(others) >= cfg["days_per_week"])
    forced = s.get("force_today") == today.isoformat()
    want, near = {}, {}
    if s["stage"] == "wait_in":
        t = dt.datetime.fromisoformat(s["next_prompt"])
        if allowed(today, forced):
            if t > now + dt.timedelta(minutes=3): want[t] = s["session"]
            elif t > now: near[t] = s["session"]       # 3 分鐘內就響：已排好的留著（別撤掉），沒排的不補，交給 tick 推
    for k in (1, 2, 3):
        t = dt.datetime.combine(today + dt.timedelta(days=k), first)
        if t < now + dt.timedelta(hours=71) and allowed(t.date()): want[t] = 1
    sync = cfg.get("phone_sync", True)                 # 關閉＝每天第一次手機不跳：第 1 段不預排，已排的撤掉
    if not sync:
        want = {t: n for t, n in want.items() if n != 1}; near = {t: n for t, n in near.items() if n != 1}
    try: have = scheduled_signins()
    except Exception: return
    want_min = {int(t.timestamp()) // 60: (t, n) for t, n in want.items()}
    near_min = {int(t.timestamp()) // 60: t for t in near}
    have_min, kept = set(), []
    for m in have:
        k = int(m["time"]) // 60
        if k in want_min and k not in have_min: have_min.add(k)
        elif k in near_min and near_min[k] not in kept: kept.append(near_min[k])
        elif is_mini(m): continue             # 手機按「已簽退」排的：交給背景程式處理，這裡不撤
        else:
            try: delete_msg(m); log("撤掉預排的簽到提醒", dt.datetime.fromtimestamp(m["time"]).strftime("%m/%d %H:%M"))
            except Exception as e: log("撤掉失敗", e)
    with State() as st:                                # 先記再送：別的程式看到新訊息時才認得是預排的
        st["presched"] = sorted(set(st.get("presched", []) + [t.isoformat(timespec="minutes") for t in [*want, *kept]]))[-30:]
        if not sync:                                   # 第 1 段已撤掉：拿掉未來的紀錄
            keep = {t.isoformat(timespec="minutes") for t in [*want, *kept]}
            st["presched"] = [x for x in st["presched"] if x <= now.isoformat(timespec="minutes") or x in keep]
    for k, (t, n) in want_min.items():
        if k in have_min: continue
        try: post(signin_msg(n, delay=str(int(t.timestamp())))); log("預排簽到提醒", f"{t:%m/%d %H:%M}", f"第 {n} 段")
        except Exception as e:
            log("預排失敗", e)
            with State() as st: st["presched"] = [x for x in st["presched"] if x != t.isoformat(timespec="minutes")]   # 沒送成：時間到由 tick 推

LATE_SIGNOUT = 15 * 60          # 簽退時間過了這麼久還在「上班中」：電腦當時在睡眠，背景程式已略過那則提醒，改由 tick 補問

def late_signout():
    """電腦睡過簽退時間：醒來後補跳簽退詢問。手機那邊已經按了「✅ 已簽退」（有精簡版簽到在排）就不問"""
    with State() as st:
        due = st.get("signout_at") if st.get("stage") == "working" else None
    if not due or (dt.datetime.now() - dt.datetime.fromisoformat(due)).total_seconds() < LATE_SIGNOUT: return
    if subprocess.run(["pgrep", "-f", "nycu_flow.py signout"], capture_output=True).returncode == 0: return
    try:
        if scheduled_signouts() or any(is_mini(m) for m in scheduled_signins()): return
    except Exception: return                                # 連不上 ntfy：下次再看
    log("電腦睡過簽退時間", due[11:16], "，補問簽退")
    subprocess.Popen([sys.executable, f"{S}/nycu_flow.py", "signout", "late", due[11:16]],
                     stdout=open(f"{S}/nycu_flow.log", "a"), stderr=subprocess.STDOUT, start_new_session=True)

LOG_KEEP_DAYS = 30

def trim_logs():
    """每天一次：三個紀錄檔只留最近 30 天。原地改寫（背景程式是附加寫入，不會寫壞）"""
    today = dt.date.today().isoformat()
    with State() as st:
        if st.get("logs_trimmed") == today: return
        st["logs_trimmed"] = today
    cut = (dt.date.today() - dt.timedelta(days=LOG_KEEP_DAYS)).isoformat()
    for name in ("nycu_flow.log", "nycu_listener.log", "nycu_settings.log"):
        p = f"{S}/{name}"
        if not os.path.exists(p): continue
        with open(p, "r+", encoding="utf-8", errors="replace") as f:
            lines, keep, old = f.readlines(), [], True
            for l in lines:                              # 沒有日期的行（錯誤訊息的後續行）跟著上一行走
                if len(l) >= 10 and l[4] == "-" and l[7] == "-" and l[:4].isdigit(): old = l[:10] < cut
                if not old: keep.append(l)
            if len(keep) < len(lines):
                f.seek(0); f.write("".join(keep)); f.truncate()
                log("清理紀錄檔", name, f"刪掉 {len(lines) - len(keep)} 行")

def tick():
    try: trim_logs()
    except Exception as e: log("清理紀錄檔失敗", e)
    try: plan_signins()
    except Exception as e: log("預排檢查失敗", e)
    try: late_signout()
    except Exception as e: log("補問簽退失敗", e)
    with State() as st:
        today = dt.date.today()
        if st.get("paused_until") and today.isoformat() < st["paused_until"]: return
        other_days = [d for d in st["days_done"] if d != st["date"]]     # 今天已簽到不擋今天的下一段
        forced = st.get("force_today") == today.isoformat()               # 從設定頁排的簽到：今天一定響
        if ((not forced and (today.weekday() not in WORKDAYS or len(other_days) >= DAYS_PER_WEEK))
                or st["stage"] != "wait_in" or dt.datetime.now() < dt.datetime.fromisoformat(st["next_prompt"])):
            return
        # 已經有簽退計時（不管是哪裡排的）：代表已經簽到了，不要再問簽到
        try: pending = scheduled_signouts()
        except Exception: pending = []
        if pending:
            st.update(stage="working", signout_at=dt.datetime.fromtimestamp(pending[0]["time"]).isoformat(timespec="seconds"))
            log("已有簽退計時，不提醒簽到"); return
        st["stage"] = "asking_in"; n = st["session"]
        phone = not st.pop("phone_scheduled", False)
        if st["next_prompt"][:16] in st.get("presched", []): phone = False   # 已經預排在 ntfy 上，手機那則已經送出了
        if n == 1 and not load_config().get("phone_sync", True): phone = False   # 每天第一次只有電腦跳
    if phone:
        try: post(signin_msg(n))
        except Exception as e: log("推手機失敗", e)
    with State() as st: st["signin_pushed_at"] = dt.datetime.now().isoformat(timespec="seconds")   # 背景程式據此加快檢查
    def remind():                                       # 太久沒回：手機也再跳（每天第一次關掉手機時就不推）
        if not (n == 1 and not load_config().get("phone_sync", True)): post(signin_msg(n))
        with State() as st: st["signin_pushed_at"] = dt.datetime.now().isoformat(timespec="seconds")
    log(f"詢問簽到 第 {n} 段")
    new = None
    r = ask(f"NYCU 該簽到了（第 {n} 段）", "要現在開簽到頁嗎？已經簽好就選「已簽到」", ["好", "已簽到", SNOOZE_IN_LABEL], "今天不做",
            watch=watch_in(), remind=remind)
    if r == "好":
        open_page("簽到"); time.sleep(20)
        r2 = ask("簽完到了嗎？", "選擇幾小時後提醒簽退", [SIGNED_LABEL, "其他時數…"], "還沒",
                 timeout=1800, watch=watch_in())
        hours = H if r2 in (SIGNED_LABEL, "@CONTENTCLICKED") else None
        if r2 == "其他時數…":
            hours = ask_hours()
        if isinstance(r2, tuple):
            r = r2                                                  # 手機那邊回覆了，交給下面處理
        elif hours:
            schedule_signout(hours); new = dict(stage="working")
        elif r2 == "其他時數…":
            new = dict(stage="working")
        else:
            new = dict(stage="wait_in", next_prompt=(dt.datetime.now() + dt.timedelta(minutes=10)).isoformat(timespec="seconds"))
    elif r == "已簽到":                                  # 已經自己簽好了：直接選多久後提醒簽退
        r2 = ask("多久後提醒簽退？", "已簽到，選擇簽退提醒時間", [f"{H} 小時後", "其他時數…"], "取消",
                 timeout=600, watch=watch_in())
        hours = H if r2 in (f"{H} 小時後", "@CONTENTCLICKED") else ask_hours() if r2 == "其他時數…" else None
        if isinstance(r2, tuple):
            r = r2
        elif hours:
            schedule_signout(hours); new = dict(stage="working"); log(f"已簽到，{hours} 小時後提醒簽退")
        else:                                            # 取消或沒選：10 分鐘後再問
            new = dict(stage="wait_in", next_prompt=(dt.datetime.now() + dt.timedelta(minutes=10)).isoformat(timespec="seconds"))
    if new is None:
        if isinstance(r, tuple) and r[1][0] == "signed":
            new = dict(stage="working"); log("手機回報：已簽到")
        elif isinstance(r, tuple):
            log("詢問期間狀態已改變，收掉詢問"); return
        elif r == SNOOZE_IN_LABEL:
            new = dict(stage="wait_in", next_prompt=(dt.datetime.now() + SNOOZE_IN).isoformat(timespec="seconds"))
        else:
            new = dict(stage="skipped"); log("今天不做")
    with State() as st:
        if st["stage"] != "asking_in":          # 詢問期間你已經在別處簽到/排簽退了，以那邊為準，不蓋掉
            if st["stage"] != new.get("stage"):  # 一樣是「上班中」是正常流程（排簽退時已改好），不用記
                log("詢問期間狀態已被改成", st["stage"], "，不覆蓋")
            return
        st.update(new)

def finish_session(at=None):
    at = dt.datetime.fromtimestamp(float(at)) if at else dt.datetime.now()
    with State() as st:
        if st["stage"] in ("done", "wait_in"): return               # 已經處理過（兩邊都回覆時）
        if st["session"] < SESSIONS_PER_DAY:
            st.update(session=st["session"] + 1, stage="wait_in", next_prompt=(at + BREAK).isoformat(timespec="seconds"))
            log("這段結束，下一段", st["next_prompt"])
        else:
            st["stage"] = "done"
            if st["date"] not in st["days_done"]: st["days_done"].append(st["date"])
            log("今天完成", st["days_done"])
            if len(st["days_done"]) >= DAYS_PER_WEEK:          # 本週做滿：暫停到下週第一個工作日
                nxt = dt.date.today() + dt.timedelta(days=7 - dt.date.today().weekday())
                while nxt.weekday() not in WORKDAYS: nxt += dt.timedelta(days=1)
                st.update(paused_until=nxt.isoformat(), paused_kind="week")
                post({"topic": TOPIC, "title": "本週 5 天已完成", "message": "這週剩下的日子不會再提醒"})
                ask("本週 5 天已完成", "這週剩下的日子不會再提醒，下週一重新計算", None, "好", timeout=60)

def signout(late=None):
    if not late: subprocess.run(["pkill", "-f", "nycu_flow.py tick"])   # 補問是 tick 叫的，不能把自己收掉
    remove_alerts()
    now_iso = lambda: dt.datetime.now().isoformat(timespec="seconds")
    with State() as st:
        st["stage"] = "asking_out"; st.pop("signout_at", None); st["signout_pushed_at"] = now_iso(); n = st.get("session", 1)
    log("詢問簽退")
    def remind():                                       # 太久沒回：手機也再跳
        post(signout_msg(text="還沒回覆簽退提醒", session=n))
        with State() as st: st["signout_pushed_at"] = now_iso()
    while True:
        r = ask("NYCU 該簽退了", (f"{late} 就該簽退了（當時電腦在睡眠）。" if late else "") + "要現在開簽退頁嗎？",
                ["好", SNOOZE_OUT_LABEL, "今天不用了"], "已簽退", remind=remind)
        if r == "今天不用了":
            finish_today(); return
        if r == SNOOZE_OUT_LABEL:
            time.sleep(SNOOZE_OUT.total_seconds()); continue
        if r == "好":
            open_page("簽退"); time.sleep(20)
            after = (f"簽退後休息 {CFG['break_min']} 分鐘提醒下一段" if n < SESSIONS_PER_DAY
                     else "這是今天最後一段，簽退後今天就完成了")
            r2 = ask("簽退了嗎？", after, ["已簽退", "今天不用了", "本月不用了"], "還沒", timeout=1800)
            if r2 == "今天不用了":
                finish_today(); return
            if r2 in ("還沒", "@TIMEOUT"):                   # 30 分鐘沒回：不算已簽退，10 分鐘後再問
                if r2 == "@TIMEOUT": log("簽退了嗎？沒回，10 分鐘後再問")
                time.sleep(600); continue
            if r2 == "本月不用了":
                pause_month(); return
        break
    finish_session()

def finish_today():
    """簽退提示選「今天不用了」：這段算簽退，今天不再提醒下一段（照最後一段處理，本週天數照算）"""
    with State() as st:
        if st["stage"] in ("done", "wait_in", "skipped"): return
        st["session"] = SESSIONS_PER_DAY
    log("今天不用了：這段結束，今天不再提醒")
    finish_session()

def pause_month():
    nxt = (dt.date.today().replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    while nxt.weekday() not in WORKDAYS:                 # 下個月第一個工作日（週一到週六）
        nxt += dt.timedelta(days=1)
    with State() as st:
        st.update(paused_until=nxt.isoformat(), paused_kind="month", stage="done")
    log("本月暫停到", nxt)
    ask("本月不再提醒", f"{nxt:%-m/%-d}（{'一二三四五六日'[nxt.weekday()]}）起恢復。要提早恢復，在終端機打：python3 ~/Scripts/nycu_flow.py resume",
        None, "好", timeout=60)

def snooze_in(at):
    at = dt.datetime.fromtimestamp(float(at))
    with State() as st:
        if st["stage"] in ("asking_in", "wait_in"):
            st.update(stage="wait_in", next_prompt=(at + SNOOZE_IN).isoformat(timespec="seconds"))
            log("手機回報：稍後提醒", st["next_prompt"])

def resume():
    with State() as st: st.pop("paused_until", None); st.pop("paused_kind", None)
    print("已恢復提醒")

def status():
    with State() as st: print(json.dumps(st, ensure_ascii=False, indent=1))

if __name__ == "__main__":
  a = sys.argv[1:]
  {"tick": tick, "plan": plan_signins, "signout": lambda: signout(a[2] if a[1:2] == ["late"] else None), "status": status, "resume": resume, "pause": pause_month,
 "finish": lambda: finish_session(a[1] if len(a) > 1 else None), "snooze": lambda: snooze_in(a[1]), "today": finish_today}[a[0]]()
