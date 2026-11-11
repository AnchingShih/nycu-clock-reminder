#!/usr/bin/env python3
"""打卡提醒設定頁面：http://127.0.0.1:8765 （只有這台 Mac 打得開）"""
import json, os, re, subprocess, sys, threading, urllib.error, datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

S = os.path.expanduser("~/Scripts")
sys.path.insert(0, S)
import nycu_flow as F
import nycu_update as U
from nycu_push import DEFAULTS, TOPIC, load_config, post, signout_msg, cancel_signouts

CONFIG = f"{S}/nycu_config.json"
PAGE = f"{S}/nycu_settings.html"

def week_dates():
    mon = dt.date.today() - dt.timedelta(days=dt.date.today().weekday())
    return [(mon + dt.timedelta(days=i)).isoformat() for i in range(7)]

def snapshot():
    with F.State() as st:
        state = dict(st)
    return {"config": load_config(), "state": state, "week": week_dates(), "today": dt.date.today().isoformat(), "topic": TOPIC, "version": U.current()}

def check_config(c):
    out = {}
    NAMES = {"break_min": "休息分鐘", "snooze_in_min": "簽到稍後提醒", "snooze_out_min": "簽退稍後提醒",
             "sessions_per_day": "每天幾段", "days_per_week": "每週幾天", "test_minutes": "測試模式", "renotify_min": "沒回覆再提醒"}
    hh, mm = map(int, str(c["first_prompt"]).split(":")); assert 0 <= hh < 24 and 0 <= mm < 60, "提醒時間"
    out["first_prompt"] = f"{hh:02d}:{mm:02d}"
    out["hours"] = float(c["hours"]); assert 0 < out["hours"] <= 12, "每段小時數要在 0.5–12"
    for k, lo, hi in [("break_min", 0, 600), ("snooze_in_min", 5, 600), ("snooze_out_min", 5, 600),
                      ("sessions_per_day", 1, 4), ("days_per_week", 1, 7), ("test_minutes", 0, 30), ("renotify_min", 0, 600)]:
        out[k] = int(c[k]); assert lo <= out[k] <= hi, f"{NAMES[k]}要在 {lo}–{hi}"
    assert out["renotify_min"] == 0 or out["renotify_min"] >= 5, "沒回覆再提醒要填 0，或至少 5 分鐘"
    out["workdays"] = sorted({int(d) for d in c["workdays"] if 0 <= int(d) <= 6})
    out["phone_pause_button"] = bool(c["phone_pause_button"])
    out["phone_notify"] = bool(c["phone_notify"])
    out["phone_sync"] = bool(c["phone_sync"])
    return out

