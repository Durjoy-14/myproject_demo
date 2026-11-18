"""
PC Controller - control your Windows PC from your iPhone (same Wi-Fi network).

Run this on the Windows PC. A QR code appears - scan it with your iPhone camera.
Two small helper packages install themselves the first time it runs
(segno for the QR code, pillow for the screen view). Nothing else to install.
"""

import ctypes
import hmac
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from ctypes import (POINTER, Structure, Union, byref, c_int, c_int32, c_size_t,
                    c_ssize_t, c_ubyte, c_uint, c_uint16, c_uint32, c_uint64,
                    c_void_p, sizeof)
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote

# ---------------------------------------------------------------- settings
PIN = "1234"          # <-- CHANGE THIS to your own PIN
PORT = 8765           # change only if another program already uses this port
DELAY = 15            # seconds before shutdown/restart happens (lets you cancel)
MAX_FAILS = 5         # wrong PIN attempts allowed before a temporary lockout
LOCKOUT_SECONDS = 60
QR_INCLUDES_PIN = True  # True: scanning the QR logs you in instantly (no PIN typing)
OPEN_BROWSER = True     # open the QR page on this PC automatically at start

# Set a feature to False to turn it off completely (the phone hides its tab).
FEATURES = {
    "power": True,    # shut down, restart, sleep, lock, timer, sign out...
    "media": True,    # volume and play/pause
    "input": True,    # trackpad and keyboard
    "screen": True,   # see the PC screen
    "apps": True,     # open apps and websites
    "info": True,     # CPU, memory, battery, disk
}

# Buttons on the Apps tab. "Button name": "what to run". Edit freely.
# A web address (https://...) opens in your default browser.
APPS = {
    "Notepad": "notepad",
    "Calculator": "calc",
    "File Explorer": "explorer",
    "Task Manager": "taskmgr",
    "Chrome": "chrome",
    "Edge": "msedge",
    "YouTube": "https://www.youtube.com",
    "Netflix": "https://www.netflix.com",
}
# -----------------------------------------------------------------------

IS_WINDOWS = sys.platform == "win32"
GB = 1024 ** 3

