#!/usr/bin/env python3
"""ALEXA - voice phone assistant + coding/Linux agent for Termux (Android). One file, stdlib only.

Setup (Termux + Termux:API from F-Droid):
    pkg update && pkg install -y python tmux termux-api android-tools pulseaudio
    python alexa.py            # opens the Alexa app UI in Chrome (http://localhost:8765)
Then: Settings -> choose Groq / Gemini Flash -> paste API key.
Phone control (Settings -> "Accessibility"): enable Developer options > Wireless debugging,
tap "Pair device with code", enter it in Alexa's Settings (Pair), then Connect.
ALWAYS-LISTEN ("Hey Alexa" even with the screen off/other app): Settings -> turn on Always-listen, needs a Groq key
(speech is uploaded to Groq Whisper). Termux: disable battery optimisation, run `termux-wake-lock`.
Otherwise use Chrome (browser voice recognition only works while the Alexa page is on screen). Chrome menu -> "Add to Home screen" for an app icon.
"""
import array
import collections
import difflib
import functools
import html as htmlmod
import io
import json
import math
import os
import re
import secrets
import select
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave
import xml.etree.ElementTree as ET
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOME = Path.home() / ".alexa"
HOME.mkdir(parents=True, exist_ok=True)
CFG_FILE = HOME / "config.json"
MEM_FILE = HOME / "memory.md"
MAX_OUT = 5_000
MAX_STEPS = 50
PORT = 8765
TOKEN = secrets.token_urlsafe(16)
IS_TERMUX = bool(os.environ.get("TERMUX_VERSION")) or "com.termux" in os.environ.get("PREFIX", "")

PROVIDERS = {
    "groq": "https://api.groq.com/openai/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
}
DEFAULT_CFG = {
    "provider": "groq", "groq_key": "", "gemini_key": "",
    "model": {"groq": "openai/gpt-oss-120b", "gemini": "gemini-2.5-flash"},
    "lang": "bn-BD", "wake": True, "voice_reply": True,
    "phone_control": False, "auto_approve": True,
    "workspace": str(Path.home() / "projects"), "adb_addr": "",
    "bg_listen": False, "greeting": "জি বস, কী করতে পারি?",
    "stt_model": "whisper-large-v3-turbo", "stt_per_hour": 150,
}
CFG_LOCK = threading.Lock()


def load_cfg():
    cfg = json.loads(json.dumps(DEFAULT_CFG))
    try:
        for k, v in json.loads(CFG_FILE.read_text()).items():
            if k == "model" and isinstance(v, dict):
                cfg["model"].update(v)
            elif k in cfg:
                cfg[k] = v
    except Exception:
        pass
    return cfg


CFG = load_cfg()


def save_cfg():
    CFG_FILE.write_text(json.dumps(CFG, indent=1))
    try:
        CFG_FILE.chmod(0o600)  # keys live here
    except OSError:
        pass


def update_cfg(d):
    with CFG_LOCK:
        for k, v in d.items():
            if k in ("groq_key", "gemini_key"):
                if isinstance(v, str) and v.strip():
                    CFG[k] = v.strip()
            elif k == "provider" and v in PROVIDERS:
                CFG[k] = v
            elif k == "model" and isinstance(v, dict):
                for p, m in v.items():
                    if p in PROVIDERS and isinstance(m, str):
                        CFG["model"][p] = m.strip()
            elif k in ("wake", "voice_reply", "phone_control", "auto_approve", "bg_listen"):
                CFG[k] = bool(v)
            elif k in ("lang", "workspace", "adb_addr", "greeting", "stt_model") and isinstance(v, str):
                CFG[k] = v.strip()
            elif k == "stt_per_hour" and str(v).isdigit():
                CFG[k] = max(10, min(int(v), 2000))
        save_cfg()


def public_cfg():
    return {k: v for k, v in CFG.items() if not k.endswith("_key")}


def workspace() -> Path:
    p = Path(CFG["workspace"]).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p.resolve()


# ------------------------------------------------------------ events / control

EV, EV_N, EV_LOCK = [], [0], threading.Lock()
STOP = threading.Event()
RUN_LOCK = threading.Lock()
MESSAGES = []
PENDING = {}
SERVICE_ON = [False]


def emit(kind, **d):
    with EV_LOCK:
        EV_N[0] += 1
        EV.append({"i": EV_N[0], "k": kind, **d})
        if len(EV) > 1500:
            del EV[:500]


def events_since(n):
    with EV_LOCK:
        return [e for e in EV if e["i"] > n]


def ask_user(text, timeout=120):
    """Show Allow/Deny in the UI and wait. Denies on timeout or Stop."""
    cid = uuid.uuid4().hex[:8]
    ev = threading.Event()
    PENDING[cid] = {"ev": ev, "ok": False}
    emit("confirm", id=cid, text=text)
    notify_confirm(cid, text)
    t0 = time.time()
    while not ev.wait(0.4):
        if STOP.is_set() or time.time() - t0 > timeout:
            break
    if shutil.which("termux-notification-remove"):
        subprocess.Popen(["termux-notification-remove", "alexa-" + cid], stderr=subprocess.DEVNULL)
    return PENDING.pop(cid, {"ok": False})["ok"]


def answer_confirm(cid, ok):
    p = PENDING.get(cid)
    if p:
        p["ok"] = bool(ok)
        p["ev"].set()


# ------------------------------------------------------------ memory

def load_memory():
    return MEM_FILE.read_text()[-5000:] if MEM_FILE.exists() else "(no notes yet)"


def t_remember(note):
    with MEM_FILE.open("a") as f:
        f.write(f"- {time.strftime('%Y-%m-%d')} {note}\n")
    return "saved"


def log_task(task, summary):
    with MEM_FILE.open("a") as f:
        f.write(f"- {time.strftime('%Y-%m-%d %H:%M')} TASK: {task[:100]} -> {summary[:160]}\n")


# ------------------------------------------------------------ helpers


def trunc(s, n=MAX_OUT):
    return s if len(s) <= n else s[:n] + f"\n...[truncated {len(s) - n} chars]"


def tail(s, n=MAX_OUT):
    return s if len(s) <= n else f"[...first {len(s) - n} chars omitted]\n" + s[-n:]


def safe_path(p):
    ws = workspace()
    full = (ws / p).resolve()
    if full != ws and ws not in full.parents:
        raise ValueError(f"path outside workspace: {p}")
    return full


def http_get(url, timeout=20, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux) alexa", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def strip_tags(s):
    return htmlmod.unescape(re.sub(r"<[^>]+>", "", s)).strip()


# ------------------------------------------------------------ file tools


def confirm_edit(path, old, new):
    diff = list(difflib.unified_diff(old.splitlines(), new.splitlines(), f"a/{path}", f"b/{path}", n=1, lineterm=""))
    if not diff or CFG["auto_approve"]:
        return True
    return ask_user("Edit file?\n" + "\n".join(diff[:40]))


def t_list_dir(path="."):
    d = safe_path(path)
    items = sorted(d.iterdir(), key=lambda x: (not x.is_dir(), x.name))
    return "\n".join(f"{i.name}/" if i.is_dir() else i.name for i in items) or "(empty)"


def t_read_file(path):
    lines = safe_path(path).read_text(errors="replace").splitlines()
    return trunc("\n".join(f"{n:4}  {l}" for n, l in enumerate(lines, 1)))


def t_write_file(path, content):
    f = safe_path(path)
    old = f.read_text(errors="replace") if f.exists() else ""
    if not confirm_edit(path, old, content):
        return "User denied this edit."
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content)
    return f"wrote {len(content)} chars to {path}"


def t_edit_file(path, old_str, new_str):
    f = safe_path(path)
    text = f.read_text()
    n = text.count(old_str)
    if n != 1:
        return "ERROR: old_str not found" if n == 0 else f"ERROR: old_str appears {n} times; add more context"
    new = text.replace(old_str, new_str, 1)
    if not confirm_edit(path, text, new):
        return "User denied this edit."
    f.write_text(new)
    return f"edited {path}"