def next_month_first_workday(workdays):
    nxt = (dt.date.today().replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    while nxt.weekday() not in workdays: nxt += dt.timedelta(days=1)
    return nxt.isoformat()

def next_week_first_workday(workdays):
    nxt = dt.date.today() + dt.timedelta(days=7 - dt.date.today().weekday())   # 下週一
    while nxt.weekday() not in workdays: nxt += dt.timedelta(days=1)
    return nxt.isoformat()

class Refuse(Exception):
    """照規則不做的動作（訊息直接給使用者看，不加「設定值不正確」）"""

def when(body):
    """把「N 小時/分鐘後」或「HH:MM」換成今天的時間點"""
    now = dt.datetime.now()
    if body.get("time"):
        hh, mm = map(int, body["time"].split(":"))
        t = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        assert t > now, f"{body['time']} 已經過了"
        return t
    n = float(body["amount"]); assert 0 < n <= (12 if body["unit"] == "hour" else 720), "時間長度不合理"
    return now + (dt.timedelta(hours=n) if body["unit"] == "hour" else dt.timedelta(minutes=n))

def do_action(a, body):
    cfg = load_config()
    if a == "signout_at":
        t = when(body)
        cancel_signouts()                                    # 新的取代舊的
        with F.State() as st: sess = st.get("session", 1)
        post(signout_msg(text=f"{t:%H:%M} 簽退提醒（從設定頁排的）", delay=str(int(t.timestamp())), session=sess))
        with F.State() as st:                                # 只留簽退提醒，不同時等簽到；狀態改回努力中
            st.update(stage="working", signout_at=t.isoformat(timespec="seconds"))
            st.pop("paused_until", None); st.pop("paused_kind", None)
        return
    if a == "open_page":                                     # 點頁面上的「打開打卡頁」：校外先自動連 VPN，再開簽到頁
        subprocess.Popen([f"{S}/nycu_clock_open.sh", "打卡"], env={**os.environ, "NO_PUSH": "1", "NO_NOTIFY": "1"},
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    if a == "cancel_next":                                   # 取消「下一步」：不再有提醒，狀態仍是努力中
        cancel_signouts()
        with F.State() as st:
            st.update(stage="idle"); st.pop("signout_at", None); st.pop("phone_scheduled", None)
        return
    with F.State() as st:
        if a == "resume":
            st.pop("paused_until", None)
        elif a == "pause":
            st.update(paused_until=next_month_first_workday(set(cfg["workdays"])), paused_kind="month", stage="done")
        elif a == "status":
            # 目前狀態：努力中 / 今天已完成 / 今天休息 / 本週已完成 / 本月已完成
            v, today, wd = body["status"], dt.date.today().isoformat(), set(cfg["workdays"])
            done = st.setdefault("days_done", [])
            if v in ("working", "today_done", "today_off"):
                st.pop("paused_until", None); st.pop("paused_kind", None)
            if v == "working":
                if st.get("stage") in ("done", "skipped"):
                    st["session"] = min(st.get("session", 1), cfg["sessions_per_day"])
                    st.update(stage="wait_in", next_prompt=dt.datetime.now().isoformat(timespec="seconds"))
                    st.pop("phone_scheduled", None)
            elif v == "today_done":
                st["stage"] = "done"
                if today not in done: done.append(today)
            elif v == "today_off":
                st["stage"] = "skipped"
            elif v == "week_done":
                st.update(paused_until=next_week_first_workday(wd), paused_kind="week", stage="done")
            elif v == "month_done":
                st.update(paused_until=next_month_first_workday(wd), paused_kind="month", stage="done")
            else:
                raise AssertionError("狀態不合理")
        elif a == "progress":
            # 今天做到第幾段：0 = 還沒開始；k = 第 k 段做完、等下一段；做滿 = 今天已完成
            k, n, today = int(body["progress"]), cfg["sessions_per_day"], dt.date.today().isoformat()
            assert 0 <= k <= n, "段數不合理"
            done = st.setdefault("days_done", [])
            st.pop("paused_until", None); st.pop("paused_kind", None); st.pop("phone_scheduled", None)
            now = dt.datetime.now()
            if k == n:
                st["stage"] = "done"
                if today not in done: done.append(today)
            else:
                # 一天只要有簽到就算一天：做過至少 1 段就算，0 段才不算
                if k == 0 and today in done: done.remove(today)
                if k >= 1 and today not in done: done.append(today)
                if k == 0:
                    first = dt.datetime.combine(dt.date.today(), dt.time(*map(int, cfg["first_prompt"].split(":"))))
                    st.update(session=1, stage="wait_in", next_prompt=max(now, first).isoformat(timespec="seconds"))
                else:
                    st.update(session=k + 1, stage="wait_in",
                              next_prompt=(now + dt.timedelta(minutes=cfg["break_min"])).isoformat(timespec="seconds"))
        elif a == "skip_today":
            st["stage"] = "skipped"
        elif a == "prompt_now":
            st.update(stage="wait_in", next_prompt=dt.datetime.now().isoformat(timespec="seconds"))
            st.pop("phone_scheduled", None)
        elif a == "set_session":
            st.update(session=int(body["session"]), stage="wait_in",
                      next_prompt=dt.datetime.now().isoformat(timespec="seconds"))
        elif a == "session":
            n = int(body["session"]); assert 1 <= n <= max(cfg["sessions_per_day"], 4), "段數不合理"
            st["session"] = n
        elif a == "signin_at" and st.get("stage") in ("working", "asking_out") and st.get("signout_at"):
            raise Refuse(f"已經簽到、{st['signout_at'][11:16]} 會提醒簽退，不會再排簽到。真的要改，先按「下一步」的取消")
        elif a == "signin_at":
            # 自己排的簽到提醒一定會響：狀態改回努力中，今天不受「非提醒日／本週已滿」限制
            st.update(stage="wait_in", next_prompt=when(body).isoformat(timespec="seconds"), force_today=dt.date.today().isoformat())
            st.pop("phone_scheduled", None); st.pop("paused_until", None); st.pop("paused_kind", None)
        elif a == "days":
            wk = set(week_dates())
            st["days_done"] = sorted(d for d in body["days"] if d in wk)
            # 點滿本週天數就自動變「本週已完成」；點掉一天就解除（不動「本月已完成」）
            if len(st["days_done"]) >= cfg["days_per_week"]:
                if st.get("paused_kind") != "month":
                    st.update(paused_until=next_week_first_workday(set(cfg["workdays"])), paused_kind="week")
            elif st.get("paused_kind") == "week":
                st.pop("paused_until", None); st.pop("paused_kind", None)
    # 同一時間只有一個提醒：不是在等簽退，就把排好的簽退提醒全部取消
    with F.State() as st:
        if st.get("stage") not in ("working", "asking_out"):
            st.pop("signout_at", None); drop = True
        else: drop = False
    if drop and a not in ("test_push", "test_mac"):
        try: cancel_signouts()
        except Exception: pass
    if a == "prompt_now" or a == "set_session":
        subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.nycu.clock.tick"])
    if a not in ("test_push", "test_mac", "open_page"):    # 狀態變了：簽到預排跟著調整
        subprocess.Popen([sys.executable, f"{S}/nycu_flow.py", "plan"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if a == "test_push":
        subprocess.run([sys.executable, f"{S}/nycu_push.py", "test"], capture_output=True)
    if a == "test_mac":                                      # Mac 右上角跳一則測試提醒；按「打開打卡頁」就照真的流程開頁
        def run():
            r = subprocess.run([f"{S}/bin/alerter", "--group", "nycu-test", "--sender", "com.apple.Terminal", "--sound", "Glass",
                                "--title", "🧪 測試通知（不用處理）", "--message", "看到這則，代表 Mac 右上角的提醒正常。可以按「打開打卡頁」試試看",
                                "--actions", "打開打卡頁", "--close-label", "關閉", "--timeout", "120"],
                               capture_output=True, text=True).stdout.strip()
            if r in ("打開打卡頁", "@CONTENTCLICKED"):
                subprocess.run([f"{S}/nycu_clock_open.sh", "打卡"], env={**os.environ, "NO_PUSH": "1", "NO_NOTIFY": "1"},
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        threading.Thread(target=run, daemon=True).start()

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send(self, code, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code); self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Cache-Control", "no-store")          # 頁面更新後，視窗一重開就是新版
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        if self.path == "/":
            self.send(200, open(PAGE, encoding="utf-8").read(), "text/html")
        elif self.path == "/api":
            self.send(200, json.dumps(snapshot(), ensure_ascii=False))
        elif re.fullmatch(r"/fig/[a-z0-9-]+\.jpg", self.path):     # 安裝指南的圖例
            try: self.send(200, open(f"{S}/guide_img/{self.path[5:]}", "rb").read(), "image/jpeg")
            except OSError: self.send(404, "{}")
        else:
            self.send(404, "{}")
    def do_POST(self):
        if self.headers.get("Origin") not in (None, "http://127.0.0.1:8765", "http://localhost:8765"):
            return self.send(403, '{"error":"forbidden"}')
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if self.path == "/api/update":
            try: return self.send(200, json.dumps(U.update(), ensure_ascii=False))
            except U.UpdateError as e: return self.send(409, json.dumps({"error": str(e)}, ensure_ascii=False))
        try:
            if self.path == "/api/config":
                c = check_config({**DEFAULTS, **body})
                json.dump(c, open(CONFIG, "w"), ensure_ascii=False, indent=1)
                subprocess.Popen([sys.executable, f"{S}/nycu_flow.py", "plan"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)   # 簽到預排跟著調整
            elif self.path.startswith("/api/action/"):
                do_action(self.path.rsplit("/", 1)[1], body)
            else:
                return self.send(404, "{}")
        except Refuse as e:
            return self.send(409, json.dumps({"error": str(e)}, ensure_ascii=False))
        except (urllib.error.URLError, OSError):            # 連不上 ntfy（網路切換、公共 Wi-Fi 登入頁等）
            return self.send(503, json.dumps({"error": "現在連不上推播伺服器，這次沒有成功。請確認網路（公共 Wi-Fi 可能要先登入），過一下再按一次。"}, ensure_ascii=False))
        except Exception as e:
            return self.send(400, json.dumps({"error": f"設定值不正確：{e}"}, ensure_ascii=False))
        self.send(200, json.dumps(snapshot(), ensure_ascii=False))

if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8765), H).serve_forever()