POWER_CMDS = {
    "shutdown": (["shutdown", "/s", "/t", str(DELAY)],
                 f"Shutting down in {DELAY} seconds. Tap Cancel to stop it."),
    "restart": (["shutdown", "/r", "/t", str(DELAY)],
                f"Restarting in {DELAY} seconds. Tap Cancel to stop it."),
    "hibernate": (["shutdown", "/h"], "Hibernating..."),
    "signout": (["shutdown", "/l"], "Signing out..."),
    "cancel": (["shutdown", "/a"], "Pending shutdown/restart cancelled."),
    "lock": (["rundll32.exe", "user32.dll,LockWorkStation"], "PC locked."),
    "sleep": (["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
              "Putting the PC to sleep..."),
}

# name -> (virtual-key code, needs the "extended key" flag)
KEYS = {
    "volup": (0xAF, False), "voldown": (0xAE, False), "mute": (0xAD, False),
    "playpause": (0xB3, False), "next": (0xB0, False), "prev": (0xB1, False),
    "enter": (0x0D, False), "backspace": (0x08, False), "tab": (0x09, False),
    "esc": (0x1B, False), "space": (0x20, False), "win": (0x5B, False),
    "delete": (0x2E, True), "home": (0x24, True), "end": (0x23, True),
    "left": (0x25, True), "up": (0x26, True), "right": (0x27, True), "down": (0x28, True),
}
MEDIA_KEYS = {"volup", "voldown", "mute", "playpause", "next", "prev"}

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004


class ActionError(Exception):
    def __init__(self, message, code=500):
        super().__init__(message)
        self.code = code


# ------------------------------------------------------- Windows structures
class POINT(Structure):
    _fields_ = [("x", c_int32), ("y", c_int32)]


class MOUSEINPUT(Structure):
    _fields_ = [("dx", c_int32), ("dy", c_int32), ("mouseData", c_uint32),
                ("dwFlags", c_uint32), ("time", c_uint32), ("dwExtraInfo", c_size_t)]


class KEYBDINPUT(Structure):
    _fields_ = [("wVk", c_uint16), ("wScan", c_uint16), ("dwFlags", c_uint32),
                ("time", c_uint32), ("dwExtraInfo", c_size_t)]


class HARDWAREINPUT(Structure):
    _fields_ = [("uMsg", c_uint32), ("wParamL", c_uint16), ("wParamH", c_uint16)]


class _INPUT_UNION(Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", c_uint32), ("u", _INPUT_UNION)]


class MEMORYSTATUSEX(Structure):
    _fields_ = [("dwLength", c_uint32), ("dwMemoryLoad", c_uint32),
                ("ullTotalPhys", c_uint64), ("ullAvailPhys", c_uint64),
                ("ullTotalPageFile", c_uint64), ("ullAvailPageFile", c_uint64),
                ("ullTotalVirtual", c_uint64), ("ullAvailVirtual", c_uint64),
                ("sullAvailExtendedVirtual", c_uint64)]


class SYSTEM_POWER_STATUS(Structure):
    _fields_ = [("ACLineStatus", c_ubyte), ("BatteryFlag", c_ubyte),
                ("BatteryLifePercent", c_ubyte), ("SystemStatusFlag", c_ubyte),
                ("BatteryLifeTime", c_uint32), ("BatteryFullLifeTime", c_uint32)]


class FILETIME(Structure):
    _fields_ = [("lo", c_uint32), ("hi", c_uint32)]


# ----------------------------------------------------------- Windows helpers
def require_windows():
    if not IS_WINDOWS:
        raise ActionError("This only works on Windows.")


_U = None


def _user32():
    """user32.dll with argument types set (cached)."""
    global _U
    if _U is None:
        require_windows()
        u = ctypes.windll.user32
        u.mouse_event.argtypes = [c_uint32, c_uint32, c_uint32, c_uint32, c_size_t]
        u.keybd_event.argtypes = [c_ubyte, c_ubyte, c_uint32, c_size_t]
        u.SendInput.argtypes = [c_uint, POINTER(INPUT), c_int]
        u.SendInput.restype = c_uint
        u.SetCursorPos.argtypes = [c_int, c_int]
        u.GetCursorPos.argtypes = [POINTER(POINT)]
        u.GetSystemMetrics.argtypes = [c_int]
        u.PostMessageW.argtypes = [c_void_p, c_uint, c_size_t, c_ssize_t]
        _U = u
    return _U


def press_key(name, repeat=1):
    u = _user32()
    if name == "alttab":
        u.keybd_event(0x12, 0, 0, 0)
        u.keybd_event(0x09, 0, 0, 0)
        u.keybd_event(0x09, 0, KEYEVENTF_KEYUP, 0)
        u.keybd_event(0x12, 0, KEYEVENTF_KEYUP, 0)
        return
    vk, ext = KEYS[name]
    flags = KEYEVENTF_EXTENDEDKEY if ext else 0
    for _ in range(repeat):
        u.keybd_event(vk, 0, flags, 0)
        u.keybd_event(vk, 0, flags | KEYEVENTF_KEYUP, 0)


def _kb_input(vk, scan, flags):
    i = INPUT()
    i.type = 1  # INPUT_KEYBOARD
    i.ki.wVk = vk
    i.ki.wScan = scan
    i.ki.dwFlags = flags
    return i


def type_text(text):
    u = _user32()
    data = text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-16-le")
    events = []
    for i in range(0, len(data), 2):
        unit = int.from_bytes(data[i:i + 2], "little")
        if unit == 0x0A:
            vk, scan, flags = 0x0D, 0, 0           # newline -> Enter key
        elif unit == 0x09:
            vk, scan, flags = 0x09, 0, 0           # tab -> Tab key
        else:
            vk, scan, flags = 0, unit, KEYEVENTF_UNICODE
        events.append(_kb_input(vk, scan, flags))
        events.append(_kb_input(vk, scan, flags | KEYEVENTF_KEYUP))
    for start in range(0, len(events), 100):
        chunk = events[start:start + 100]
        arr = (INPUT * len(chunk))(*chunk)
        u.SendInput(len(chunk), arr, sizeof(INPUT))
        time.sleep(0.005)


def mouse_rel(dx, dy):
    u = _user32()
    p = POINT()
    u.GetCursorPos(byref(p))
    u.SetCursorPos(int(p.x + dx), int(p.y + dy))


def mouse_abs(fx, fy):
    u = _user32()
    w, h = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
    fx, fy = min(max(fx, 0.0), 1.0), min(max(fy, 0.0), 1.0)
    u.SetCursorPos(int(fx * (w - 1)), int(fy * (h - 1)))


def mouse_click(kind):
    u = _user32()
    buttons = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}
    times = 1
    if kind == "double":
        kind, times = "left", 2
    if kind not in buttons:
        raise ActionError("Unknown click type.", 400)
    down, up = buttons[kind]
    for _ in range(times):
        u.mouse_event(down, 0, 0, 0, 0)
        u.mouse_event(up, 0, 0, 0, 0)


def mouse_scroll(notches):
    u = _user32()
    u.mouse_event(0x0800, 0, 0, (notches * 120) & 0xFFFFFFFF, 0)


# ------------------------------------------------------------------ actions
def run_cmd(cmd):
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        return subprocess.run(cmd, capture_output=True, text=True, creationflags=flags)
    except FileNotFoundError:
        raise ActionError("This only works on Windows.")


def _cmd_error(result):
    detail = (result.stderr or result.stdout or "").strip()
    if result.returncode == 1190 or "already been scheduled" in detail:
        return "A shutdown is already scheduled. Tap Cancel first."
    return detail or "Command failed"


def act_power(name):
    cmd, message = POWER_CMDS[name]
    result = run_cmd(cmd)
    if result.returncode != 0 and name != "sleep":
        if name == "cancel":
            return "Nothing was pending to cancel."
        raise ActionError(_cmd_error(result))
    return message


def act_timer(body):
    minutes = int(body.get("minutes", 0))
    if not 1 <= minutes <= 720:
        raise ActionError("Choose between 1 and 720 minutes.", 400)
    result = run_cmd(["shutdown", "/s", "/t", str(minutes * 60)])
    if result.returncode != 0:
        raise ActionError(_cmd_error(result))
    if minutes % 60 == 0:
        label = f"{minutes // 60} hour(s)"
    else:
        label = f"{minutes} minutes"
    return f"PC will shut down in {label}. Tap Cancel to stop it."


def act_screen_off(body):
    _user32().PostMessageW(0xFFFF, 0x0112, 0xF170, 2)  # WM_SYSCOMMAND, SC_MONITORPOWER, off
    return "Screen off. Moving the PC's mouse turns it back on."


def act_key(body):
    name = str(body.get("name", ""))
    repeat = max(1, min(int(body.get("repeat", 1)), 50))
    if name != "alttab" and name not in KEYS:
        raise ActionError("Unknown key.", 400)
    feature = "media" if name in MEDIA_KEYS else "input"
    if not FEATURES.get(feature):
        raise ActionError("This feature is turned off on the PC.", 403)
    press_key(name, repeat)
    return ""


def act_type(body):
    text = str(body.get("text", ""))
    if not text:
        raise ActionError("Nothing to type.", 400)
    if len(text) > 2000:
        raise ActionError("Text is too long (2000 characters max).", 400)
    type_text(text)
    return ""


def act_mouse(body):
    if body.get("abs"):
        fx, fy = body["abs"]
        mouse_abs(float(fx), float(fy))
    if body.get("dx") is not None or body.get("dy") is not None:
        dx = max(-3000.0, min(3000.0, float(body.get("dx") or 0)))
        dy = max(-3000.0, min(3000.0, float(body.get("dy") or 0)))
        mouse_rel(dx, dy)
    if body.get("scroll"):
        mouse_scroll(max(-50, min(50, int(body["scroll"]))))
    if body.get("click"):
        mouse_click(str(body["click"]))
    return ""


def act_open(body):
    url = str(body.get("url", "")).strip()
    app = str(body.get("app", "")).strip()
    if url:
        if len(url) > 2000 or not re.match(r"^https?://", url, re.I):
            raise ActionError("Only http and https links can be opened.", 400)
        webbrowser.open(url)
        return "Opened on the PC."
    if app in APPS:
        target = APPS[app]
        if re.match(r"^https?://", target, re.I):
            webbrowser.open(target)
        else:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            try:
                subprocess.Popen(["cmd", "/c", "start", "", target], creationflags=flags)
            except FileNotFoundError:
                raise ActionError("This only works on Windows.")
        return f"Opening {app}..."
    raise ActionError("Unknown app.", 400)


_last_cpu = None
_cpu_lock = threading.Lock()


def _filetime_int(ft):
    return (ft.hi << 32) | ft.lo


def _cpu_times():
    idle, kernel, user = FILETIME(), FILETIME(), FILETIME()
    ctypes.windll.kernel32.GetSystemTimes(byref(idle), byref(kernel), byref(user))
    return _filetime_int(idle), _filetime_int(kernel) + _filetime_int(user)  # kernel includes idle


def cpu_percent():
    global _last_cpu
    with _cpu_lock:
        prev = _last_cpu
        if prev is None:
            prev = _cpu_times()
            time.sleep(0.25)
        cur = _cpu_times()
        _last_cpu = cur
    didle, dtotal = cur[0] - prev[0], cur[1] - prev[1]
    if dtotal <= 0:
        return None
    return max(0, min(100, round(100 * (1 - didle / dtotal))))


def act_info(body):
    require_windows()
    k32 = ctypes.windll.kernel32
    data = {"host": socket.gethostname(), "time": time.strftime("%H:%M")}
    try:
        data["cpu"] = cpu_percent()
    except Exception:
        data["cpu"] = None
    try:
        m = MEMORYSTATUSEX()
        m.dwLength = sizeof(MEMORYSTATUSEX)
        k32.GlobalMemoryStatusEx(byref(m))
        data["ram_total_gb"] = round(m.ullTotalPhys / GB, 1)
        data["ram_used_gb"] = round((m.ullTotalPhys - m.ullAvailPhys) / GB, 1)
        data["ram_percent"] = int(m.dwMemoryLoad)
    except Exception:
        data["ram_total_gb"] = data["ram_used_gb"] = data["ram_percent"] = None
    try:
        s = SYSTEM_POWER_STATUS()
        k32.GetSystemPowerStatus(byref(s))
        has_battery = not (s.BatteryFlag & 128) and s.BatteryLifePercent != 255
        data["battery"] = int(s.BatteryLifePercent) if has_battery else None
        data["plugged"] = (s.ACLineStatus == 1) if s.ACLineStatus != 255 else None
    except Exception:
        data["battery"] = data["plugged"] = None
    try:
        drive = os.environ.get("SystemDrive", "C:") + "\\"
        du = shutil.disk_usage(drive)
        data["disk_name"] = drive.rstrip("\\")
        data["disk_total_gb"] = round(du.total / GB)
        data["disk_free_gb"] = round(du.free / GB)
    except Exception:
        data["disk_name"] = data["disk_total_gb"] = data["disk_free_gb"] = None
    try:
        k32.GetTickCount64.restype = c_uint64
        data["uptime_s"] = int(k32.GetTickCount64() // 1000)
    except Exception:
        data["uptime_s"] = None
    return data


def grab_screen():
    require_windows()
    try:
        from PIL import ImageGrab
    except ImportError:
        raise ActionError("Screen view needs Pillow. On the PC run: pip install pillow")
    return ImageGrab.grab()


def screen_jpeg(max_width=1280, quality=55):
    img = grab_screen().convert("RGB")
    if img.width > max_width:
        img = img.resize((max_width, int(img.height * max_width / img.width)))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


# action name -> (feature it belongs to, function(body) -> message or dict)
HANDLERS = {
    "timer": ("power", act_timer),
    "screenoff": ("power", act_screen_off),
    "key": (None, act_key),          # feature checked inside (media vs input)
    "type": ("input", act_type),
    "mouse": ("input", act_mouse),
    "open": ("apps", act_open),
    "info": ("info", act_info),
}
for _name in POWER_CMDS:
    HANDLERS[_name] = ("power", (lambda n: lambda body: act_power(n))(_name))

QUIET_PATHS = ("/api/mouse", "/api/screen", "/api/info", "/api/key")

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover, user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="PC Control">
<title>PC Control</title>
<style>
  :root { color-scheme: dark; --bg:#0f1115; --card:#1a1e27; --card2:#222733; --text:#f2f4f8;
          --muted:#8b93a4; --blue:#2471a3; --red:#c0392b; --amber:#d68910; --green:#1e8449; }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html, body { margin: 0; background: var(--bg); color: var(--text); }
  body { font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif; min-height: 100vh;
         padding: max(16px, env(safe-area-inset-top)) 16px calc(96px + env(safe-area-inset-bottom)); }
  .wrap { max-width: 480px; margin: 0 auto; }
  h1 { font-size: 26px; margin: 14px 0 4px; }
  h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted);
       margin: 22px 0 10px; font-weight: 600; }
  .sub { color: var(--muted); margin: 0 0 18px; font-size: 15px; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .grid3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
  .grid4 { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
  .wide { grid-column: 1 / -1; }
  button { border: 0; border-radius: 16px; padding: 18px 10px; font-size: 16px; font-weight: 600;
           color: #fff; background: var(--card2); display: flex; flex-direction: column;
           align-items: center; justify-content: center; gap: 6px; width: 100%; font-family: inherit; }
  button:active { transform: scale(.97); opacity: .85; }
  button .ico { font-size: 26px; line-height: 1; }
  button.small { padding: 14px 6px; font-size: 15px; }
  .danger { background: var(--red); } .warn { background: var(--amber); }
  .calm { background: var(--blue); } .good { background: var(--green); }
  input[type=text], input[type=password], input[type=url] {
    width: 100%; font-size: 17px; padding: 14px; border-radius: 14px; border: 1px solid #333a4a;
    background: #171a21; color: #fff; font-family: inherit; }
  #pin { font-size: 24px; text-align: center; letter-spacing: 8px; padding: 16px; margin-bottom: 14px; }
  .row { display: flex; gap: 10px; align-items: stretch; }
  .row > button { width: auto; flex: 0 0 auto; padding: 0 20px; }
  .row.fill > button { flex: 1; padding: 12px 6px; font-size: 14px; }
  #pad { height: 250px; border-radius: 22px; background: var(--card); border: 1px solid #2c3342;
         display: flex; align-items: center; justify-content: center; color: var(--muted);
         font-size: 14px; line-height: 1.6; text-align: center; padding: 20px;
         touch-action: none; user-select: none; -webkit-user-select: none; }
  #shot { width: 100%; border-radius: 14px; background: #000; display: block; min-height: 120px; }
  .card { background: var(--card); border-radius: 16px; padding: 14px; }
  .card .lbl { color: var(--muted); font-size: 13px; }
  .card .val { font-size: 20px; font-weight: 600; margin-top: 4px; }
  .bar { height: 6px; background: #2c3342; border-radius: 3px; margin-top: 10px; overflow: hidden; }
  .bar i { display: block; height: 100%; background: #4aa3ff; border-radius: 3px; }
  .tab { display: none; } .tab.active { display: block; }
  .hidden { display: none !important; }
  .link { background: none; color: var(--muted); font-size: 14px; padding: 22px; font-weight: 400; }
  #toast { position: fixed; left: 16px; right: 16px; bottom: calc(86px + env(safe-area-inset-bottom));
           background: #2b3140; padding: 14px 16px; border-radius: 14px; text-align: center;
           opacity: 0; transform: translateY(12px); transition: all .25s; pointer-events: none; z-index: 5; }
  #toast.show { opacity: 1; transform: none; }
  nav { position: fixed; left: 0; right: 0; bottom: 0; display: flex; z-index: 4;
        background: rgba(20, 23, 30, .96); -webkit-backdrop-filter: blur(12px); backdrop-filter: blur(12px);
        border-top: 1px solid #262c3a; padding: 6px 4px calc(6px + env(safe-area-inset-bottom)); }
  nav button { flex: 1; background: none; padding: 8px 2px; font-size: 11px; font-weight: 500;
               color: var(--muted); gap: 3px; border-radius: 10px; }
  nav button .ico { font-size: 22px; }
  nav button.active { color: #fff; } nav button.active .ico { color: #4aa3ff; }
</style>
</head>
<body>
<div class="wrap">

  <section id="login" class="hidden">
    <h1>PC Control</h1>
    <p class="sub">Enter your PIN to continue</p>
    <input id="pin" type="password" inputmode="numeric" autocomplete="off" placeholder="PIN">
    <button class="calm" id="unlockBtn">Unlock</button>
  </section>

  <div id="panel" class="hidden">

    <section class="tab" id="tab-power">
      <h1>Power</h1>
      <p class="sub" id="host">Connected</p>
      <div class="grid">
        <button class="danger" data-action="shutdown" data-confirm="Shut down the PC?"><span class="ico">&#9211;</span>Shut down</button>
        <button class="warn" data-action="restart" data-confirm="Restart the PC?"><span class="ico">&#8635;</span>Restart</button>
        <button class="calm" data-action="sleep" data-confirm="Put the PC to sleep?"><span class="ico">&#9790;</span>Sleep</button>
        <button class="calm" data-action="lock"><span class="ico">&#128274;</span>Lock</button>
        <button data-action="hibernate" data-confirm="Hibernate the PC?"><span class="ico">&#10052;</span>Hibernate</button>
        <button data-action="signout" data-confirm="Sign out? Unsaved work will be lost."><span class="ico">&#10162;</span>Sign out</button>
        <button class="wide" data-action="screenoff"><span class="ico">&#9681;</span>Turn screen off</button>
      </div>
      <h2>Shut down timer</h2>
      <div class="grid4">
        <button class="small" data-timer="15">15 min</button>
        <button class="small" data-timer="30">30 min</button>
        <button class="small" data-timer="60">1 hour</button>
        <button class="small" data-timer="120">2 hours</button>
      </div>
      <div class="grid" style="margin-top:14px">
        <button class="good wide" data-action="cancel"><span class="ico">&#10006;</span>Cancel shutdown / restart</button>
      </div>
      <button class="link" id="logout">Forget PIN on this phone</button>
    </section>

    <section class="tab" id="tab-media">
      <h1>Media</h1>
      <h2>Volume</h2>
      <div class="grid3">
        <button data-key="voldown" data-repeat="3"><span class="ico">&#128265;</span>Vol -</button>
        <button data-key="mute"><span class="ico">&#128263;</span>Mute</button>
        <button data-key="volup" data-repeat="3"><span class="ico">&#128266;</span>Vol +</button>
      </div>
      <h2>Playback</h2>
      <div class="grid3">
        <button data-key="prev"><span class="ico">&#9198;</span>Previous</button>
        <button class="calm" data-key="playpause"><span class="ico">&#9199;</span>Play / Pause</button>
        <button data-key="next"><span class="ico">&#9197;</span>Next</button>
      </div>
    </section>

    <section class="tab" id="tab-mouse">
      <h1>Mouse &amp; keyboard</h1>
      <p class="sub">Use your phone as a trackpad</p>
      <div id="pad">Drag to move the pointer<br>Tap to click &middot; two-finger tap to right-click<br>Drag with two fingers to scroll</div>
      <div class="grid3" style="margin-top:12px">
        <button class="small" data-click="left">Left click</button>
        <button class="small" data-click="double">Double</button>
        <button class="small" data-click="right">Right click</button>
      </div>
      <h2>Type on the PC</h2>
      <div class="row">
        <input id="typeText" type="text" placeholder="Type here, then Send" autocomplete="off" autocapitalize="off" autocorrect="off">
        <button class="calm" id="typeBtn">Send</button>
      </div>
      <h2>Keys</h2>
      <div class="grid3">
        <button class="small" data-key="esc">Esc</button>
        <button class="small" data-key="tab">Tab</button>
        <button class="small" data-key="backspace">&#9003;</button>
        <button class="small" data-key="enter">Enter</button>
        <button class="small" data-key="space">Space</button>
        <button class="small" data-key="delete">Del</button>
        <span></span><button class="small" data-key="up">&#9650;</button><span></span>
        <button class="small" data-key="left">&#9664;</button>
        <button class="small" data-key="down">&#9660;</button>
        <button class="small" data-key="right">&#9654;</button>
        <button class="small" data-key="win">Win</button>
        <button class="small" data-key="alttab">Alt+Tab</button>
      </div>
    </section>

    <section class="tab" id="tab-screen">
      <h1>Screen</h1>
      <div class="row fill" style="margin-bottom:10px">
        <button class="calm" id="shotBtn">Refresh</button>
        <button id="autoBtn">Auto: off</button>
        <button id="tapBtn">Tap to click: off</button>
      </div>
      <img id="shot" alt="PC screen">
      <p class="sub" style="margin-top:10px">Shows your main monitor. Turn on &quot;Tap to click&quot; to click where you tap.</p>
    </section>

    <section class="tab" id="tab-apps">
      <h1>Apps</h1>
      <h2>Open a website on the PC</h2>
      <div class="row">
        <input id="url" type="url" placeholder="youtube.com" autocomplete="off" autocapitalize="off" autocorrect="off">
        <button class="calm" id="openUrl">Open</button>
      </div>
      <h2>Launch</h2>
      <div class="grid" id="apps"></div>
    </section>

    <section class="tab" id="tab-info">
      <h1>PC status</h1>
      <p class="sub">Updates every few seconds</p>
      <div class="grid" id="infoGrid"></div>
    </section>

  </div>
</div>

<nav id="nav" class="hidden">
  <button data-tab="power"><span class="ico">&#9211;</span>Power</button>
  <button data-tab="media"><span class="ico">&#9835;</span>Media</button>
  <button data-tab="mouse"><span class="ico">&#10021;</span>Mouse</button>
  <button data-tab="screen"><span class="ico">&#9635;</span>Screen</button>
  <button data-tab="apps"><span class="ico">&#8862;</span>Apps</button>
  <button data-tab="info"><span class="ico">&#9432;</span>Status</button>
</nav>
<div id="toast"></div>

<script>
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));
  const DELAY = __DELAY__;
  const TAB_FEATURE = { power: "power", media: "media", mouse: "input", screen: "screen", apps: "apps", info: "info" };
  let pin = "";
  try { pin = localStorage.getItem("pcpin") || ""; } catch (e) {}
  let features = {};
  let current = null;
  let autoOn = false, tapOn = false, shotBusy = false, shotUrl = null;
  let autoTimer = null, infoTimer = null;

  function toast(msg) {
    const t = $("#toast");
    t.textContent = msg;
    t.classList.add("show");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => t.classList.remove("show"), 3500);
  }

  function stopTimers() {
    clearTimeout(autoTimer); autoTimer = null;
    clearInterval(infoTimer); infoTimer = null;
  }

  function show(which) {
    $("#login").classList.toggle("hidden", which !== "login");
    $("#panel").classList.toggle("hidden", which !== "panel");
    $("#nav").classList.toggle("hidden", which !== "panel");
    if (which !== "panel") stopTimers();
  }

  async function api(action, payload, usePin) {
    try {
      const r = await fetch("/api/" + action, {
        method: "POST",
        headers: { "X-PIN": encodeURIComponent(usePin === undefined ? pin : usePin), "Content-Type": "application/json" },
        body: JSON.stringify(payload || {})
      });
      let data = {};
      try { data = await r.json(); } catch (e) {}
      return { status: r.status, data };
    } catch (e) {
      return { status: 0, data: { message: "Can't reach the PC. Is it on and on the same Wi-Fi?" } };
    }
  }

  async function act(action, payload, quiet) {
    const r = await api(action, payload);
    if (r.status === 401) { show("login"); toast("PIN no longer valid"); }
    else if (r.status !== 200) toast(r.data.message || "Something went wrong");
    else if (!quiet && r.data.message) toast(r.data.message);
    return r;
  }

  /* ---------- tabs ---------- */
  function enterTab(name) {
    if (name === "screen") refreshShot();
    if (name === "info") { loadInfo(); infoTimer = setInterval(loadInfo, 3000); }
  }

  function showTab(name) {
    current = name;
    stopTimers();
    $$(".tab").forEach((s) => s.classList.toggle("active", s.id === "tab-" + name));
    $$("#nav button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
    window.scrollTo(0, 0);
    enterTab(name);
  }

  function setupTabs() {
    let first = null;
    $$("#nav button").forEach((b) => {
      const on = features[TAB_FEATURE[b.dataset.tab]] !== false;
      b.classList.toggle("hidden", !on);
      if (on && !first) first = b.dataset.tab;
    });
    const keep = current && features[TAB_FEATURE[current]] !== false ? current : null;
    showTab(keep || first || "power");
  }

  $$("#nav button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stopTimers();
    else if (current && !$("#panel").classList.contains("hidden")) enterTab(current);
  });

  /* ---------- login ---------- */
  async function tryUnlock(p) {
    const r = await api("check", {}, p);
    if (r.status === 200) {
      pin = p;
      try { localStorage.setItem("pcpin", p); } catch (e) {}
      features = r.data.features || {};
      $("#host").textContent = "Connected to " + (r.data.host || "your PC");
      buildApps(r.data.apps || []);
      show("panel");
      setupTabs();
      return true;
    }
    show("login");
    if (r.status !== 0 && p) toast(r.data.message || "Wrong PIN");
    if (r.status === 0) toast(r.data.message);
    return false;
  }

  $("#unlockBtn").addEventListener("click", () => tryUnlock($("#pin").value.trim()));
  $("#pin").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#unlockBtn").click(); });
  $("#logout").addEventListener("click", () => {
    try { localStorage.removeItem("pcpin"); } catch (e) {}
    pin = "";
    show("login");
  });

  /* ---------- power ---------- */
  $("[data-action=shutdown]").dataset.confirm = "Shut down the PC in " + DELAY + " seconds?";
  $("[data-action=restart]").dataset.confirm = "Restart the PC in " + DELAY + " seconds?";
  $$("button[data-action]").forEach((b) => b.addEventListener("click", async () => {
    const msg = b.dataset.confirm;
    if (msg && !confirm(msg)) return;
    await act(b.dataset.action, {});
  }));
  $$("button[data-timer]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("Shut down the PC in " + b.textContent.trim() + "?")) return;
    await act("timer", { minutes: Number(b.dataset.timer) });
  }));

  /* ---------- keys (media + keyboard) ---------- */
  $$("button[data-key]").forEach((b) => b.addEventListener("click", () =>
    act("key", { name: b.dataset.key, repeat: Number(b.dataset.repeat || 1) }, true)));

  /* ---------- trackpad ---------- */
  const GAIN = 1.8;
  const pad = $("#pad");
  let pdx = 0, pdy = 0, moving = false;
  let tStart = 0, tMax = 0, moved = 0, last = { x: 0, y: 0 }, scrollAcc = 0;

  function center(touches) {
    let x = 0, y = 0;
    for (const t of touches) { x += t.clientX; y += t.clientY; }
    return { x: x / touches.length, y: y / touches.length };
  }

  async function flushMove() {
    if (moving) return;
    moving = true;
    while (Math.abs(pdx) >= 1 || Math.abs(pdy) >= 1) {
      const dx = Math.trunc(pdx), dy = Math.trunc(pdy);
      pdx -= dx; pdy -= dy;
      await act("mouse", { dx, dy }, true);
    }
    moving = false;
  }

  pad.addEventListener("touchstart", (e) => {
    e.preventDefault();
    if (e.touches.length === 1) { tStart = Date.now(); tMax = 1; moved = 0; scrollAcc = 0; }
    tMax = Math.max(tMax, e.touches.length);
    last = center(e.touches);
  }, { passive: false });

  pad.addEventListener("touchmove", (e) => {
    e.preventDefault();
    const c = center(e.touches);
    const dx = c.x - last.x, dy = c.y - last.y;
    last = c;
    moved += Math.abs(dx) + Math.abs(dy);
    if (e.touches.length >= 2) {
      scrollAcc += dy;
      const notches = Math.trunc(scrollAcc / 24);
      if (notches) { scrollAcc -= notches * 24; act("mouse", { scroll: notches }, true); }
    } else {
      pdx += dx * GAIN; pdy += dy * GAIN;
      flushMove();
    }
  }, { passive: false });

  pad.addEventListener("touchend", (e) => {
    e.preventDefault();
    if (e.touches.length === 0) {
      if (moved < 10 && Date.now() - tStart < 300) act("mouse", { click: tMax >= 2 ? "right" : "left" }, true);
    } else {
      last = center(e.touches);
    }
  }, { passive: false });

  $$("button[data-click]").forEach((b) => b.addEventListener("click", () => act("mouse", { click: b.dataset.click }, true)));

  $("#typeBtn").addEventListener("click", async () => {
    const t = $("#typeText").value;
    if (!t) return;
    const r = await act("type", { text: t }, true);
    if (r.status === 200) { $("#typeText").value = ""; toast("Typed on the PC"); }
  });
  $("#typeText").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#typeBtn").click(); });

  /* ---------- screen ---------- */
  function stopAuto() {
    autoOn = false;
    clearTimeout(autoTimer); autoTimer = null;
    $("#autoBtn").textContent = "Auto: off";
  }

  async function refreshShot() {
    if (shotBusy) return;
    shotBusy = true;
    try {
      const r = await fetch("/api/screen", {
        method: "POST",
        headers: { "X-PIN": encodeURIComponent(pin), "Content-Type": "application/json" },
        body: "{}"
      });
      if (r.status === 200) {
        const url = URL.createObjectURL(await r.blob());
        $("#shot").src = url;
        if (shotUrl) URL.revokeObjectURL(shotUrl);
        shotUrl = url;
      } else {
        let d = {};
        try { d = await r.json(); } catch (e) {}
        if (r.status === 401) show("login");
        toast(d.message || "Couldn't get the screen");
        stopAuto();
      }
    } catch (e) {
      toast("Can't reach the PC.");
      stopAuto();
    }
    shotBusy = false;
    if (autoOn && current === "screen" && !document.hidden) {
      clearTimeout(autoTimer);
      autoTimer = setTimeout(refreshShot, 1500);
    }
  }

  $("#shotBtn").addEventListener("click", refreshShot);
  $("#autoBtn").addEventListener("click", () => {
    autoOn = !autoOn;
    $("#autoBtn").textContent = "Auto: " + (autoOn ? "on" : "off");
    if (autoOn) refreshShot(); else { clearTimeout(autoTimer); autoTimer = null; }
  });
  $("#tapBtn").addEventListener("click", () => {
    tapOn = !tapOn;
    $("#tapBtn").textContent = "Tap to click: " + (tapOn ? "on" : "off");
  });
  $("#shot").addEventListener("click", (e) => {
    if (!tapOn) return;
    const r = e.target.getBoundingClientRect();
    const fx = (e.clientX - r.left) / r.width, fy = (e.clientY - r.top) / r.height;
    act("mouse", { abs: [fx, fy], click: "left" }, true).then(() => setTimeout(refreshShot, 400));
  });

  /* ---------- apps ---------- */
  function buildApps(names) {
    const g = $("#apps");
    g.textContent = "";
    names.forEach((n) => {
      const b = document.createElement("button");
      b.className = "small";
      b.textContent = n;
      b.addEventListener("click", () => act("open", { app: n }));
      g.appendChild(b);
    });
  }

  $("#openUrl").addEventListener("click", async () => {
    let u = $("#url").value.trim();
    if (!u) return;
    if (!/^https?:[/][/]/i.test(u)) u = "https://" + u;
    const r = await act("open", { url: u });
    if (r.status === 200) $("#url").value = "";
  });
  $("#url").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#openUrl").click(); });

  /* ---------- status ---------- */
  function card(label, value, pct) {
    const d = document.createElement("div");
    d.className = "card";
    const l = document.createElement("div"); l.className = "lbl"; l.textContent = label;
    const v = document.createElement("div"); v.className = "val"; v.textContent = value;
    d.appendChild(l); d.appendChild(v);
    if (typeof pct === "number") {
      const bar = document.createElement("div"); bar.className = "bar";
      const fill = document.createElement("i");
      fill.style.width = Math.max(0, Math.min(100, pct)) + "%";
      bar.appendChild(fill); d.appendChild(bar);
    }
    return d;
  }

  function fmtUptime(s) {
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    return d ? d + "d " + h + "h" : h ? h + "h " + m + "m" : m + "m";
  }

  async function loadInfo() {
    const r = await act("info", {}, true);
    if (r.status !== 200) { clearInterval(infoTimer); infoTimer = null; return; }
    const d = r.data;
    const g = $("#infoGrid");
    g.textContent = "";
    g.appendChild(card("CPU", d.cpu == null ? "n/a" : d.cpu + "%", d.cpu));
    g.appendChild(card("Memory", d.ram_used_gb == null ? "n/a" : d.ram_used_gb + " / " + d.ram_total_gb + " GB", d.ram_percent));
    g.appendChild(card("Battery", d.battery == null ? "No battery" : d.battery + "%" + (d.plugged ? " (charging)" : ""), d.battery));
    const used = d.disk_total_gb ? Math.round(100 * (d.disk_total_gb - d.disk_free_gb) / d.disk_total_gb) : null;
    g.appendChild(card("Disk " + (d.disk_name || ""), d.disk_free_gb == null ? "n/a" : d.disk_free_gb + " GB free", used));
    g.appendChild(card("Uptime", d.uptime_s == null ? "n/a" : fmtUptime(d.uptime_s)));
    g.appendChild(card("PC clock", d.time || "n/a"));
  }

  /* ---------- start ---------- */
  // Opened from the QR code: the PIN rides in the #fragment (never sent over the network)
  const scanned = location.hash.match(/pin=([^&]*)/);
  if (scanned) {
    history.replaceState(null, "", location.pathname);
    tryUnlock(decodeURIComponent(scanned[1]));
  } else if (pin) {
    tryUnlock(pin);
  } else {
    show("login");
  }
</script>
</body>
</html>
"""

QR_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connect your iPhone</title>
<style>
  body { margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
         background: #0f1115; color: #f2f4f8; font-family: "Segoe UI", system-ui, sans-serif; text-align: center; }
  .card { padding: 32px; }
  h1 { margin: 0 0 12px; font-size: 32px; }
  p { color: #9aa3b5; font-size: 18px; margin: 6px 0; }
  .qr { background: #fff; padding: 18px; border-radius: 20px; display: inline-block; margin: 24px 0 14px; }
  .qr svg { width: min(70vw, 420px); height: auto; display: block; }
  .url { font-family: Consolas, monospace; color: #c9d1e0; }
  .note { font-size: 15px; margin-top: 18px; }
</style>
</head>
<body>
<div class="card">
  <h1>Connect your iPhone</h1>
  <p>1. Open the <b>Camera</b> app on your iPhone (same Wi-Fi as this PC)</p>
  <p>2. Point it at the code, then tap the link that pops up</p>
  <div class="qr">__QR__</div>
  <p class="url">__URL__</p>
  <p class="note">__NOTE__</p>
</div>
</body>
</html>
"""

fail_count = 0
locked_until = 0.0
segno = None  # QR library, loaded in main()


def ensure_package(import_name, pip_name, why):
    """Import a package, installing it with pip the first time if it is missing."""
    import importlib
    try:
        return importlib.import_module(import_name)
    except ImportError:
        pass
    print(f"Installing {pip_name} ({why}) - one time only...")
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", pip_name], check=True)
        importlib.invalidate_caches()
        return importlib.import_module(import_name)
    except Exception:
        return None


def scan_url():
    """The address the QR code points to (optionally with the PIN in the #fragment)."""
    url = f"http://{local_ip()}:{PORT}/"
    if QR_INCLUDES_PIN:
        url += "#pin=" + quote(PIN, safe="")
    return url


def qr_page():
    shown = f"http://{local_ip()}:{PORT}"
    if segno is None:
        qr_html = "<p>QR library missing. Run: pip install segno</p>"
    else:
        qr_html = segno.make(scan_url(), error="m").svg_inline(
            scale=1, border=1, dark="#000000", light="#ffffff")
        # Swap fixed width/height for a viewBox so the code scales to any screen size
        qr_html = re.sub(r'width="(\d+)" height="(\d+)"', r'viewBox="0 0 \1 \2"', qr_html, count=1)
    note = ("This page only opens on this PC. Scanning the code logs you in automatically."
            if QR_INCLUDES_PIN else "You will be asked for your PIN after scanning.")
    return QR_PAGE.replace("__QR__", qr_html).replace("__URL__", shown).replace("__NOTE__", note)


def local_ip():
    """Best-effort guess of this PC's LAN IP address."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class Handler(BaseHTTPRequestHandler):
    server_version = "PCController"
    protocol_version = "HTTP/1.1"   # keep connections open: smoother trackpad
    timeout = 30                    # drop idle connections

    def log_message(self, fmt, *args):
        if getattr(self, "path", "") in QUIET_PATHS:
            return
        sys.stdout.write("[%s] %s - %s\n" % (time.strftime("%H:%M:%S"), self.client_address[0], fmt % args))

    def _send(self, code, body, ctype="application/json"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        if ctype.startswith("text/") or ctype == "application/json":
            ctype += "; charset=utf-8"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, **payload):
        self._send(code, json.dumps(payload))

    def _read_body(self):
        """Always consume the request body so keep-alive connections stay in sync."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > 65536:
            self.close_connection = True
            return None
        return self.rfile.read(length) if length > 0 else b""

    def do_GET(self):
        route = self.path.split("?")[0]
        if route in ("/", "/index.html"):
            self._send(200, PAGE.replace("__DELAY__", str(DELAY)), "text/html")
        elif route == "/qr":
            # The QR can contain the PIN, so only show it on the PC itself.
            if self.client_address[0] not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
                self._send(403, "Open this page on the PC itself, not on another device.", "text/plain")
            else:
                self._send(200, qr_page(), "text/html")
        else:
            self._json(404, message="Not found")

    def do_POST(self):
        global fail_count, locked_until

        raw = self._read_body()
        if raw is None:
            return self._json(413, message="Request too large")
        if not self.path.startswith("/api/"):
            return self._json(404, message="Not found")
        action = self.path[len("/api/"):]

        now = time.time()
        if now < locked_until:
            wait = int(locked_until - now) + 1
            return self._json(429, message=f"Too many wrong PINs. Try again in {wait}s.")

        supplied = unquote(self.headers.get("X-PIN", ""))
        if not hmac.compare_digest(supplied.encode(), PIN.encode()):
            fail_count += 1
            if fail_count >= MAX_FAILS:
                locked_until = now + LOCKOUT_SECONDS
                fail_count = 0
            return self._json(401, message="Wrong PIN")
        fail_count = 0

        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(body, dict):
                body = {}
        except ValueError:
            return self._json(400, message="Bad request")

        if action == "check":
            return self._json(200, ok=True, host=socket.gethostname(),
                              features=FEATURES, apps=list(APPS) if FEATURES.get("apps") else [])

        if action == "screen":
            if not FEATURES.get("screen"):
                return self._json(403, message="Screen view is turned off on the PC.")
            try:
                return self._send(200, screen_jpeg(), "image/jpeg")
            except ActionError as e:
                return self._json(e.code, message=str(e))
            except Exception as e:
                return self._json(500, message=f"Couldn't capture the screen: {e}")

        entry = HANDLERS.get(action)
        if not entry:
            return self._json(404, message="Unknown action")
        feature, fn = entry
        if feature and not FEATURES.get(feature):
            return self._json(403, message="This feature is turned off on the PC.")

        try:
            result = fn(body)
        except ActionError as e:
            return self._json(e.code, message=str(e))
        except (ValueError, TypeError, KeyError):
            return self._json(400, message="Bad request")
        except Exception as e:
            return self._json(500, message=f"Failed: {e}")

        if isinstance(result, dict):
            return self._json(200, **result)
        return self._json(200, message=result or "")


def main():
    global segno
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    if PIN == "1234":
        print("!! You are still using the default PIN (1234). Edit PIN at the top of this file.\n")

    if IS_WINDOWS:
        try:
            ctypes.windll.user32.SetProcessDPIAware()  # real pixel sizes for screen + mouse
        except Exception:
            pass

    segno = ensure_package("segno", "segno", "QR code")
    if IS_WINDOWS and FEATURES.get("screen"):
        if ensure_package("PIL", "pillow", "screen view") is None:
            print("Could not install Pillow, so the Screen tab won't work. Try: pip install pillow\n")

    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    ip = local_ip()
    print("PC Controller is running.\n")
    print(f"Address: http://{ip}:{PORT}")
    print("Features on: " + ", ".join(k for k, v in FEATURES.items() if v))
    if segno is not None:
        print("\nScan this with your iPhone camera (a bigger code opens in your browser):\n")
        try:
            segno.make(scan_url(), error="m").terminal(compact=True, border=2)
        except Exception:
            pass
        if OPEN_BROWSER:
            threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}/qr")).start()
    else:
        print("\n(QR code unavailable - type the address above into Safari on your iPhone.)")
    print("\nTip: in Safari tap Share > Add to Home Screen for a one-tap app icon.")
    print("Press Ctrl+C here to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