def t_grep(pattern, path="."):
    rx, base, hits = re.compile(pattern), safe_path(path), []
    ws = workspace()
    for f in ([base] if base.is_file() else base.rglob("*")):
        if not f.is_file() or any(p.startswith(".") or p in ("node_modules", "__pycache__", "venv") for p in f.relative_to(ws).parts):
            continue
        try:
            for n, line in enumerate(f.read_text().splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{f.relative_to(ws)}:{n}:{line.strip()}")
        except (UnicodeDecodeError, OSError):
            continue
    return trunc("\n".join(hits)) or "no matches"


# ------------------------------------------------------------ shell

DANGEROUS = re.compile(
    r"\brm\s+-\S*[rR]\S*\s+(/|~|\$HOME|\*)\S*(\s|$)|\bmkfs|\bdd\s+.*of=/dev|:\(\)\s*\{|\bshutdown\b|\breboot\b"
    r"|git\s+push\s+.*(--force|-f\b)|(curl|wget)[^|]*\|\s*(sudo\s+)?(ba)?sh|chmod\s+-R\s+777\s+/"
    r"|termux-sms-send|termux-telephony-call|termux-sms-list|termux-contact-list|termux-call-log"
)


def t_run_bash(command, timeout=300):
    timeout = min(int(timeout), 1800)
    risky = bool(DANGEROUS.search(command))
    if risky or not CFG["auto_approve"]:
        if not ask_user(("⚠ SENSITIVE\n" if risky else "") + f"Run command?\n$ {command}"):
            return "User denied this command."
    env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
    p = subprocess.Popen(["bash", "-c", command], cwd=workspace(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, text=True, env=env, start_new_session=True)
    out = []

    def reader():
        for line in p.stdout:
            out.append(line)

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    t0 = time.time()
    while p.poll() is None:
        if STOP.is_set() or time.time() - t0 > timeout:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
            why = "Stopped by user." if STOP.is_set() else f"ERROR: timed out after {timeout}s (use tmux for long jobs)."
            return why + "\n" + tail("".join(out), 1500)
        time.sleep(0.3)
    th.join(2)
    return tail(f"exit code {p.returncode}\n" + "".join(out))


def t_read_screen(lines=60, target=""):
    if not shutil.which("tmux"):
        return "ERROR: tmux missing (pkg install tmux)"
    cmd = ["tmux", "capture-pane", "-p", "-S", f"-{int(lines)}"] + (["-t", target] if target else [])
    r = subprocess.run(cmd, capture_output=True, text=True)
    return tail(r.stdout or r.stderr or "(empty)")


# ------------------------------------------------------------ web / github


def t_web_search(query):
    h = http_get("https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query))
    out = []
    for m in re.finditer(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?class="result__snippet"[^>]*>(.*?)</a>', h, re.S):
        url, title, snip = m.groups()
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        url = q["uddg"][0] if "uddg" in q else url
        out.append(f"{strip_tags(title)}\n{url}\n{strip_tags(snip)}")
        if len(out) >= 6:
            break
    return "\n\n".join(out) or "no results"


def t_fetch_url(url):
    h = http_get(url)
    if re.search(r"<(html|body|head)", h[:2000], re.I):
        h = re.sub(r"(?is)<(script|style|noscript|svg).*?</\1>", " ", h)
        h = re.sub(r"\s+", " ", htmlmod.unescape(re.sub(r"(?s)<[^>]+>", " ", h))).strip()
    return trunc(h)


def t_github_read(repo, path="", ref=""):
    url = f"https://api.github.com/repos/{repo}/contents/{path}" + (f"?ref={ref}" if ref else "")
    data = json.loads(http_get(url, headers={"Accept": "application/vnd.github+json"}))
    if isinstance(data, list):
        return "\n".join(f"{i['type']:4} {i['path']}" for i in data)
    import base64
    return trunc(base64.b64decode(data["content"]).decode("utf-8", "replace")) if data.get("encoding") == "base64" else trunc(json.dumps(data))


def open_url_on_phone(url):
    for cmd in (["termux-open-url", url], ["xdg-open", url]):
        if shutil.which(cmd[0]):
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
    return False


def t_open_browser(url):
    return f"opened {url}" if open_url_on_phone(url) else "ERROR: no opener (pkg install termux-api)"


# ------------------------------------------------------------ phone control (ADB)

LAST_UI, SCREEN = [], [""]
SENSITIVE = re.compile(
    r"\b(pay|send|buy|purchase|order|transfer|delete|remove|uninstall|erase|reset|confirm|submit|post|log ?out|sign ?out|install|call|dial)\b"
    r"|পাঠা|কিন|মুছ|পেমেন্ট|ডিলিট|কল|ইনস্টল", re.I)
KEYS = {"home": 3, "back": 4, "enter": 66, "recents": 187, "volume_up": 24, "volume_down": 25, "mute": 164,
        "play_pause": 85, "next": 87, "previous": 88, "wake": 224, "delete": 67, "tab": 61, "search": 84}
APPS = {"youtube": "com.google.android.youtube", "chrome": "com.android.chrome", "settings": "com.android.settings",
        "termux": "com.termux", "whatsapp": "com.whatsapp", "facebook": "com.facebook.katana",
        "messenger": "com.facebook.orca", "instagram": "com.instagram.android", "telegram": "org.telegram.messenger",
        "maps": "com.google.android.apps.maps", "gmail": "com.google.android.gm", "play store": "com.android.vending",
        "photos": "com.google.android.apps.photos", "tiktok": "com.zhiliaoapp.musically"}


def adb(*args, timeout=30):
    if not shutil.which("adb"):
        return 127, "", "adb not installed (pkg install android-tools)"
    cmd = ["adb"] + (["-s", CFG["adb_addr"]] if CFG["adb_addr"] else []) + list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return 124, "", "adb timed out"
    return r.returncode, r.stdout, r.stderr


def adb_connected():
    if not shutil.which("adb"):
        return False
    c, o, _ = adb("get-state", timeout=8)
    return c == 0 and o.strip() == "device"


def need_phone():
    if not CFG["phone_control"]:
        return "ERROR: Accessibility (phone control) is OFF. Tell the user to turn it ON in Alexa settings."
    if not adb_connected():
        return "ERROR: phone not connected via ADB. Tell the user: Settings > Phone connection > Pair, then Connect."
    return ""


def parse_ui(xml):
    items = []
    for n in ET.fromstring(xml).iter("node"):
        text, desc = n.get("text") or "", n.get("content-desc") or ""
        rid = (n.get("resource-id") or "").split("/")[-1]
        click = n.get("clickable") == "true"
        edit = "EditText" in (n.get("class") or "")
        b = re.findall(r"\d+", n.get("bounds") or "")
        if len(b) != 4 or not (text or desc or (click and rid) or edit):
            continue
        x1, y1, x2, y2 = map(int, b)
        items.append({"text": text, "desc": desc, "id": rid, "click": click, "edit": edit,
                      "x1": x1, "y1": y1, "x2": x2, "y2": y2, "cx": (x1 + x2) // 2, "cy": (y1 + y2) // 2})
    return items


def item_at(x, y):
    hit = [i for i in LAST_UI if i["x1"] <= x <= i["x2"] and i["y1"] <= y <= i["y2"]]
    return min(hit, key=lambda i: (i["x2"] - i["x1"]) * (i["y2"] - i["y1"])) if hit else None


def t_phone_screen():
    err = need_phone()
    if err:
        return err
    xml = ""
    for _ in range(3):
        c, o, e = adb("shell", "uiautomator", "dump", "/sdcard/alexa_ui.xml", timeout=25)
        if c == 0 and "dumped" in (o + e).lower():
            _, xml, _ = adb("exec-out", "cat", "/sdcard/alexa_ui.xml", timeout=20)
            if xml.strip().startswith("<"):
                break
        xml = ""
        time.sleep(1)
    if not xml:
        return "ERROR: could not read the screen (video/secure screens block it). Try phone_key back, or wait and retry."
    try:
        items = parse_ui(xml)
    except ET.ParseError:
        return "ERROR: screen XML unreadable"
    LAST_UI[:] = items
    if not SCREEN[0]:
        m = re.search(r"(\d+)x(\d+)", adb("shell", "wm", "size", timeout=8)[1])
        SCREEN[0] = f"{m.group(1)}x{m.group(2)}" if m else "unknown"
    m = re.search(r"Window\{\S+ \S+ ([\w.]+)/", adb("shell", "dumpsys window | grep mCurrentFocus", timeout=10)[1])
    lines = [f"app: {m.group(1) if m else '?'}   screen: {SCREEN[0]}"]
    for n, i in enumerate(items[:70], 1):
        label = (i["text"] or i["desc"] or i["id"]).replace("\n", " ")[:60]
        flag = "[input]" if i["edit"] else "[tap]" if i["click"] else ""
        lines.append(f'{n}. "{label}" {flag} @({i["cx"]},{i["cy"]})')
    return trunc("\n".join(lines))


def do_tap(x, y):
    time.sleep(0.2)
    c, o, e = adb("shell", "input", "tap", str(int(x)), str(int(y)))
    return "tapped" if c == 0 else f"ERROR: {e or o}"


def t_phone_tap_text(text):
    err = need_phone()
    if err:
        return err
    q = text.lower().strip()

    def find():
        c = [i for i in LAST_UI if q in (i["text"] + " " + i["desc"]).lower()]
        c.sort(key=lambda i: (not i["click"], (i["x2"] - i["x1"]) * (i["y2"] - i["y1"])))
        return c[0] if c else None

    it = find()
    if not it:
        t_phone_screen()
        it = find()
    if not it:
        return f"ERROR: nothing on screen contains {text!r}. Call phone_screen to see what is there."
    label = it["text"] or it["desc"]
    if SENSITIVE.search(label) and not ask_user(f"Alexa wants to tap: '{label}'. Allow?"):
        return "User denied this action."
    return do_tap(it["cx"], it["cy"]) + f" '{label}'"


def t_phone_tap(x, y):
    err = need_phone()
    if err:
        return err
    it = item_at(int(x), int(y))
    label = (it["text"] or it["desc"]) if it else ""
    if label and SENSITIVE.search(label) and not ask_user(f"Alexa wants to tap: '{label}'. Allow?"):
        return "User denied this action."
    return do_tap(x, y)


def t_phone_type(text):
    err = need_phone()
    if err:
        return err
    if not text.isascii():
        return "ERROR: only ASCII text can be typed. For searches use phone_open_url."
    c, o, e = adb("shell", "input", "text", shlex.quote(text.replace("%", "").replace(" ", "%s")))
    return "typed" if c == 0 else f"ERROR: {e or o}"


def t_phone_key(key):
    err = need_phone()
    if err:
        return err
    code = KEYS.get(key.lower())
    if code is None:
        return f"ERROR: key must be one of {', '.join(KEYS)}"
    c, o, e = adb("shell", "input", "keyevent", str(code))
    return "ok" if c == 0 else f"ERROR: {e or o}"


def t_phone_scroll(direction="down"):
    err = need_phone()
    if err:
        return err
    if not SCREEN[0]:
        t_phone_screen()
    m = re.match(r"(\d+)x(\d+)", SCREEN[0] or "")
    w, h = (int(m.group(1)), int(m.group(2))) if m else (1080, 2400)
    a, b = (0.72, 0.28) if direction == "down" else (0.28, 0.72)
    c, o, e = adb("shell", "input", "swipe", str(w // 2), str(int(h * a)), str(w // 2), str(int(h * b)), "350")
    return "scrolled" if c == 0 else f"ERROR: {e or o}"


def t_phone_open_app(name):
    err = need_phone()
    if err:
        return err
    n = name.lower().strip()
    pkg = APPS.get(n)
    if not pkg:
        _, o, _ = adb("shell", "pm", "list", "packages", timeout=20)
        pk = sorted((p.split(":", 1)[1].strip() for p in o.splitlines() if n.replace(" ", "") in p.lower().replace(".", "")), key=len)
        pkg = pk[0] if pk else None
    if not pkg:
        return f"ERROR: no installed app matches {name!r}"
    c, o, e = adb("shell", "monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1", timeout=20)
    if "No activities found" in (o + e) or c != 0:
        return f"ERROR: could not launch {pkg}: {(o + e)[:150]}"
    time.sleep(1.5)
    return f"opened {pkg}"


def t_phone_open_url(url):
    err = need_phone()
    if err:
        return err
    if not re.match(r"https?://", url):
        return "ERROR: only http(s) URLs"
    c, o, e = adb("shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", shlex.quote(url))
    time.sleep(1.5)
    return "opened" if c == 0 else f"ERROR: {e or o}"


# ------------------------------------------------------------ model layer (Groq / Gemini)


def tool(name, desc, props, req=()):
    return {"name": name, "description": desc, "input_schema": {
        "type": "object", "properties": {k: {"type": v} for k, v in props.items()}, "required": list(req)}}


TOOLS = [
    tool("list_dir", "List a folder in the workspace.", {"path": "string"}),
    tool("read_file", "Read a text file.", {"path": "string"}, ["path"]),
    tool("write_file", "Create/overwrite a file.", {"path": "string", "content": "string"}, ["path", "content"]),
    tool("edit_file", "Replace one unique occurrence of old_str with new_str.", {"path": "string", "old_str": "string", "new_str": "string"}, ["path", "old_str", "new_str"]),
    tool("grep", "Regex search in workspace files.", {"pattern": "string", "path": "string"}, ["pattern"]),
    tool("run_bash", "Run a shell command in Termux (workspace cwd). timeout seconds (max 1800).", {"command": "string", "timeout": "integer"}, ["command"]),
    tool("read_screen", "Read a tmux pane (output of background jobs).", {"lines": "integer", "target": "string"}),
    tool("web_search", "Search the web.", {"query": "string"}, ["query"]),
    tool("fetch_url", "Fetch a page/raw file as text.", {"url": "string"}, ["url"]),
    tool("github_read", "List/read a GitHub repo path. repo='owner/name'.", {"repo": "string", "path": "string", "ref": "string"}, ["repo"]),
    tool("open_browser", "Open a URL in the user's browser.", {"url": "string"}, ["url"]),
    tool("remember", "Save a durable note about the user/projects.", {"note": "string"}, ["note"]),
    tool("phone_screen", "PHONE: read the current screen (app + numbered visible elements with coordinates).", {}),
    tool("phone_tap_text", "PHONE: tap the element whose text/description contains this text.", {"text": "string"}, ["text"]),
    tool("phone_tap", "PHONE: tap at x,y.", {"x": "integer", "y": "integer"}, ["x", "y"]),
    tool("phone_type", "PHONE: type ASCII text into the focused field.", {"text": "string"}, ["text"]),
    tool("phone_key", "PHONE: press home, back, enter, recents, volume_up, volume_down, mute, play_pause, next, previous, wake, delete, tab, search.", {"key": "string"}, ["key"]),
    tool("phone_scroll", "PHONE: scroll 'down' or 'up'.", {"direction": "string"}),
    tool("phone_open_app", "PHONE: open an app by name (youtube, chrome, settings, termux, whatsapp, ...).", {"name": "string"}, ["name"]),
    tool("phone_open_url", "PHONE: open an http(s) URL in the matching app (YouTube links open YouTube).", {"url": "string"}, ["url"]),
]
HANDLERS = {
    "list_dir": t_list_dir, "read_file": t_read_file, "write_file": t_write_file, "edit_file": t_edit_file, "grep": t_grep,
    "run_bash": t_run_bash, "read_screen": t_read_screen, "web_search": t_web_search, "fetch_url": t_fetch_url,
    "github_read": t_github_read, "open_browser": t_open_browser, "remember": t_remember,
    "phone_screen": t_phone_screen, "phone_tap_text": t_phone_tap_text, "phone_tap": t_phone_tap, "phone_type": t_phone_type,
    "phone_key": t_phone_key, "phone_scroll": t_phone_scroll, "phone_open_app": t_phone_open_app, "phone_open_url": t_phone_open_url,
}


def run_tool(name, args):
    try:
        return HANDLERS[name](**args)
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"


def system_prompt():
    phone = "ON" if CFG["phone_control"] else "OFF"
    return f"""You are Alexa, a voice assistant and autonomous agent running in Termux on the user's Android phone. You also code.
Your FINAL reply is spoken aloud: 1-2 short sentences, no markdown, in the language the user spoke (Bangla if they spoke Bangla).
Work until the task is done AND verified. On any error: read it, fix the root cause, retry; after 3 failures change approach. Ask the user only if truly blocked.
Workspace (files/projects): {workspace()}. Phone control (Accessibility) is currently {phone}.
PHONE: use phone_open_app / phone_open_url to open things (YouTube search: https://www.youtube.com/results?search_query=WORDS). Call phone_screen before tapping, prefer phone_tap_text over coordinates, then check the result with phone_screen. Never type passwords, OTP or payment data. If phone control is OFF and you need it, say so.
TERMUX/LINUX: run commands with run_bash. Install a Linux distro: `pkg install -y proot-distro && proot-distro install ubuntu`; run inside it: `proot-distro login ubuntu -- bash -c '...'`. For long jobs (installs) start `tmux new -d -s NAME 'cmd; echo FINISHED'` and poll read_screen(target=NAME) until done. If the user wants to watch, phone_open_app termux.
NETWORK: Wi-Fi info `termux-wifi-connectioninfo` (needs Termux:API app), speed test via `pip install speedtest-cli` or curl timing against a speed endpoint. Security check = only the user's OWN phone/router/network: open ports (nmap), Wi-Fi encryption, outdated packages; explain findings simply. Never scan devices/networks the user does not own.
Never send SMS, make calls, post, pay or delete on your own; the system asks the user for those.
Use remember for durable facts (preferences, how to run things). Notes from before:
{load_memory()}"""


def trim_old(keep=4):
    for m in MESSAGES[:-keep]:
        if m["role"] == "user" and isinstance(m["content"], list):
            for b in m["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_result" and isinstance(b["content"], str) and len(b["content"]) > 400:
                    b["content"] = b["content"][:250] + "\n...[old output trimmed]"


def compact():
    if len(MESSAGES) > 60:
        i = len(MESSAGES) - 40
        while i < len(MESSAGES) and not (MESSAGES[i]["role"] == "user" and isinstance(MESSAGES[i]["content"], str)):
            i += 1
        if i < len(MESSAGES):
            del MESSAGES[:i]


class Blk:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def api_key():
    key = CFG[CFG["provider"] + "_key"]
    if not key:
        raise RuntimeError("API key নেই — Settings-এ Groq/Gemini key দিন")
    return key


def post_json(url, body):
    headers = {"Content-Type": "application/json", "Authorization": "Bearer " + api_key(),
               "User-Agent": "Mozilla/5.0 (X11; Linux) alexa/1.0"}
    for attempt in range(6):
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
        try:
            return urllib.request.urlopen(req, timeout=120)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            if e.code == 429 and attempt < 5:
                wait = min(float(e.headers.get("retry-after") or 3 * 2 ** attempt), 60)
                emit("info", t=f"rate limit — {wait:.0f}s অপেক্ষা")
                for _ in range(int(wait * 2)):
                    if STOP.is_set():
                        raise RuntimeError("stopped")
                    time.sleep(0.5)
                continue
            raise RuntimeError(f"HTTP {e.code}: {detail}")
    raise RuntimeError("rate limit: 6 বার চেষ্টা করেও হয়নি")


def list_models():
    base = PROVIDERS[CFG["provider"]]
    req = urllib.request.Request(base + "/models", headers={"Authorization": "Bearer " + api_key(), "User-Agent": "Mozilla/5.0 alexa/1.0"})
    data = json.loads(urllib.request.urlopen(req, timeout=30).read())
    return sorted(m["id"].replace("models/", "") for m in data.get("data", []))


def to_openai():
    out = [{"role": "system", "content": system_prompt()}]
    for m in MESSAGES:
        if m["role"] == "user":
            if isinstance(m["content"], str):
                out.append({"role": "user", "content": m["content"]})
            else:
                for b in m["content"]:
                    out.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": b["content"]})
        else:
            text = "".join(b.text for b in m["content"] if b.type == "text")
            calls = [{"id": b.id, "type": "function", "function": {"name": b.name, "arguments": json.dumps(b.input)}}
                     for b in m["content"] if b.type == "tool_use"]
            msg = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
    return out


def openai_tools():
    res = []
    for t in TOOLS:
        schema = dict(t["input_schema"])
        if not schema.get("required"):
            schema.pop("required", None)
        res.append({"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": schema}})
    return res


def chat():
    """One streaming model call. Returns (blocks, stop_reason)."""
    prov = CFG["provider"]
    body = {"model": CFG["model"][prov], "messages": to_openai(), "tools": openai_tools(), "stream": True, "max_tokens": 4096}
    resp = post_json(PROVIDERS[prov] + "/chat/completions", body)
    parts, calls = [], {}
    for raw in resp:
        if STOP.is_set():
            resp.close()
            break
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            ev = json.loads(data)
        except ValueError:
            continue
        if ev.get("error"):
            raise RuntimeError(str(ev["error"]))
        d = ((ev.get("choices") or [{}])[0].get("delta")) or {}
        if d.get("content"):
            parts.append(d["content"])
            emit("text", t=d["content"])
        for tc in d.get("tool_calls") or []:
            c = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": ""})
            c["id"] = tc.get("id") or c["id"]
            f = tc.get("function") or {}
            if f.get("name") and not c["name"]:
                c["name"] = f["name"]
            c["args"] += f.get("arguments") or ""
    blocks = [Blk(type="text", text="".join(parts))] if parts else []
    for i in sorted(calls):
        c = calls[i]
        try:
            args = json.loads(c["args"] or "{}")
        except ValueError:
            args = {}
        blocks.append(Blk(type="tool_use", id=c["id"] or f"call_{i}_{uuid.uuid4().hex[:6]}", name=c["name"], input=args))
    return blocks, ("tool_use" if calls else "end_turn")


def agent_turn():
    for _ in range(MAX_STEPS):
        if STOP.is_set():
            emit("info", t="থামানো হয়েছে")
            return ""
        trim_old()
        content, stop = chat()
        MESSAGES.append({"role": "assistant", "content": content})
        if stop != "tool_use":
            text = " ".join(b.text for b in content if b.type == "text").strip()
            emit("say", t=text[:300], bg=CUR_BG[0])
            return text
        results = []
        for b in content:
            if b.type == "tool_use":
                emit("tool", name=b.name, args=", ".join(f"{k}={str(v)[:40]!r}" for k, v in b.input.items()))
                out = "Stopped by user." if STOP.is_set() else run_tool(b.name, b.input)
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": out})
        MESSAGES.append({"role": "user", "content": results})
    emit("info", t="ধাপের সীমা শেষ — আবার বলুন 'চালিয়ে যাও'")
    return ""


def fix_dangling():
    """After Stop/error keep the history valid for the next request."""
    if MESSAGES and MESSAGES[-1]["role"] == "assistant":
        calls = [b for b in MESSAGES[-1]["content"] if b.type == "tool_use"]
        if calls:
            MESSAGES.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": b.id, "content": "Stopped by user."} for b in calls]})
    elif MESSAGES and MESSAGES[-1]["role"] == "user" and isinstance(MESSAGES[-1]["content"], str):
        MESSAGES.pop()


def _run(text, bg=False):
    summary = ""
    try:
        emit("user", t=text)
        compact()
        MESSAGES.append({"role": "user", "content": text})
        summary = agent_turn()
        log_task(text, summary)
    except Exception as e:
        emit("error", t=str(e))
        summary = "একটা সমস্যা হয়েছে, স্ক্রিনে দেখুন।"
    finally:
        fix_dangling()
        RUN_LOCK.release()
    if bg and summary:
        tts(summary)


def start_task(text, bg=False):
    if not RUN_LOCK.acquire(blocking=False):
        return False
    STOP.clear()
    CUR_BG[0] = bg
    threading.Thread(target=_run, args=(text, bg), daemon=True).start()
    return True


# ------------------------------------------------------------ service (notification, adb reconnect)


def service_start():
    SERVICE_ON[0] = True
    if shutil.which("termux-notification"):
        subprocess.Popen(["termux-notification", "--id", "alexa", "--title", "Alexa", "--content", ("শুনছি — \"Alexa\" বলুন" if CFG["bg_listen"] else "Tap to talk"), "--ongoing",
                          "--action", f"termux-open-url http://localhost:{PORT}"], stderr=subprocess.DEVNULL)
    if CFG["phone_control"] and CFG["adb_addr"] and shutil.which("adb"):
        threading.Thread(target=lambda: adb("connect", CFG["adb_addr"], timeout=15), daemon=True).start()
    if CFG["bg_listen"]:
        bg_start()


def service_stop():
    SERVICE_ON[0] = False
    STOP.set()
    bg_stop()
    for cid in list(PENDING):
        answer_confirm(cid, False)
    if shutil.which("termux-notification-remove"):
        subprocess.Popen(["termux-notification-remove", "alexa"], stderr=subprocess.DEVNULL)


def adb_action(op, addr, code=""):
    if not shutil.which("adb"):
        return False, "adb নেই: pkg install android-tools"
    if not addr:
        return False, "host:port দিন"
    subprocess.run(["adb", "start-server"], capture_output=True, timeout=20)
    if op == "pair":
        r = subprocess.run(["adb", "pair", addr, code], capture_output=True, text=True, timeout=40, stdin=subprocess.DEVNULL)
        msg = (r.stdout + r.stderr).strip()
        return "uccessfully" in msg, msg[:200]
    r = subprocess.run(["adb", "connect", addr], capture_output=True, text=True, timeout=25, stdin=subprocess.DEVNULL)
    msg = (r.stdout + r.stderr).strip()
    ok = "connected" in msg and "unable" not in msg and "failed" not in msg
    if ok:
        update_cfg({"adb_addr": addr})
    return ok, msg[:200]


# ------------------------------------------------------------ always-listen (background)

CUR_BG = [False]
MUTE_UNTIL = [0.0]
TTS_LOCK = threading.Lock()
BG_STOP = threading.Event()
BG_THREAD = [None]
BG_STATE = {"on": False, "uploads": [], "mictest": "", "info_t": 0.0}
WAKE_RE = re.compile(
    r"(?:hey\s+|hi\s+|hello\s+|ok(?:ay)?\s+|হেই\s+|হ্যালো\s+|হেলো\s+|ওকে\s+)?"
    r"(?:alexa|alexia|alexsa|elexa|aleksa|আলেক্সা|এলেক্সা|অ্যালেক্সা|আলেকসা|এলেকসা|অ্যালেকসা)[\s,.!?:।\-]*(.*)",
    re.I | re.S)
STOP_RE = re.compile(r"^\s*(stop|cancel|থামো|থামুন|বন্ধ)", re.I)


def tts(text):
    """Speak aloud (works in the background through Termux:API). Mutes the mic meanwhile."""
    text = (text or "").strip()[:300]
    if not text:
        return
    lang = "bn" if re.search(r"[\u0980-\u09FF]", text) else "en"
    if IS_TERMUX and shutil.which("termux-tts-speak"):
        cmd = ["termux-tts-speak", "-l", lang, text]
    elif shutil.which("espeak-ng"):
        cmd = ["espeak-ng", "-v", lang, text]
    else:
        return
    with TTS_LOCK:
        MUTE_UNTIL[0] = time.time() + 3600
        t0 = time.time()
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        except Exception:
            pass
        est, el = len(text) * 0.07, time.time() - t0
        MUTE_UNTIL[0] = time.time() + 0.9 + (max(0.0, est - el) if el < est * 0.5 else 0.0)  # command returned early?


def notify_confirm(cid, text):
    """Allow/Deny buttons in the notification shade (usable when Chrome is not on screen)."""
    if not shutil.which("termux-notification"):
        return
    base = f"curl -s -X POST -H 'X-Token: {TOKEN}' -H 'Content-Type: application/json' http://localhost:{PORT}/api/confirm -d "
    allow = base + shlex.quote(json.dumps({"id": cid, "ok": True}))
    deny = base + shlex.quote(json.dumps({"id": cid, "ok": False}))
    subprocess.Popen(["termux-notification", "--id", "alexa-" + cid, "--title", "Alexa: অনুমতি দরকার", "--content", text[:200],
                      "--priority", "high", "--button1", "Allow", "--button1-action", allow,
                      "--button2", "Deny", "--button2-action", deny], stderr=subprocess.DEVNULL)
    if CUR_BG[0]:
        threading.Thread(target=tts, args=("অনুমতি দরকার, নোটিফিকেশন দেখুন।",), daemon=True).start()


class Listener:
    """Raw 16 kHz mono mic stream + simple energy VAD (pure Python, no numpy)."""
    FRAME = 960  # bytes = 30 ms

    def __init__(self):
        self.proc, self.floor = None, None

    def open(self):
        if shutil.which("parec"):
            cmd = ["parec", "--format=s16le", "--rate=16000", "--channels=1", "--latency-msec=30"]
            if IS_TERMUX:
                if subprocess.run(["pulseaudio", "--check"], capture_output=True).returncode != 0:
                    subprocess.run(["pulseaudio", "--start", "--exit-idle-time=-1"], capture_output=True)
                    time.sleep(1.0)
                srcs = subprocess.run(["pactl", "list", "short", "sources"], capture_output=True, text=True).stdout
                if "OpenSL_ES_source" not in srcs:
                    subprocess.run(["pactl", "load-module", "module-sles-source"], capture_output=True)
                cmd.append("--device=OpenSL_ES_source")
        elif shutil.which("arecord"):
            cmd = ["arecord", "-q", "-f", "S16_LE", "-r", "16000", "-c", "1", "-t", "raw"]
        else:
            raise RuntimeError("mic টুল নেই: Termux → pkg install pulseaudio ; Linux → apt install pulseaudio-utils")
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        time.sleep(0.6)
        if self.proc.poll() is not None:
            raise RuntimeError("mic খোলা যায়নি (Termux মাইক অনুমতি / pulseaudio দেখুন)")

    def close(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def read_frame(self):
        d = self.proc.stdout.read(self.FRAME)
        return d if d and len(d) == self.FRAME else b""

    def flush(self):
        fd = self.proc.stdout.fileno()
        while select.select([fd], [], [], 0)[0]:
            if not os.read(fd, 4096):
                break

    @staticmethod
    def rms(data):
        a = array.array("h")
        a.frombytes(data)
        return math.sqrt(sum(x * x for x in a) / len(a))

    def next_segment(self, timeout=None):
        """One spoken phrase as PCM. None = timeout/stop, b'' = too short (ignore)."""
        pre, voiced, started, loud, quiet, t0 = collections.deque(maxlen=10), [], False, 0, 0, time.time()
        self.flush()
        while not BG_STOP.is_set():
            data = self.read_frame()
            if not data:
                raise RuntimeError("mic stream বন্ধ হয়ে গেছে")
            if time.time() < MUTE_UNTIL[0]:  # we are talking / just talked
                pre.clear()
                voiced, started, loud, quiet = [], False, 0, 0
                continue
            r = self.rms(data)
            if self.floor is None:
                self.floor = r
            speech = r > max(350.0, self.floor * 3 + 120)
            if not started:
                if not speech:
                    self.floor = 0.95 * self.floor + 0.05 * r
                pre.append(data)
                loud = loud + 1 if speech else 0
                if loud >= 3:
                    started, voiced, quiet = True, list(pre), 0
                elif timeout is not None and time.time() - t0 > timeout:
                    return None
            else:
                voiced.append(data)
                quiet = 0 if speech else quiet + 1
                if quiet >= 24 or len(voiced) > 8 * 33:  # ~0.7 s silence, or 8 s cap
                    break
        else:
            return None
        return b"".join(voiced) if len(voiced) >= 17 else b""


def multipart(fields, fname, fbytes):
    b = "----alexa" + uuid.uuid4().hex
    parts = [f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in fields.items()]
    parts.append(f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="{fname}"\r\nContent-Type: audio/wav\r\n\r\n'.encode() + fbytes + b"\r\n")
    parts.append(f"--{b}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={b}"


def stt(pcm):
    """Speech -> text with Groq Whisper (budgeted: CFG['stt_per_hour'] uploads)."""
    now, ups = time.time(), BG_STATE["uploads"]
    ups[:] = [t for t in ups if now - t < 3600]
    if len(ups) >= int(CFG["stt_per_hour"]):
        if now - BG_STATE["info_t"] > 120:
            BG_STATE["info_t"] = now
            emit("info", t="STT-র ঘণ্টাপ্রতি সীমা ছুঁয়েছে — একটু পরে আবার শুনবে")
        return ""
    ups.append(now)
    buf = io.BytesIO()
    w = wave.open(buf, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(16000)
    w.writeframes(pcm)
    w.close()
    body, ctype = multipart({"model": CFG["stt_model"], "response_format": "json", "temperature": "0",
                             "prompt": "Alexa, hey Alexa, hello Alexa, আলেক্সা, YouTube, Termux"}, "a.wav", buf.getvalue())
    req = urllib.request.Request(PROVIDERS["groq"] + "/audio/transcriptions", data=body, headers={
        "Authorization": "Bearer " + CFG["groq_key"], "Content-Type": ctype, "User-Agent": "Mozilla/5.0 (X11; Linux) alexa/1.0"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=30).read()).get("text", "").strip()
    except urllib.error.HTTPError as e:
        if e.code == 429:
            time.sleep(3)
        elif now - BG_STATE["info_t"] > 60:
            BG_STATE["info_t"] = now
            emit("error", t=f"STT HTTP {e.code}: {e.read().decode(errors='replace')[:150]}")
    except Exception as e:
        emit("error", t=f"STT: {e}")
    return ""


def bg_loop():
    if not CFG["groq_key"]:
        emit("error", t="Always-listen-এর জন্য Groq key লাগবে (Whisper দিয়ে কথা বোঝে)")
        return
    lis = Listener()
    try:
        lis.open()
        BG_STATE["on"] = True
        if shutil.which("termux-wake-lock"):
            subprocess.Popen(["termux-wake-lock"], stderr=subprocess.DEVNULL)
        emit("info", t='ব্যাকগ্রাউন্ডে শুনছি — "Alexa" বলুন')
        while not BG_STOP.is_set():
            seg = lis.next_segment()
            if not seg:
                continue
            text = stt(seg)
            if not text:
                continue
            emit("heard", t=text)
            m = WAKE_RE.search(text)
            if not m:
                continue
            cmd = m.group(1).strip(" ,.!?:।-")
            if STOP_RE.match(cmd):
                STOP.set()
                continue
            if not cmd:  # only "Hey Alexa" -> greet, then listen for the command
                tts(CFG["greeting"])
                t0, seg2 = time.time(), None
                while time.time() - t0 < 10 and not BG_STOP.is_set():
                    seg2 = lis.next_segment(timeout=max(0.5, 10 - (time.time() - t0)))
                    if seg2 or seg2 is None:
                        break
                cmd = stt(seg2) if seg2 else ""
                if not cmd:
                    continue
            if STOP_RE.match(cmd):
                STOP.set()
                continue
            if RUN_LOCK.locked():
                tts("আমি এখনো একটা কাজ করছি।")
                continue
            tts("ঠিক আছে।")
            start_task(cmd, bg=True)
    except Exception as e:
        emit("error", t=f"mic: {e}")
    finally:
        lis.close()
        BG_STATE["on"] = False
        if shutil.which("termux-wake-unlock"):
            subprocess.Popen(["termux-wake-unlock"], stderr=subprocess.DEVNULL)
        emit("info", t="শোনা বন্ধ")


def bg_start():
    if BG_THREAD[0] and BG_THREAD[0].is_alive():
        return
    BG_STOP.clear()
    BG_THREAD[0] = threading.Thread(target=bg_loop, daemon=True)
    BG_THREAD[0].start()


def bg_stop():
    BG_STOP.set()


def run_mictest():
    if BG_STATE["on"]:
        BG_STATE["mictest"] = "আগে Stop চাপুন (mic একসাথে দুই জায়গায় চলে না)"
        return
    BG_STATE["mictest"] = "টেস্ট চলছে… ৮ সেকেন্ড বলুন"
    lis = Listener()
    try:
        lis.open()
        t0, vals = time.time(), []
        while time.time() - t0 < 8:
            d = lis.read_frame()
            if not d:
                break
            vals.append(lis.rms(d))
        if not vals:
            raise RuntimeError("কোনো অডিও আসেনি")
        peak, avg = max(vals), sum(vals) / len(vals)
        ok = "✅ কথা ধরা পড়ছে" if peak > 600 else "❌ প্রায় নিঃশব্দ — mic অনুমতি বা ব্যাকগ্রাউন্ড ব্লক হতে পারে"
        BG_STATE["mictest"] = f"{ok} (peak {peak:.0f}, avg {avg:.0f}, জোরালো ফ্রেম {sum(1 for v in vals if v > 400)})"
    except Exception as e:
        BG_STATE["mictest"] = f"❌ {e}"
    finally:
        lis.close()


# ------------------------------------------------------------ web UI


@functools.lru_cache(maxsize=4)
def make_png(size):
    cx = cy = size / 2
    r2, in2 = (size * 0.46) ** 2, (size * 0.17) ** 2
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            d = (x - cx) ** 2 + (y - cy) ** 2
            row += bytes((255, 255, 255) if d <= in2 else (124, 92, 255) if d <= r2 else (10, 10, 15))
        rows.append(bytes(row))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))


MANIFEST = {"name": "Alexa", "short_name": "Alexa", "start_url": "/", "display": "standalone",
            "background_color": "#0a0a0f", "theme_color": "#0a0a0f",
            "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                      {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"}]}

PAGE = r'''<!doctype html>
<html lang="bn"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0a0a0f">
<link rel="manifest" href="/manifest.json">
<title>Alexa</title>
<style>
:root{--bg:#0a0a0f;--card:#16161e;--line:#272733;--fg:#fff;--mut:#a0a0b4;--acc:#7c5cff;--ok:#22c55e;--bad:#ef4444}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;height:100%;background:var(--bg);color:var(--fg);font:16px/1.4 system-ui,-apple-system,Roboto,sans-serif}
#app{display:flex;flex-direction:column;height:100%;padding-top:env(safe-area-inset-top)}
header{display:flex;align-items:center;gap:10px;padding:12px 14px;border-bottom:1px solid var(--line)}
.dot{width:10px;height:10px;border-radius:50%;background:#555;flex:none}
.dot.on{background:var(--ok)}.dot.work{background:var(--acc);animation:p 1s infinite}
@keyframes p{50%{opacity:.3}}
h1{font-size:20px;margin:0;font-weight:600}
#st{color:var(--mut);font-size:13px;flex:1;min-width:0}
button{font:inherit;color:var(--fg);background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 14px}
button.pri{background:var(--acc);border-color:var(--acc)}button.bad{background:var(--bad);border-color:var(--bad)}
#log{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:8px}
.m{max-width:88%;padding:10px 12px;border-radius:14px;white-space:pre-wrap;word-break:break-word}
.me{align-self:flex-end;background:var(--acc)}.ag{align-self:flex-start;background:var(--card);border:1px solid var(--line)}
.tl{align-self:flex-start;color:var(--mut);font:12px ui-monospace,monospace;max-width:95%;word-break:break-all}
.er{align-self:flex-start;color:#ff8a8a}
#sub{position:fixed;left:0;right:0;bottom:calc(76px + env(safe-area-inset-bottom));text-align:center;pointer-events:none;padding:0 16px;z-index:3}
#subt{display:none;background:rgba(0,0,0,.85);color:#fff;padding:8px 14px;border-radius:10px;font-size:18px;max-width:100%}
#bar{display:flex;gap:8px;align-items:center;padding:10px 12px calc(10px + env(safe-area-inset-bottom));border-top:1px solid var(--line)}
#bar input{flex:1;min-width:0;background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:22px;padding:11px 16px;font:inherit}
#cc{padding:10px 12px;font-weight:700}#cc.off{opacity:.4;text-decoration:line-through}
#mic{width:48px;height:48px;border-radius:50%;padding:0;font-size:20px;flex:none}#mic.live{background:var(--bad);border-color:var(--bad)}
.sheet{position:fixed;inset:0;background:var(--bg);overflow-y:auto;padding:16px;display:none;z-index:5}.sheet.show{display:block}
label{display:block;margin:14px 0 6px;color:var(--mut);font-size:13px}
.sheet input[type=text],.sheet input[type=password],.sheet select{width:100%;background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:10px;padding:11px;font:inherit}
.row{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:12px 0;border-bottom:1px solid var(--line)}
.row small{display:block;color:var(--mut)}
.sw{width:46px;height:26px;background:#3a3a48;border-radius:13px;position:relative;flex:none}.sw.on{background:var(--ok)}
.sw::after{content:"";position:absolute;top:3px;left:3px;width:20px;height:20px;border-radius:50%;background:#fff;transition:.15s}.sw.on::after{left:23px}
#cf{position:fixed;inset:0;background:rgba(0,0,0,.75);display:none;align-items:center;justify-content:center;z-index:9;padding:20px}#cf.show{display:flex}
.box{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px;width:100%;max-width:420px}
#cft{white-space:pre-wrap;word-break:break-word;margin:0 0 14px;font:14px ui-monospace,monospace;max-height:50vh;overflow:auto}
.btns{display:flex;gap:10px}.btns button{flex:1}
.hint{color:var(--mut);font-size:13px}
</style></head>
<body><div id="app">
<header><span class="dot" id="dot"></span><h1>Alexa</h1><span id="st">বন্ধ</span>
<button id="startBtn" class="pri">Start</button><button id="gear">⚙</button></header>
<div id="log"></div>
<div id="sub"><span id="subt"></span></div>
<div id="bar"><button id="cc" title="Subtitle on/off">CC</button><input id="txt" placeholder="Alexa, YouTube kholo…"><button id="mic">🎤</button><button id="send" class="pri">➤</button></div>
</div>

<div class="sheet" id="settings">
<div class="row" style="border:0"><h2 style="margin:0">Settings</h2><button id="closeS">✕</button></div>
<label>Model provider</label>
<select id="provider"><option value="groq">Groq</option><option value="gemini">Gemini Flash</option></select>
<label>Groq API key</label><input type="password" id="groq_key" autocomplete="off">
<label>Gemini API key</label><input type="password" id="gemini_key" autocomplete="off">
<label>Model (এই provider-এর)</label><input type="text" id="model" list="mlist"><datalist id="mlist"></datalist>
<button id="listM" style="margin-top:8px">Model list আনুন</button>
<label>কথা বোঝার ভাষা</label>
<select id="lang"><option value="bn-BD">বাংলা</option><option value="en-US">English (US)</option><option value="en-IN">English (India)</option><option value="hi-IN">हिन्दी</option></select>
<div class="row"><div>Accessibility (ফোন কন্ট্রোল)<small>ট্যাপ, টাইপ, অ্যাপ খোলা — ADB দিয়ে</small></div><div class="sw" id="sw_phone_control"></div></div>
<div class="row"><div>Wake word "Alexa"<small>বন্ধ থাকলে সব কথাই কমান্ড</small></div><div class="sw" id="sw_wake"></div></div>
<div class="row"><div>কথা বলে উত্তর<small>উত্তর পড়ে শোনায়</small></div><div class="sw" id="sw_voice_reply"></div></div>
<div class="row"><div>Auto mode<small>ফাইল/কমান্ডে অনুমতি ছাড়াই (বিপজ্জনকে সবসময় জিজ্ঞেস)</small></div><div class="sw" id="sw_auto_approve"></div></div>
<div class="row"><div>Always-listen (ব্যাকগ্রাউন্ড)<small>"Hey Alexa" বললেই জাগবে — Chrome খোলা না থাকলেও। কথা Groq Whisper-এ আপলোড হয় (Groq key লাগবে)</small></div><div class="sw" id="sw_bg_listen"></div></div>
<label>জাগলে যা বলবে</label><input type="text" id="greeting">
<button id="micBtn" style="margin-top:8px">Mic test (৮ সেকেন্ড)</button><div id="miclvl" class="hint"></div>
<label>Workspace folder</label><input type="text" id="workspace">
<h3>ফোন সংযোগ (ADB)</h3><div id="adbst" class="hint"></div>
<p class="hint">Developer options → Wireless debugging চালু করুন → "Pair device with code"</p>
<label>Pair: host:port (যেমন localhost:37123)</label><input type="text" id="pair_addr">
<label>Pairing code</label><input type="text" id="pair_code" inputmode="numeric">
<button id="pairBtn" style="margin-top:8px">Pair</button>
<label>Connect: host:port (Wireless debugging-এর মূল পেজে দেখানো)</label><input type="text" id="conn_addr">
<button id="connBtn" style="margin-top:8px">Connect</button>
<p class="hint">API key এই ফোনেই <code>~/.alexa/config.json</code>-এ থাকে।</p>
</div>

<div id="cf"><div class="box"><div id="cft"></div><div class="btns"><button id="cfn" class="bad">Deny</button><button id="cfy" class="pri">Allow</button></div></div></div>

<script>
const TOKEN="__TOKEN__";
const $=s=>document.querySelector(s);
const SR=window.SpeechRecognition||window.webkitSpeechRecognition;
let cfg={lang:'bn-BD',wake:true,voice_reply:true},since=0,listening=false,rec=null,oneRec=null,awaitUntil=0,speaking=false,
    subOn=localStorage.getItem('subOn')!=='0',subTimer=null,cur=null,confirmId=null,lastRunning=false,bgOn=false;

async function api(path,body){
  const o={headers:{'X-Token':TOKEN,'Content-Type':'application/json'}};
  if(body!==undefined){o.method='POST';o.body=JSON.stringify(body);}
  try{const r=await fetch(path,o);return await r.json();}catch(e){return {error:String(e)};}
}
function log(cls,txt){const d=document.createElement('div');d.className=(cls==='me'||cls==='ag')?'m '+cls:cls;d.textContent=txt;$('#log').appendChild(d);scrollLog();return d;}
function scrollLog(){const l=$('#log');l.scrollTop=l.scrollHeight;}
function sub(t){
  if(!subOn||!t)return;
  const s=$('#subt');s.textContent=t.slice(-180);s.style.display='inline-block';
  clearTimeout(subTimer);subTimer=setTimeout(()=>{s.style.display='none';},5000);
}
function beep(){try{const a=new (window.AudioContext||window.webkitAudioContext)(),o=a.createOscillator();o.connect(a.destination);o.frequency.value=880;o.start();setTimeout(()=>{o.stop();a.close();},110);}catch(_){}}

function speak(t){
  if(!cfg.voice_reply||!t||!('speechSynthesis' in window))return;
  speechSynthesis.cancel();
  const u=new SpeechSynthesisUtterance(t);u.lang=/[\u0980-\u09FF]/.test(t)?'bn-BD':'en-US';
  speaking=true;u.onend=u.onerror=()=>setTimeout(()=>{speaking=false;},800);speechSynthesis.speak(u);
}

function onEvent(e){
  switch(e.k){
    case 'user':log('me',e.t);sub('🧑 '+e.t);cur=null;break;
    case 'text':if(!cur)cur=log('ag','');cur.textContent+=e.t;sub(cur.textContent);scrollLog();break;
    case 'tool':log('tl','→ '+e.name+'('+e.args+')');cur=null;break;
    case 'say':if(!e.bg)speak(e.t);cur=null;break;
    case 'heard':sub('🎤 '+e.t);break;
    case 'error':log('er','⚠ '+e.t);sub('⚠ '+e.t);cur=null;break;
    case 'info':log('tl',e.t);sub(e.t);break;
    case 'confirm':confirmId=e.id;$('#cft').textContent=e.text;$('#cf').classList.add('show');speak('অনুমতি দরকার');break;
  }
}
async function poll(){
  const r=await api('/api/events?since='+since);
  if(!r.events)return;
  for(const e of r.events){since=Math.max(since,e.i);onEvent(e);}
  bgOn=!!r.bg;setStatus(r.running);
}
function setStatus(running){
  lastRunning=running;
  $('#dot').className='dot'+(running?' work':((listening||bgOn)?' on':''));
  $('#st').textContent=running?'কাজ করছে…':(bgOn?'ব্যাকগ্রাউন্ডে শুনছি — "Alexa" বলুন':(listening?'শুনছি — "Alexa" বলুন':'বন্ধ'));
  const on=listening||bgOn||running;$('#startBtn').textContent=on?'Stop':'Start';$('#startBtn').className=on?'bad':'pri';
}

const WAKE=/(?:hey\s+|হেই\s+|ওকে\s+)?(?:alexa|alexia|alexsa|elexa|আলেক্সা|এলেক্সা|অ্যালেক্সা|আলেকসা|এলেকসা|অ্যালেকসা)[\s,.!?:।\-]*(.*)/i;
function heard(t){
  if(!t||speaking)return;
  if(Date.now()<awaitUntil){awaitUntil=0;return send(t);}
  if(!cfg.wake)return send(t);
  const m=t.match(WAKE);
  if(!m){sub('… '+t);return;}
  const c=m[1].trim();
  if(c.length>1)send(c);else{awaitUntil=Date.now()+8000;sub('Alexa: বলুন…');beep();}
}
async function send(t){
  t=(t||'').trim();if(!t)return;
  if(/^(stop|থামো|থামুন|বন্ধ করো|cancel)/i.test(t)){await api('/api/abort',{});speechSynthesis.cancel();return;}
  const r=await api('/api/command',{text:t});
  if(r.error==='busy')sub('Alexa এখনো কাজ করছে — Stop চাপুন');
  else if(r.error)sub('⚠ '+r.error);
}
function spawn(){
  if(!listening)return;
  rec=new SR();rec.lang=cfg.lang||'bn-BD';rec.continuous=true;rec.interimResults=true;
  rec.onresult=e=>{
    if(speaking)return;
    for(let i=e.resultIndex;i<e.results.length;i++){
      const r=e.results[i],t=r[0].transcript.trim();
      if(!r.isFinal)sub('🎤 '+t);else heard(t);
    }
  };
  rec.onerror=e=>{if(e.error==='not-allowed'||e.error==='service-not-allowed'){listening=false;setStatus(lastRunning);alert('Microphone-এর অনুমতি দিন');}};
  rec.onend=()=>{if(listening)setTimeout(spawn,250);};
  try{rec.start();}catch(_){}
}
$('#startBtn').onclick=async()=>{
  if(listening||bgOn||lastRunning){
    listening=false;bgOn=false;try{rec&&rec.abort();}catch(_){}
    speechSynthesis.cancel();await api('/api/stop',{});setStatus(false);
  }else{
    if(!cfg.bg_listen){if(!SR)alert('Voice-এর জন্য Chrome লাগবে। লিখে কমান্ড দিতে পারবেন।');else{listening=true;spawn();}}
    await api('/api/start',{});setStatus(false);
  }
};
$('#mic').onclick=()=>{
  if(!SR){alert('Voice-এর জন্য Chrome লাগবে');return;}
  speechSynthesis.cancel();
  if(listening){awaitUntil=Date.now()+8000;sub('🎤 বলুন…');beep();return;}
  if(oneRec){oneRec.abort();return;}
  oneRec=new SR();oneRec.lang=cfg.lang||'bn-BD';oneRec.interimResults=true;
  oneRec.onresult=e=>{const r=e.results[e.results.length-1],t=r[0].transcript.trim();if(r.isFinal)send(t);else sub('🎤 '+t);};
  oneRec.onend=()=>{oneRec=null;$('#mic').classList.remove('live');};oneRec.onerror=()=>{};
  $('#mic').classList.add('live');oneRec.start();
};
$('#send').onclick=()=>{const v=$('#txt').value;$('#txt').value='';send(v);};
$('#txt').addEventListener('keydown',e=>{if(e.key==='Enter')$('#send').click();});
$('#cc').onclick=()=>{subOn=!subOn;localStorage.setItem('subOn',subOn?'1':'0');$('#cc').classList.toggle('off',!subOn);if(!subOn)$('#subt').style.display='none';};
$('#cc').classList.toggle('off',!subOn);
async function answer(ok){$('#cf').classList.remove('show');await api('/api/confirm',{id:confirmId,ok});}
$('#cfy').onclick=()=>answer(true);$('#cfn').onclick=()=>answer(false);

const save=o=>api('/api/settings',o).then(r=>{if(r.cfg)cfg=r.cfg;});
async function loadState(first){
  const s=await api('/api/state');if(!s.cfg)return;
  cfg=s.cfg;if(first)since=s.last;
  $('#provider').value=cfg.provider;$('#model').value=cfg.model[cfg.provider]||'';$('#lang').value=cfg.lang;$('#workspace').value=cfg.workspace;$('#greeting').value=cfg.greeting||'';$('#miclvl').textContent=s.mictest||'';
  $('#groq_key').placeholder=s.groq_set?'•••• সেভ করা আছে':'gsk_...';$('#gemini_key').placeholder=s.gemini_set?'•••• সেভ করা আছে':'AIza...';
  for(const k of ['wake','voice_reply','phone_control','auto_approve','bg_listen'])$('#sw_'+k).classList.toggle('on',!!cfg[k]);
  $('#adbst').textContent=s.adb?('✅ ফোন সংযুক্ত: '+s.adb_addr):('❌ ফোন সংযুক্ত নয়'+(s.adb_missing?' — pkg install android-tools':''));
  if(!$('#conn_addr').value)$('#conn_addr').value=cfg.adb_addr||'';
}
for(const k of ['wake','voice_reply','phone_control','auto_approve','bg_listen'])
  $('#sw_'+k).onclick=()=>{cfg[k]=!cfg[k];$('#sw_'+k).classList.toggle('on',cfg[k]);save({[k]:cfg[k]});};
$('#provider').onchange=async e=>{await save({provider:e.target.value});await loadState();};
$('#model').onchange=e=>save({model:{[cfg.provider]:e.target.value}});
$('#lang').onchange=e=>save({lang:e.target.value});
$('#workspace').onchange=e=>save({workspace:e.target.value});
$('#greeting').onchange=e=>save({greeting:e.target.value});
$('#micBtn').onclick=async()=>{await api('/api/mictest',{});alert('৮ সেকেন্ড বলুন (চাইলে Home চেপে অন্য অ্যাপে গিয়েও)। তারপর Settings আবার খুলে ফল দেখুন।');};
$('#groq_key').onchange=async e=>{await save({groq_key:e.target.value});e.target.value='';loadState();};
$('#gemini_key').onchange=async e=>{await save({gemini_key:e.target.value});e.target.value='';loadState();};
$('#listM').onclick=async()=>{
  const r=await api('/api/models');
  if(r.models){$('#mlist').innerHTML=r.models.map(m=>'<option value="'+m+'">').join('');alert(r.models.length+' টা model পাওয়া গেছে — Model ঘরে চাপ দিয়ে বাছুন');}
  else alert(r.error||'পাওয়া যায়নি (API key ঠিক আছে?)');
};
$('#pairBtn').onclick=async()=>{const r=await api('/api/adb',{op:'pair',addr:$('#pair_addr').value,code:$('#pair_code').value});alert(r.msg||r.error);loadState();};
$('#connBtn').onclick=async()=>{const r=await api('/api/adb',{op:'connect',addr:$('#conn_addr').value});alert(r.msg||r.error);loadState();};
$('#gear').onclick=()=>{$('#settings').classList.add('show');loadState();};
$('#closeS').onclick=()=>$('#settings').classList.remove('show');

loadState(true);setInterval(poll,600);
</script></body></html>
'''


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def guard(self, api):
        if self.headers.get("Host", "").split(":")[0] not in ("localhost", "127.0.0.1"):
            self.send(403, "bad host", "text/plain")
            return False
        if api and self.headers.get("X-Token") != TOKEN:
            self.send(403, {"error": "bad token"})
            return False
        return True

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p = u.path
        if not self.guard(p.startswith("/api/")):
            return
        if p == "/":
            return self.send(200, PAGE.replace("__TOKEN__", TOKEN), "text/html; charset=utf-8")
        if p == "/manifest.json":
            return self.send(200, MANIFEST, "application/manifest+json")
        if p in ("/icon-192.png", "/icon-512.png"):
            return self.send(200, make_png(192 if "192" in p else 512), "image/png")
        if p == "/api/state":
            return self.send(200, {"cfg": public_cfg(), "groq_set": bool(CFG["groq_key"]), "gemini_set": bool(CFG["gemini_key"]),
                                   "adb": adb_connected(), "adb_addr": CFG["adb_addr"], "adb_missing": not shutil.which("adb"),
                                   "last": EV_N[0], "running": RUN_LOCK.locked(), "bg": BG_STATE["on"], "mictest": BG_STATE["mictest"]})
        if p == "/api/events":
            since = int((urllib.parse.parse_qs(u.query).get("since") or ["0"])[0])
            return self.send(200, {"events": events_since(since), "running": RUN_LOCK.locked(), "bg": BG_STATE["on"]})
        if p == "/api/models":
            try:
                return self.send(200, {"models": list_models()})
            except Exception as e:
                return self.send(200, {"error": str(e)})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        if not self.guard(True):
            return
        if "application/json" not in self.headers.get("Content-Type", ""):
            return self.send(415, {"error": "json only"})
        try:
            d = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except ValueError:
            return self.send(400, {"error": "bad json"})
        p = self.path
        if p == "/api/command":
            text = str(d.get("text", "")).strip()[:2000]
            if not text:
                return self.send(200, {"error": "empty"})
            return self.send(200, {"ok": True} if start_task(text) else {"error": "busy"})
        if p == "/api/settings":
            update_cfg(d)
            return self.send(200, {"cfg": public_cfg()})
        if p == "/api/start":
            service_start()
            return self.send(200, {"ok": True})
        if p == "/api/stop":
            service_stop()
            return self.send(200, {"ok": True})
        if p == "/api/abort":
            STOP.set()
            return self.send(200, {"ok": True})
        if p == "/api/confirm":
            answer_confirm(str(d.get("id", "")), d.get("ok"))
            return self.send(200, {"ok": True})
        if p == "/api/mictest":
            threading.Thread(target=run_mictest, daemon=True).start()
            return self.send(200, {"ok": True})
        if p == "/api/adb":
            ok, msg = adb_action(d.get("op"), str(d.get("addr", "")).strip(), str(d.get("code", "")).strip())
            return self.send(200, {"ok": ok, "msg": msg})
        self.send(404, {"error": "not found"})


def main():
    global PORT
    srv = None
    for port in range(PORT, PORT + 20):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            PORT = port
            break
        except OSError:
            continue
    if not srv:
        sys.exit("no free port")
    srv.daemon_threads = True
    url = f"http://localhost:{PORT}"
    print(f"Alexa running → {url}\nChrome-এ খুলছে… (Ctrl+C = বন্ধ)")
    threading.Timer(0.8, lambda: open_url_on_phone(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        service_stop()
        print("\nbye")


if __name__ == "__main__":
    main()
