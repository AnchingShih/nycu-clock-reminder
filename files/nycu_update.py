#!/usr/bin/env python3
"""設定頁「更新」按鈕：從 GitHub 下載新版程式換上去。
只換程式檔（nycu_*.py／.html／.sh）和安裝指南圖例（guide_img/*.jpg），設定、狀態、推播頻道都不碰。
下載後先核對 sha256、檢查語法，全部通過才替換；換到一半失敗就還原。
發新版：python3 nycu_update.py manifest <files 資料夾> <版本>，再把 files 傳上 GitHub
"""
import glob, hashlib, json, os, re, shutil, subprocess, sys, threading, time, urllib.error, urllib.request

S = os.path.expanduser("~/Scripts")
SOURCE = "https://raw.githubusercontent.com/AnchingShih/nycu-clock-reminder/main"
PROGRAMS = ["nycu_flow.py", "nycu_listener.py", "nycu_push.py", "nycu_settings.py", "nycu_settings.html",
            "nycu_signed.py", "nycu_clock_open.sh", "nycu_update.py"]
SAFE_NAME = re.compile(r"^(nycu_[a-z_]+\.(py|html|sh)|guide_img/[a-z0-9-]+\.jpg)$")   # nycu_config.json、頻道、狀態都不符合，不會被換掉
VERSION, SOURCE_FILE = f"{S}/nycu_version.txt", f"{S}/.nycu_update_source"
BACKUP, TMP = f"{S}/.nycu_update_backup", f"{S}/.nycu_update_tmp"

class UpdateError(Exception):
    """給使用者看的訊息"""

def current():
    try: return open(VERSION).read().strip() or "0"
    except OSError: return "0"

def source():
    try: s = open(SOURCE_FILE).read().strip()
    except OSError: s = SOURCE
    s = s.rstrip("/")
    if s and not s.startswith(("https://", "http://127.0.0.1:")):
        raise UpdateError("更新來源的網址不安全，沒有更新")
    return s

def fetch(url):
    try:
        with urllib.request.urlopen(url, timeout=20) as r: return r.read()
    except (urllib.error.URLError, OSError):
        raise UpdateError("連不上更新來源，這次沒有更新。請確認網路，過一下再按一次")

def key(v):
    try: return tuple(int(x) for x in str(v).split("."))
    except ValueError: raise UpdateError("更新來源的版本資料有問題，沒有更新")

def waiting_for_reply():
    """有提醒在等回覆時不更新：重啟背景程式會把正在跳的提醒收掉"""
    try: return json.load(open(f"{S}/.nycu_state.json")).get("stage") in ("asking_in", "asking_out")
    except (OSError, ValueError): return False

def restart():
    """等回應送出後才重啟（設定頁也在重啟名單裡）；每 2 分鐘的檢查下次執行就是新版，不用重啟"""
    def run():
        time.sleep(1)
        for n in ("listener", "settings"):
            subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.nycu.clock.{n}"])
    threading.Thread(target=run, daemon=True).start()

def install(files, latest):
    """files＝{檔名: 暫存路徑}。逐一換上；中途失敗就還原，一個都不留半套"""
    shutil.rmtree(BACKUP, ignore_errors=True); os.makedirs(BACKUP)
    done = []
    try:
        for name, tmp in files.items():
            dst = f"{S}/{name}"
            os.makedirs(os.path.dirname(dst), exist_ok=True); os.makedirs(os.path.dirname(f"{BACKUP}/{name}"), exist_ok=True)
            if os.path.exists(dst): shutil.copy2(dst, f"{BACKUP}/{name}")
            os.chmod(tmp, 0o755 if name.endswith((".py", ".sh")) else 0o644)
            os.replace(tmp, dst); done.append(name)
        with open(f"{TMP}/version", "w") as f: f.write(latest + "\n")
        os.replace(f"{TMP}/version", VERSION)
    except OSError:
        for name in done:
            old = f"{BACKUP}/{name}"
            if os.path.exists(old): shutil.copy2(old, f"{S}/{name}")
            else: os.remove(f"{S}/{name}")
        raise UpdateError("換檔案時出錯，已還原成原本的版本")

def update():
    """按鈕本體：回傳 status＝unconfigured（還沒設來源）／latest（已是最新）／updated（已更新）"""
    now, src = current(), source()
    if not src: return {"status": "unconfigured", "current": now}
    try:
        m = json.loads(fetch(f"{src}/files/manifest.json"))
        latest, wanted = m["version"], m["files"]
        assert isinstance(latest, str) and isinstance(wanted, dict) and wanted
    except (ValueError, KeyError, AssertionError):
        raise UpdateError("更新來源的版本資料有問題，沒有更新")
    if key(latest) <= key(now): return {"status": "latest", "current": now}
    if waiting_for_reply(): raise UpdateError("有提醒正在等你回覆，回覆後再按更新")
    bad = [n for n in wanted if not SAFE_NAME.match(n)]
    if bad: raise UpdateError("更新內容包含不該換的檔案，沒有更新")
    shutil.rmtree(TMP, ignore_errors=True); os.makedirs(TMP)
    try:
        files = {}
        for name, want in wanted.items():
            data = fetch(f"{src}/files/{name}")
            if hashlib.sha256(data).hexdigest() != want:
                raise UpdateError("下載的內容不完整，沒有更新。新版剛上傳的話，過幾分鐘再按一次")
            if name.endswith(".py"):
                try: compile(data, name, "exec")
                except (SyntaxError, ValueError): raise UpdateError("新版程式有錯誤，沒有更新")
            files[name] = f"{TMP}/{name}"
            os.makedirs(os.path.dirname(files[name]), exist_ok=True)
            with open(files[name], "wb") as f: f.write(data)
        install(files, latest)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    restart()
    return {"status": "updated", "current": latest}

def make_manifest(folder, version):
    key(version)
    files = {n: hashlib.sha256(open(f"{folder}/{n}", "rb").read()).hexdigest() 
             for n in PROGRAMS + sorted(os.path.relpath(x, folder) for x in glob.glob(f"{folder}/guide_img/*.jpg"))}
    json.dump({"version": version, "files": files}, open(f"{folder}/manifest.json", "w"), indent=1)
    open(f"{folder}/nycu_version.txt", "w").write(version + "\n")

if __name__ == "__main__":
    if sys.argv[1:2] == ["manifest"]: make_manifest(sys.argv[2], sys.argv[3])
    else: print(update())
