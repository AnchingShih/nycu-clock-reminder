#!/usr/bin/env python3
"""排簽退提醒。用法：
  已簽到            → 現在簽到，4 小時後提醒
  已簽到 09:15      → 今天 09:15 簽到，13:15 提醒
  已簽到 --hours 3  → 現在簽到，3 小時後提醒（可配時間：已簽到 09:15 --hours 3）
  已簽到 --in 1m    → 測試用，1 分鐘後提醒
"""
import json, sys, os, urllib.request, datetime as dt
sys.path.insert(0, os.path.expanduser("~/Scripts"))
from nycu_push import post as push, signout_msg, TOPIC, CFG, cancel_signouts, scheduled_signouts
HOURS = CFG["hours"]

now = dt.datetime.now()
args = sys.argv[1:]
if "--hours" in args:
    i = args.index("--hours")
    try:
        HOURS = float(args[i + 1]); assert 0 < HOURS <= 12
    except (ValueError, IndexError, AssertionError):
        sys.exit("--hours 後面請接 0–12 的數字，例如 已簽到 --hours 3")
    del args[i:i + 2]
if args[:1] == ["--in"]:
    delay, label, start = args[1], f"{args[1]} 後（測試）", now
else:
    start = now
    if args:
        try:
            h, m = map(int, args[0].split(":"))
            start = now.replace(hour=h, minute=m, second=0, microsecond=0)
        except ValueError:
            sys.exit(f"看不懂「{args[0]}」，請用 24 小時制，例如 已簽到 09:15")
        if start > now:
            sys.exit(f"簽到時間 {args[0]} 比現在晚，請確認時間")
    due = start + dt.timedelta(hours=HOURS)
    if due <= now:
        sys.exit(f"{start:%H:%M} 簽到到現在已超過 {HOURS:g} 小時，請直接去簽退")
    delay, label = str(int(due.timestamp())), f"{due:%H:%M}"

pending = scheduled_signouts()
if pending:                                                  # 已經在計時：以原本的為準，不重新排
    import nycu_flow
    t0 = dt.datetime.fromtimestamp(pending[0]["time"])
    with nycu_flow.State() as st:
        st.update(stage="working", signout_at=t0.isoformat(timespec="seconds"))
    sys.exit(f"已經在計時，{t0:%H:%M} 提醒簽退，不重新排")
import nycu_flow
with nycu_flow.State() as st: sess = st.get("session", 1)
push(signout_msg(f"{HOURS:g}", f"{start:%H:%M}", delay=delay, session=sess))
import nycu_flow
with nycu_flow.State() as st:
    st["stage"] = "working"
    if args[:1] != ["--in"]:
        st["signout_at"] = due.isoformat(timespec="seconds")
        st["confirmed_signout"] = st["signout_at"]           # 背景程式看到這個就不再推一次
if args[:1] != ["--in"]:
    try: push({"topic": TOPIC, "title": "✅ 已開始計時", "tags": ["white_check_mark"], "message": f"{due:%H:%M} 提醒簽退"})
    except Exception: pass
    try: nycu_flow.plan_signins()                            # 當下就預排第 2 段簽到（不等 2 分鐘一次的檢查）
    except Exception: pass
print(f"已排好：{start:%H:%M} 簽到 → {label} 提醒簽退（手機會收到「已開始計時」）")
