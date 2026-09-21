import ctypes
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from favorite_streamers import load_favorites, resolve_profile_url, save_favorites, upsert_favorite
from profile_cards import (
    account_id_for_profile,
    download_avatar,
    load_card_state,
    next_profile_to_show,
    normalize_display_name,
    profile_label,
    profile_number,
    save_card_state,
    start_profile_bridge,
)


CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

PROFILES = {
    "機器人 A": "Default",
}

# Keep the account cards from previous sessions without treating empty Chrome
# profiles as logged-in accounts.
_chrome_local_data = os.environ.get("LOCALAPPDATA")
if _chrome_local_data:
    _chrome_profile_root = Path(_chrome_local_data) / "Google" / "Chrome" / "User Data"
    for _profile_number in range(1, 7):
        if not (_chrome_profile_root / f"Profile {_profile_number}").is_dir():
            break
        PROFILES[f"機器人 {chr(ord('A') + _profile_number)}"] = f"Profile {_profile_number}"

PROFILE_AVATARS = {
    "Default": ("A", 0xE8A14A),
    "Profile 1": ("B", 0x1ED760),
    "Profile 2": ("C", 0xB65A7A),
}

PROFILE_ACCOUNTS = {
    name: f"fb_account_{index:03d}"
    for index, name in enumerate(PROFILES, start=1)
}

LOGIN_DISPLAY_NAME = os.environ.get("FB_AUTO_LOGIN_ACCOUNT", "").strip()
if LOGIN_DISPLAY_NAME:
    PROFILE_AVATARS["Default"] = (LOGIN_DISPLAY_NAME[:1].upper(), 0x4AA3DF)

PROFILE_DISPLAY_NAMES = {
    "Default": LOGIN_DISPLAY_NAME or "Facebook 使用者",
    "Profile 1": "Facebook 使用者",
    "Profile 2": "Facebook 使用者",
}

ACCOUNT_COLORS = [
    0xE8A14A,
    0x1ED760,
    0xB65A7A,
    0x4A90E2,
    0xD9822B,
    0x8E6AD8,
    0x2FA7A0,
    0xD94F70,
]


def account_label(index):
    return f"機器人 {chr(ord('A') + index)}"


def chrome_profile_dir(index):
    return "Default" if index == 0 else f"Profile {index}"


def get_base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BASE_DIR = get_base_dir()
APP_START_TIME = time.time()
PROJECT_DIR = BASE_DIR.parent
SHARED_TEXT_FILE = BASE_DIR / "Text.jsonl"
SESSION_TEXT_DIR = BASE_DIR / "sessions"
SESSION_META_DIR = SESSION_TEXT_DIR / "metadata"
REPLY_DIR = BASE_DIR / "replies"
STT_EXE = BASE_DIR / "stt_worker.exe"
LLM_EXE = BASE_DIR / "llm_server.exe"
DB_API_EXE = BASE_DIR / "reply_db_api.exe"
STT_SCRIPT = PROJECT_DIR / "stt" / "WASAPI_test.py"
LLM_SCRIPT = PROJECT_DIR / "test_LLM" / "test_llm_8" / "live_stream_llm.py"
DB_API_SCRIPT = BASE_DIR / "user_input.py"
if not LLM_SCRIPT.exists():
    LLM_SCRIPT = BASE_DIR / "live_stream_llm.py"
if not DB_API_SCRIPT.exists():
    DB_API_SCRIPT = PROJECT_DIR / "test_LLM" / "test_llm_8" / "user_input.py"
DB_API_BASE = "http://127.0.0.1:5001"

PROFILE_STATE_DIR = Path(os.environ.get("LOCALAPPDATA") or BASE_DIR) / "FB_Live_Auto_Comment"
ROBOT_CARD_STATE_FILE = PROFILE_STATE_DIR / "robot_cards.json"
FAVORITES_FILE = PROFILE_STATE_DIR / "favorite_streamers.json"
PROFILE_AVATAR_DIR = PROFILE_STATE_DIR / "avatars"
AUTO_MODE_MASCOT = Path(getattr(sys, "_MEIPASS", BASE_DIR)) / "assets" / "auto_mode_mascot.png"
favorite_streamers = load_favorites(FAVORITES_FILE)
favorite_lock = threading.RLock()
favorite_probe_pending = set()
favorite_probe_cache = {}
url_entered_at = {}
_saved_cards = load_card_state(ROBOT_CARD_STATE_FILE, list(PROFILES.values()))
PROFILES = {profile_label(directory): directory for directory in _saved_cards["profiles"]}
PROFILE_ACCOUNTS = {
    name: account_id_for_profile(directory) for name, directory in PROFILES.items()
}
PROFILE_DISPLAY_NAMES.update(_saved_cards["names"])
next_profile_number = _saved_cards["next_profile_number"]
PROFILE_AVATAR_URLS = {}
PROFILE_IMAGE_CACHE = {}
AUTO_IMAGE_HANDLE = None
profile_bridge = None
profile_state_lock = threading.RLock()
for _label, _directory in PROFILES.items():
    _number = profile_number(_directory)
    PROFILE_AVATARS.setdefault(
        _directory, (chr(ord("A") + _number), ACCOUNT_COLORS[_number % len(ACCOUNT_COLORS)])
    )
    _saved_name = normalize_display_name(PROFILE_DISPLAY_NAMES.get(_directory))
    if _saved_name:
        _, _avatar_color = PROFILE_AVATARS[_directory]
        PROFILE_AVATARS[_directory] = (_saved_name[:1], _avatar_color)

processes = []
llm_processes = {}
db_api_process = None
service_start_lock = threading.RLock()


def llm_port_for_profile(profile_name, row_index=0):
    if not 0 <= row_index < MAX_URL_FIELDS:
        raise ValueError(f"Invalid live row: {row_index}")
    port = 5002 + profile_number(PROFILES[profile_name]) * MAX_URL_FIELDS + row_index
    if port > 65535:
        raise ValueError(f"No LLM port available for {profile_name}")
    return port


def llm_base_for_profile(profile_name, row_index=0):
    return f"http://127.0.0.1:{llm_port_for_profile(profile_name, row_index)}"


def current_llm_base():
    active = url_active_profiles[active_reply_stream_index]
    profile_name = next((name for name in PROFILES if name in active), None)
    if profile_name is None:
        profile_name = next((name for name in PROFILES if name in selected_profiles), selected_profile)
    return llm_base_for_profile(profile_name, active_reply_stream_index)


def run_process(command, cwd, env=None, new_console=True):
    flags = subprocess.CREATE_NEW_CONSOLE if new_console and os.name == "nt" else 0
    proc = subprocess.Popen(command, cwd=str(cwd), env=env, creationflags=flags)
    processes.append(proc)
    return proc


def run_visible_console(command, cwd, env=None, title="FB LLM Server"):
    if os.name == "nt":
        shell_line = f"title {title} & chcp 65001 >nul & {subprocess.list2cmdline(command)}"
        cmd = os.environ.get("COMSPEC", "cmd.exe")
        return run_process([cmd, "/k", shell_line], cwd, env, new_console=True)
    return run_process(command, cwd, env, new_console=True)


def run_background_service(command, cwd, env=None):
    """Start a local API without opening a console window."""
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    proc = subprocess.Popen(
        command,
        cwd=str(cwd),
        env=env,
        creationflags=flags,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    processes.append(proc)
    return proc


def process_running(proc):
    return proc is not None and proc.poll() is None


def wait_for_service(url, timeout_sec=30):
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            with urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def wait_for_llm(profile_name, row_index=0, timeout_sec=30):
    return wait_for_service(f"{llm_base_for_profile(profile_name, row_index)}/health", timeout_sec=timeout_sec)


def wait_for_db_api(timeout_sec=30):
    return wait_for_service(f"{DB_API_BASE}/health", timeout_sec=timeout_sec)


def request_json(url, payload=None, method="GET", timeout=10):
    body = None
    headers = {}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"

    request = Request(url, data=body, headers=headers, method=method)
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def start_llm(profile_name, row_index):
    env = os.environ.copy()
    env["LLM_INPUT_DIR"] = str(SESSION_TEXT_DIR)
    env["LLM_REPLY_DIR"] = str(REPLY_DIR)
    env["LLM_INPUT_PATTERN"] = "*.jsonl"
    env["GENERATION_COOLDOWN_SECONDS"] = "0"
    env["LLM_PORT"] = str(llm_port_for_profile(profile_name, row_index))
    env["LLM_ACCOUNT_ID"] = PROFILE_ACCOUNTS[profile_name]
    title = f"LLM {profile_name} - Live {row_index + 1}"

    if LLM_EXE.exists():
        return run_visible_console([str(LLM_EXE)], BASE_DIR, env, title=title)

    if not LLM_SCRIPT.exists():
        raise FileNotFoundError(f"找不到 LLM 程式：{LLM_SCRIPT}")

    return run_visible_console(
        [sys.executable, str(LLM_SCRIPT)],
        LLM_SCRIPT.parent,
        env,
        title=title,
    )


def start_db_api():
    env = os.environ.copy()

    if DB_API_EXE.exists():
        return run_background_service([str(DB_API_EXE)], BASE_DIR, env)

    if not DB_API_SCRIPT.exists():
        raise FileNotFoundError(f"找不到語句資料庫 API 程式：{DB_API_SCRIPT}")

    return run_background_service([sys.executable, str(DB_API_SCRIPT)], DB_API_SCRIPT.parent, env)


def ensure_llm_ready(show_existing_status=False, targets=None):
    global db_api_process
    if targets is None:
        name = next((name for name in PROFILES if name in selected_profiles), selected_profile)
        targets = [(name, active_reply_stream_index)] if name else []
    targets = list(dict.fromkeys(targets))
    with service_start_lock:
        if not wait_for_db_api(timeout_sec=2):
            if process_running(db_api_process):
                if not wait_for_db_api(timeout_sec=15):
                    raise RuntimeError("留言資料 API 未就緒")
            else:
                set_status("正在啟動留言資料 API...")
                db_api_process = start_db_api()
                if not wait_for_db_api(timeout_sec=30):
                    raise RuntimeError("留言資料 API 30 秒內未就緒，請檢查 MongoDB / Ollama")

        for profile_name, row_index in targets:
            key = (profile_name, row_index)
            proc = llm_processes.get(key)
            if process_running(proc) and wait_for_llm(profile_name, row_index, timeout_sec=2):
                continue
            if process_running(proc):
                terminate_process_tree(proc)
                llm_processes.pop(key, None)
            elif wait_for_llm(profile_name, row_index, timeout_sec=1):
                port = llm_port_for_profile(profile_name, row_index)
                raise RuntimeError(f"連接埠 {port} 已由其他 LLM 使用，請先關閉舊終端機")
            set_status(f"正在啟動 {profile_name} 直播 {row_index + 1} 的 LLM...")
            proc = start_llm(profile_name, row_index)
            llm_processes[key] = proc
            if not wait_for_llm(profile_name, row_index, timeout_sec=45):
                terminate_process_tree(proc)
                llm_processes.pop(key, None)
                raise RuntimeError(f"{profile_name} 直播 {row_index + 1} 的 LLM 45 秒內未就緒")
            request_json(f"{llm_base_for_profile(profile_name, row_index)}/reload_replies", method="POST")
        set_status(f"已啟動 {len(targets)} 個 LLM 終端機")


def start_stt(url, profile_dir, output_file, stream_id):
    env = os.environ.copy()
    env["STT_STREAM_URL"] = url
    env["STT_OUTPUT_JSONL"] = str(output_file)
    env["STT_CHROME_PROFILE"] = profile_dir

    if STT_EXE.exists():
        return run_process([
            str(STT_EXE),
            "--url", url,
            "--output", str(output_file),
            "--stream-id", stream_id,
            "--chrome-profile", profile_dir,
        ], BASE_DIR, env)

    if not STT_SCRIPT.exists():
        raise FileNotFoundError(f"找不到 STT 程式：{STT_SCRIPT}")

    return run_process([
        sys.executable,
        str(STT_SCRIPT),
        "--url", url,
        "--output", str(output_file),
        "--stream-id", stream_id,
        "--chrome-profile", profile_dir,
    ], STT_SCRIPT.parent, env)


def open_chrome(url, profile_dir):
    if not Path(CHROME_PATH).exists():
        raise FileNotFoundError(f"找不到 Chrome：{CHROME_PATH}")
    windows_before = set(get_chrome_windows())
    proc = subprocess.Popen([
        CHROME_PATH,
        f"--profile-directory={profile_dir}",
        "--new-window",
        url,
    ])
    new_windows = []
    deadline = time.time() + 8
    while time.time() < deadline:
        new_windows = [
            hwnd for hwnd in get_chrome_windows()
            if hwnd not in windows_before
        ]
        if new_windows:
            break
        time.sleep(0.25)
    return {"process": proc, "windows": new_windows}


def with_extension_config(url, account, jsonl_file, llm_port):
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    fragment = dict(parse_qsl(parts.fragment, keep_blank_values=True))
    query["fb_auto_account"] = account
    query["fb_auto_jsonl"] = str(jsonl_file)
    query["fb_auto_port"] = str(llm_port)
    fragment["fb_auto_account"] = account
    fragment["fb_auto_jsonl"] = str(jsonl_file)
    fragment["fb_auto_port"] = str(llm_port)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), urlencode(fragment)))


def build_stream_file(profile_name, url, index, channel_name=""):
    SESSION_TEXT_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_META_DIR.mkdir(parents=True, exist_ok=True)
    account = PROFILE_ACCOUNTS[profile_name]
    stream_key = f"{account}_live_{index:02d}"
    text_file = SESSION_TEXT_DIR / f"{stream_key}.jsonl"
    metadata = {
        "stream_id": stream_key,
        "channel_name": channel_name.strip(),
        "facebook_url": url,
        "account_id": account,
        "login_account": os.environ.get("FB_AUTO_LOGIN_ACCOUNT", ""),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    (SESSION_META_DIR / f"{stream_key}.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return text_file, stream_key


def create_llm_session(profile_name, text_file, row_index):
    request_json(
        f"{llm_base_for_profile(profile_name, row_index)}/process?from_start=true",
        {
            "file_path": str(text_file),
            "account_ids": [PROFILE_ACCOUNTS[profile_name]],
        },
        method="POST",
    )


def start_pipeline(tasks, cancelled=None):
    if not tasks:
        raise ValueError("請至少選擇一個帳號與直播網址")
    for task in tasks:
        if not task["url"].startswith(("http://", "https://")):
            raise ValueError("直播網址必須以 http 或 https 開頭")

    grouped_tasks = []
    for task in tasks:
        for group in grouped_tasks:
            same_row = task.get("stream_index") is None or group["row_index"] == task["stream_index"] - 1
            if group["url"] == task["url"] and same_row:
                group["tasks"].append(task)
                break
        else:
            row_index = task.get("stream_index", len(grouped_tasks) + 1) - 1
            grouped_tasks.append({"url": task["url"], "row_index": row_index, "tasks": [task]})

    targets = list(dict.fromkeys(
        (task["profile_name"], group["row_index"])
        for group in grouped_tasks for task in group["tasks"]
    ))
    existing_targets = {key for key, proc in llm_processes.items() if process_running(proc)}
    started_stream_processes = []
    started_browser_processes = []
    try:
        if cancelled and cancelled():
            raise InterruptedError("直播啟動已取消")
        SESSION_TEXT_DIR.mkdir(parents=True, exist_ok=True)
        REPLY_DIR.mkdir(parents=True, exist_ok=True)
        set_latest_reply("尚未產生留言。", "")
        ensure_llm_ready(show_existing_status=True, targets=targets)
        if cancelled and cancelled():
            raise InterruptedError("直播啟動已取消")
        refresh_reply_list()

        for group in grouped_tasks:
            if cancelled and cancelled():
                raise InterruptedError("直播啟動已取消")
            first_profile = group["tasks"][0]["profile_name"]
            channel_name = group["tasks"][0].get("channel_name", "")
            row_index = group["row_index"]
            text_file, stream_id = build_stream_file(
                first_profile, group["url"], row_index + 1, channel_name
            )
            text_file.write_text("", encoding="utf-8")
            started_stream_processes.append(
                start_stt(group["url"], PROFILES[first_profile], text_file, stream_id)
            )

            for task in group["tasks"]:
                if cancelled and cancelled():
                    raise InterruptedError("直播啟動已取消")
                profile_name = task["profile_name"]
                create_llm_session(profile_name, text_file, row_index)
                started_browser_processes.append(
                    open_chrome(
                        with_extension_config(
                            group["url"], PROFILE_ACCOUNTS[profile_name], text_file,
                            llm_port_for_profile(profile_name, row_index),
                        ),
                        PROFILES[profile_name],
                    )
                )
        if cancelled and cancelled():
            raise InterruptedError("直播啟動已取消")
        return started_stream_processes, started_browser_processes, targets
    except Exception:
        close_browser_resources(started_browser_processes)
        terminate_processes(started_stream_processes)
        stop_llm_targets(key for key in targets if key not in existing_targets)
        raise


user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

# Ask Windows for real display pixels so it does not bitmap-scale the whole
# window on high-DPI displays. Fall back to system DPI awareness on older OSes.
try:
    set_dpi_awareness = user32.SetProcessDpiAwarenessContext
    set_dpi_awareness.argtypes = [ctypes.c_void_p]
    set_dpi_awareness.restype = wintypes.BOOL
    if not set_dpi_awareness(ctypes.c_void_p(-4)):
        user32.SetProcessDPIAware()
except AttributeError:
    user32.SetProcessDPIAware()

LRESULT = ctypes.c_ssize_t
HCURSOR = wintypes.HANDLE
HICON = wintypes.HANDLE
HBRUSH = wintypes.HANDLE
HINSTANCE = wintypes.HANDLE
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.CallWindowProcW.restype = LRESULT
user32.CreateWindowExW.restype = wintypes.HWND
user32.SendMessageW.argtypes = [
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    ctypes.c_ssize_t,
]
user32.SendMessageW.restype = LRESULT
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetClientRect.restype = wintypes.BOOL
user32.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
user32.ScreenToClient.restype = wintypes.BOOL
gdi32.SetBkColor.argtypes = [wintypes.HDC, wintypes.DWORD]
gdi32.SetTextColor.argtypes = [wintypes.HDC, wintypes.DWORD]
gdi32.CreateSolidBrush.restype = HBRUSH
gdi32.CreateFontW.restype = wintypes.HANDLE
gdi32.CreatePen.restype = wintypes.HANDLE
gdi32.GetStockObject.restype = wintypes.HANDLE
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
gdi32.SelectObject.restype = wintypes.HANDLE
gdi32.DeleteObject.argtypes = [wintypes.HANDLE]

gdiplus = ctypes.windll.gdiplus


class GdiplusStartupInput(ctypes.Structure):
    _fields_ = [
        ("GdiplusVersion", ctypes.c_uint32),
        ("DebugEventCallback", ctypes.c_void_p),
        ("SuppressBackgroundThread", wintypes.BOOL),
        ("SuppressExternalCodecs", wintypes.BOOL),
    ]


gdiplus.GdiplusStartup.argtypes = [ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(GdiplusStartupInput), ctypes.c_void_p]
gdiplus.GdipLoadImageFromFile.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
gdiplus.GdipCreateFromHDC.argtypes = [wintypes.HDC, ctypes.POINTER(ctypes.c_void_p)]
gdiplus.GdipCreatePath.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
gdiplus.GdipAddPathEllipseI.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
gdiplus.GdipSetClipPath.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
gdiplus.GdipDrawImageRectI.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
gdiplus.GdipSetInterpolationMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
gdiplus.GdipSetSmoothingMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
gdiplus.GdipDisposeImage.argtypes = [ctypes.c_void_p]
gdiplus.GdipDeletePath.argtypes = [ctypes.c_void_p]
gdiplus.GdipDeleteGraphics.argtypes = [ctypes.c_void_p]
gdiplus.GdiplusShutdown.argtypes = [ctypes.c_size_t]
GDIPLUS_TOKEN = ctypes.c_size_t()
GDIPLUS_READY = gdiplus.GdiplusStartup(
    ctypes.byref(GDIPLUS_TOKEN), ctypes.byref(GdiplusStartupInput(1, None, False, False)), None
) == 0

CS_HREDRAW = 0x0002
CS_VREDRAW = 0x0001
CW_USEDEFAULT = 0x80000000
WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_VISIBLE = 0x10000000
WS_CHILD = 0x40000000
WS_TABSTOP = 0x00010000
WS_BORDER = 0x00800000
WS_VSCROLL = 0x00200000
ES_AUTOHSCROLL = 0x0080
ES_READONLY = 0x0800
LBS_NOTIFY = 0x0001
SW_HIDE = 0

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_GETMINMAXINFO = 0x0024
WM_SIZE = 0x0005
WM_PAINT = 0x000F
WM_CTLCOLOREDIT = 0x0133
WM_CTLCOLORSTATIC = 0x0138
WM_COMMAND = 0x0111
WM_SETFONT = 0x0030
EM_SETCUEBANNER = 0x1501
EM_SETREADONLY = 0x00CF
EN_SETFOCUS = 0x0100
EN_KILLFOCUS = 0x0200
WM_LBUTTONUP = 0x0202
WM_MOUSEWHEEL = 0x020A
SW_SHOW = 5
DT_LEFT = 0x00000000
DT_CENTER = 0x00000001
DT_RIGHT = 0x00000002
DT_VCENTER = 0x00000004
DT_WORDBREAK = 0x00000010
DT_SINGLELINE = 0x00000020
DT_END_ELLIPSIS = 0x00008000
TRANSPARENT = 1

LB_ADDSTRING = 0x0180
LB_RESETCONTENT = 0x0184
LB_GETCURSEL = 0x0188
LB_ERR = -1

ID_URL = 2000
ID_PHRASE = 2001
ID_REPLY_LIST = 2002
ID_CHANNEL = 2010
ID_URL_2 = 2003
ID_URL_3 = 2004
ID_PROFILE_NAME_EDIT = 2020
ID_PROFILE_NAME_SAVE = 2021
MAX_URL_FIELDS = 5
MAX_ACCOUNTS = 8
ACCOUNT_ROW_SPACING = 140

DESIGN_WIDTH = 1280
DESIGN_HEIGHT = 720
WIDTH = 1280
HEIGHT = 720
CARD_RECT = (16, 82, 1264, 704)
START_RECT = (432, 366, 490, 414)
SAVE_PHRASE_RECT = (1108, 136, 1248, 174)
REPLY_PANEL_RECT = (596, 198, 1264, 704)
REPLY_LIST_RECT = (612, 270, 1248, 630)
REPLY_HEADING_RECT = (612, 215, 778, 262)
REPLY_DEFAULT_TAB_RECT = (838, 220, 900, 250)
REPLY_USER_TAB_RECT = (905, 220, 967, 250)
REPLY_CROWD_TAB_RECT = (972, 220, 1046, 250)
REPLY_MANUAL_TAB_RECT = (1064, 225, 1152, 255)
REPLY_AUTO_TAB_RECT = (1158, 225, 1248, 255)
REFRESH_RECT = (612, 650, 922, 688)
DELETE_RECT = (934, 650, 1248, 688)
STOP_RECT = (498, 366, 556, 414)
PROFILE_RECTS = {
    "機器人 A": (32, 145, 156, 270),
    "機器人 B": (166, 145, 290, 270),
    "機器人 C": (300, 145, 424, 270),
    "機器人 D": (434, 145, 558, 270),
}


class WNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", HINSTANCE),
        ("hIcon", HICON),
        ("hCursor", HCURSOR),
        ("hbrBackground", HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [
        ("hdc", wintypes.HDC),
        ("fErase", wintypes.BOOL),
        ("rcPaint", wintypes.RECT),
        ("fRestore", wintypes.BOOL),
        ("fIncUpdate", wintypes.BOOL),
        ("rgbReserved", ctypes.c_byte * 32),
    ]


def rgb(r, g, b):
    return r | (g << 8) | (b << 16)


def rect(left, top, right, bottom):
    return wintypes.RECT(left, top, right, bottom)


def make_font(size, weight=400):
    return gdi32.CreateFontW(
        -size, 0, 0, 0, weight, 0, 0, 0, 0, 0, 0, 5, 0, "Microsoft JhengHei UI"
    )


FONT_TITLE = make_font(25, 700)
FONT_HEADING = make_font(18, 700)
FONT_TAB = make_font(15, 700)
FONT_BODY = make_font(16, 400)
FONT_BODY_BOLD = make_font(16, 700)
FONT_SMALL = make_font(13, 400)
FONT_TINY = make_font(12, 400)
FONT_AVATAR = make_font(21, 700)
FONT_HEADER = make_font(12, 700)
FONT_HEADER_SMALL = make_font(10, 700)

FONT_SPECS = {
    int(FONT_TITLE): (25, 700),
    int(FONT_HEADING): (18, 700),
    int(FONT_TAB): (15, 700),
    int(FONT_BODY): (16, 400),
    int(FONT_BODY_BOLD): (16, 700),
    int(FONT_SMALL): (13, 400),
    int(FONT_TINY): (12, 400),
    int(FONT_AVATAR): (21, 700),
    int(FONT_HEADER): (12, 700),
    int(FONT_HEADER_SMALL): (10, 700),
}
SCALED_FONT_CACHE = {}

COLOR_BG = rgb(14, 19, 27)
COLOR_BG_SHADOW = rgb(14, 19, 27)
COLOR_CARD = rgb(23, 30, 40)
COLOR_CARD_2 = rgb(29, 38, 50)
COLOR_PANEL = rgb(25, 33, 44)
COLOR_TEXT = rgb(250, 250, 250)
COLOR_MUTED = rgb(155, 168, 186)
COLOR_GREEN = rgb(20, 194, 115)
COLOR_GRAY_BUTTON = rgb(49, 62, 79)
COLOR_DANGER = rgb(207, 60, 79)
EDIT_BRUSH = gdi32.CreateSolidBrush(COLOR_CARD_2)

hwnd_main = None
hwnd_url = None
hwnd_url_2 = None
hwnd_url_3 = None
url_controls = []
hwnd_phrase = None
hwnd_channel = None
hwnd_reply_list = None
hwnd_profile_name_edit = None
hwnd_profile_name_save = None
editing_profile_name = ""
selected_profile = next(iter(PROFILES), "")
selected_profiles = {selected_profile} if selected_profile else set()
run_mode = "one_to_many"
url_field_count = 1
url_states = ["idle"] * MAX_URL_FIELDS
starting_stream_indexes = set()
url_start_tokens = [0] * MAX_URL_FIELDS
url_processes = [[] for _ in range(MAX_URL_FIELDS)]
url_browser_processes = [[] for _ in range(MAX_URL_FIELDS)]
url_llm_keys = [set() for _ in range(MAX_URL_FIELDS)]
url_originals = [""] * MAX_URL_FIELDS
url_stream_ids = [""] * MAX_URL_FIELDS
url_active_profiles = [set() for _ in range(MAX_URL_FIELDS)]
is_busy = False
pipeline_state = "idle"
status_text = "就緒，請輸入單一帳號與單一直播網址。"
latest_reply_text = "尚未產生留言。"
latest_input_text = ""
reply_items = []
selected_reply_index = -1
editing_reply_id = None
reply_scroll_offset = 0
reply_filter = "default"
phrase_category = "default"
stream_reply_mode = "manual"
pending_phrase_category = phrase_category
pending_reply_mode = stream_reply_mode
mode_selection_dirty = False
reply_mode_generation = 0
reply_mode_service_lock = threading.Lock()
reply_selection_dirty = False
active_reply_stream_index = 0
last_reply_mtimes = {}
last_stream_mtimes = {}
stop_monitor = False
client_width = WIDTH
client_height = HEIGHT


def set_control_font(hwnd, font):
    user32.SendMessageW(hwnd, WM_SETFONT, scaled_font(font), True)


def scale_x():
    return max(0.1, client_width / DESIGN_WIDTH)


def scale_y():
    return max(0.1, client_height / required_content_height())


def visual_scale():
    return min(scale_x(), scale_y())


def scaled_font(font):
    spec = FONT_SPECS.get(int(font))
    if not spec:
        return font
    scale_key = max(50, min(250, int(round(visual_scale() * 100))))
    cache_key = (int(font), scale_key)
    if cache_key not in SCALED_FONT_CACHE:
        size, weight = spec
        SCALED_FONT_CACHE[cache_key] = make_font(
            max(8, int(round(size * scale_key / 100))),
            weight,
        )
    return SCALED_FONT_CACHE[cache_key]


def set_input_placeholder(hwnd, text):
    """Display native grey cue text without making it the edit value."""
    cue = ctypes.c_wchar_p(text)
    user32.SendMessageW(
        hwnd,
        EM_SETCUEBANNER,
        False,
        ctypes.cast(cue, ctypes.c_void_p).value,
    )


def update_client_size(hwnd=None):
    global client_width, client_height
    target = hwnd or hwnd_main
    if not target:
        return
    rc = wintypes.RECT()
    if user32.GetClientRect(target, ctypes.byref(rc)):
        client_width = max(1, rc.right - rc.left)
        client_height = max(1, rc.bottom - rc.top)


def required_content_height():
    return max(DESIGN_HEIGHT + account_field_extra_y(), favorite_card_rect()[3] + 28)


def content_offset_x():
    return 0


def content_offset_y():
    return 0


def shifted_area(area):
    sx = scale_x()
    sy = scale_y()
    return (
        int(round(area[0] * sx)),
        int(round(area[1] * sy)),
        int(round(area[2] * sx)),
        int(round(area[3] * sy)),
    )


def shifted_point(x, y):
    return int(round(x * scale_x())), int(round(y * scale_y()))


def move_control(hwnd, x, y, width, height):
    if hwnd:
        px, py = shifted_point(x, y)
        user32.MoveWindow(
            hwnd,
            px,
            py,
            max(1, int(round(width * scale_x()))),
            max(1, int(round(height * scale_y()))),
            True,
        )


def account_field_extra_y():
    rows = account_row_count()
    return max(0, rows - 1) * ACCOUNT_ROW_SPACING


def below_accounts_area(area):
    extra = account_field_extra_y()
    return (area[0], area[1] + extra, area[2], area[3] + extra)


def show_control(hwnd, visible):
    if hwnd:
        user32.ShowWindow(hwnd, SW_SHOW if visible else SW_HIDE)


def set_control_enabled(hwnd, enabled):
    if hwnd:
        user32.EnableWindow(hwnd, enabled)


def apply_mode_layout():
    update_client_size()
    multi_url = run_mode == "one_to_many"
    visible_count = url_field_count if multi_url else 1
    for index, control in enumerate(url_controls):
        visible = index < visible_count
        show_control(control, visible)
        if visible:
            move_control(control, 124, url_input_y(index), 211, 21)

    show_control(hwnd_channel, False)
    move_control(hwnd_phrase, 628, 145, 455, 22)
    for control in url_controls:
        set_control_font(control, FONT_BODY)
    set_control_font(hwnd_channel, FONT_BODY)
    set_control_font(hwnd_phrase, FONT_BODY)
    show_control(hwnd_reply_list, False)
    show_control(hwnd_phrase, reply_filter != "default")
    if editing_profile_name:
        position_profile_name_editor()
    if hwnd_main:
        user32.InvalidateRect(hwnd_main, None, True)


def save_robot_cards(profiles=None, names=None, next_number=None):
    save_card_state(
        ROBOT_CARD_STATE_FILE,
        list(PROFILES.values()) if profiles is None else profiles,
        PROFILE_DISPLAY_NAMES if names is None else names,
        next_profile_number if next_number is None else next_number,
    )


def profile_avatar_file(profile_dir, avatar_url=None):
    account_id = account_id_for_profile(profile_dir)
    if avatar_url is None:
        avatar_url = PROFILE_AVATAR_URLS.get(profile_dir)
    if avatar_url:
        digest = hashlib.sha256(avatar_url.encode("utf-8")).hexdigest()[:16]
        return PROFILE_AVATAR_DIR / f"{account_id}_{digest}.jpg"
    try:
        return max(
            PROFILE_AVATAR_DIR.glob(f"{account_id}_*.jpg"),
            key=lambda path: path.stat().st_mtime_ns,
        )
    except (OSError, ValueError):
        return PROFILE_AVATAR_DIR / f"{account_id}_missing.jpg"


def receive_facebook_profile(account_id, display_name, avatar_url):
    """Called by the local bridge after the extension identifies its browser profile."""
    with profile_state_lock:
        profile_dir = next(
            (directory for name, directory in PROFILES.items() if PROFILE_ACCOUNTS[name] == account_id),
            None,
        )
        if profile_dir is None:
            return False
        name_changed = bool(display_name and PROFILE_DISPLAY_NAMES.get(profile_dir) != display_name)
        if name_changed:
            PROFILE_DISPLAY_NAMES[profile_dir] = display_name
            _, color = PROFILE_AVATARS.get(profile_dir, ("FB", COLOR_GREEN))
            PROFILE_AVATARS[profile_dir] = (display_name[:1], color)
            try:
                save_robot_cards()
            except OSError:
                pass
    image_changed = False
    if avatar_url and PROFILE_AVATAR_URLS.get(profile_dir) != avatar_url:
        destination = profile_avatar_file(profile_dir, avatar_url)
        image_changed = destination.exists() or download_avatar(avatar_url, destination)
        if image_changed:
            PROFILE_AVATAR_URLS[profile_dir] = avatar_url
    if name_changed or image_changed:
        if hwnd_main:
            user32.InvalidateRect(hwnd_main, None, True)
        details = "姓名與頭貼" if name_changed and image_changed else "姓名" if name_changed else "頭貼"
        set_status(f"已同步 {profile_label(profile_dir)} 的 Facebook {details}。")
    return bool(display_name or image_changed or profile_avatar_file(profile_dir).exists())


def add_account_profile():
    global next_profile_number, selected_profile
    if is_busy:
        return
    if len(PROFILES) >= MAX_ACCOUNTS:
        set_status("已達帳號上限。")
        return

    index, restoring = next_profile_to_show(PROFILES.values(), next_profile_number)
    if index > 25:
        show_message("無法新增", "機器人編號已用完，請保留現有字卡。", error=True)
        return
    name = account_label(index)
    profile_dir = chrome_profile_dir(index)
    ordered_dirs = sorted([*PROFILES.values(), profile_dir], key=profile_number)
    try:
        with profile_state_lock:
            save_robot_cards(ordered_dirs, next_number=next_profile_number if restoring else index + 1)
            if not restoring:
                next_profile_number = index + 1
            PROFILES.clear()
            PROFILES.update((profile_label(directory), directory) for directory in ordered_dirs)
            PROFILE_ACCOUNTS[name] = account_id_for_profile(profile_dir)
            saved_name = normalize_display_name(PROFILE_DISPLAY_NAMES.get(profile_dir))
            PROFILE_AVATARS[profile_dir] = (
                saved_name[:1] if saved_name else chr(ord("A") + index),
                ACCOUNT_COLORS[index % len(ACCOUNT_COLORS)],
            )
            PROFILE_DISPLAY_NAMES.setdefault(profile_dir, "Facebook 使用者")
    except OSError as exc:
        show_message("無法儲存帳號", str(exc), error=True)
        return
    if not selected_profile:
        selected_profile = name
        selected_profiles.add(name)
    apply_mode_layout()
    try:
        setup_url = ("https://www.facebook.com/me#" if restoring else "https://www.facebook.com/?") + urlencode(
            {"fb_auto_account": PROFILE_ACCOUNTS[name]}
        )
        open_chrome(setup_url, profile_dir)
        if restoring:
            set_status(f"已重新顯示 {name}，並開啟原本的 Facebook 帳號。")
        else:
            set_status(f"已新增 {name}（{profile_dir}），並開啟 Facebook 登入頁；登入狀態會保存在此 Chrome Profile。")
    except Exception as exc:
        set_status(f"{'已重新顯示' if restoring else '已新增'} {name}，但無法開啟 Facebook：{exc}")


def remove_account_profile(profile_name):
    global selected_profile, stream_reply_mode
    if profile_name not in PROFILES or is_busy:
        return
    affected_rows = [
        index for index, state in enumerate(url_states)
        if state in {"running", "paused"} and profile_name in url_active_profiles[index]
    ]
    consequence = (
        f"這也會停止第 {', '.join(str(index + 1) for index in affected_rows)} 列正在使用此機器人的直播。\n"
        if affected_rows else ""
    )
    confirmation = user32.MessageBoxW(
        hwnd_main,
        f"要關閉 {profile_name} 的字卡嗎？\n{consequence}Chrome Profile 與 Facebook 登入資料不會刪除，再按「新增帳號」可重新顯示。",
        "關閉機器人字卡",
        0x24 | 0x100,  # MB_YESNO | MB_ICONQUESTION | MB_DEFBUTTON2
    )
    if confirmation != 6:  # IDYES
        return
    try:
        with profile_state_lock:
            remaining_dirs = [directory for name, directory in PROFILES.items() if name != profile_name]
            save_robot_cards(remaining_dirs)
            removed_dir = PROFILES.pop(profile_name)
            PROFILE_ACCOUNTS.pop(profile_name, None)
            PROFILE_AVATAR_URLS.pop(removed_dir, None)
    except OSError as exc:
        show_message("無法儲存變更", str(exc), error=True)
        return
    for index in affected_rows:
        stop_url_row(index)
    selected_profiles.discard(profile_name)
    if selected_profile == profile_name:
        selected_profile = next(iter(selected_profiles), next(iter(PROFILES), ""))
    if not selected_profiles and selected_profile:
        selected_profiles.add(selected_profile)
    apply_mode_layout()
    set_status(f"已關閉 {profile_name} 字卡；登入資料仍保留，可按新增帳號重新顯示。")
    if PROFILES:
        def worker():
            try:
                ensure_llm_ready()
                refresh_reply_list()
            except Exception as exc:
                set_status(f"字卡已移除，語句同步暫時失敗：{exc}")
        threading.Thread(target=worker, daemon=True).start()
    else:
        stream_reply_mode = "manual"
        populate_reply_list([])


def add_url_field():
    global url_field_count
    if run_mode != "one_to_many" or is_busy:
        return
    if url_field_count >= len(url_controls):
        set_status("已達網址輸入上限。")
        return
    url_field_count += 1
    apply_mode_layout()
    set_status(f"已新增第 {url_field_count} 個直播網址輸入欄。")


def set_profile_selection(profile_name):
    global selected_profile, selected_profiles
    selected_profile = profile_name
    if profile_name in selected_profiles and len(selected_profiles) > 1:
        selected_profiles.remove(profile_name)
    else:
        selected_profiles.add(profile_name)
    set_status(f"已選擇 {', '.join(selected_profiles)}。")
    if hwnd_main:
        user32.InvalidateRect(hwnd_main, None, True)


def sync_profile_clicked(profile_name):
    if profile_name not in PROFILES:
        return
    profile_dir = PROFILES[profile_name]
    account_id = PROFILE_ACCOUNTS[profile_name]

    def worker():
        try:
            # Facebook preserves the fragment when /me redirects to this user's profile.
            # The content script can then read the visible name and profile photo.
            url = "https://www.facebook.com/me#" + urlencode({"fb_auto_account": account_id})
            open_chrome(url, profile_dir)
            set_status(f"已開啟 {profile_name} 的 Facebook 個人檔案，正在嘗試同步姓名與頭貼。")
        except Exception as exc:
            set_status(f"無法開啟 {profile_name} 的 Facebook 個人檔案：{exc}")

    threading.Thread(target=worker, daemon=True).start()


def draw_text(hdc, text, area, font, color, flags=DT_LEFT):
    old_font = gdi32.SelectObject(hdc, scaled_font(font))
    old_mode = gdi32.SetBkMode(hdc, TRANSPARENT)
    gdi32.SetTextColor(hdc, color)
    rc = rect(*shifted_area(area))
    user32.DrawTextW(hdc, text, -1, ctypes.byref(rc), flags)
    gdi32.SetBkMode(hdc, old_mode)
    gdi32.SelectObject(hdc, old_font)


def fill_rect(hdc, area, color):
    brush = gdi32.CreateSolidBrush(color)
    rc = rect(*shifted_area(area))
    user32.FillRect(hdc, ctypes.byref(rc), brush)
    gdi32.DeleteObject(brush)


def fill_raw_rect(hdc, area, color):
    brush = gdi32.CreateSolidBrush(color)
    rc = rect(*area)
    user32.FillRect(hdc, ctypes.byref(rc), brush)
    gdi32.DeleteObject(brush)


def dynamic_card_rect():
    bottom = max(CARD_RECT[3], current_start_rect()[3] + 16)
    return (CARD_RECT[0], CARD_RECT[1], CARD_RECT[2], bottom)


def dynamic_shadow_rect():
    card = dynamic_card_rect()
    return (card[0] + 7, card[1] + 10, card[2] + 7, card[3] + 10)


def dynamic_header_rect():
    card = dynamic_card_rect()
    return (card[0], card[1], card[2], card[1] + 82)


def fill_round_rect(hdc, area, color, radius=18):
    area = shifted_area(area)
    scaled_radius = max(2, int(round(radius * visual_scale())))
    brush = gdi32.CreateSolidBrush(color)
    old_brush = gdi32.SelectObject(hdc, brush)
    old_pen = gdi32.SelectObject(hdc, gdi32.GetStockObject(8))
    gdi32.RoundRect(hdc, area[0], area[1], area[2], area[3], scaled_radius, scaled_radius)
    gdi32.SelectObject(hdc, old_pen)
    gdi32.SelectObject(hdc, old_brush)
    gdi32.DeleteObject(brush)


def stroke_round_rect(hdc, area, color, radius=18, width=1):
    area = shifted_area(area)
    scaled_radius = max(2, int(round(radius * visual_scale())))
    scaled_width = max(1, int(round(width * visual_scale())))
    pen = gdi32.CreatePen(0, scaled_width, color)
    old_pen = gdi32.SelectObject(hdc, pen)
    old_brush = gdi32.SelectObject(hdc, gdi32.GetStockObject(5))
    gdi32.RoundRect(hdc, area[0], area[1], area[2], area[3], scaled_radius, scaled_radius)
    gdi32.SelectObject(hdc, old_brush)
    gdi32.SelectObject(hdc, old_pen)
    gdi32.DeleteObject(pen)


def fill_ellipse(hdc, area, color):
    original = area
    area = shifted_area(area)
    if abs((original[2] - original[0]) - (original[3] - original[1])) <= 2:
        width = area[2] - area[0]
        height = area[3] - area[1]
        diameter = min(width, height)
        center_x = (area[0] + area[2]) // 2
        center_y = (area[1] + area[3]) // 2
        area = (
            center_x - diameter // 2,
            center_y - diameter // 2,
            center_x + (diameter + 1) // 2,
            center_y + (diameter + 1) // 2,
        )
    brush = gdi32.CreateSolidBrush(color)
    old_brush = gdi32.SelectObject(hdc, brush)
    old_pen = gdi32.SelectObject(hdc, gdi32.GetStockObject(8))
    gdi32.Ellipse(hdc, area[0], area[1], area[2], area[3])
    gdi32.SelectObject(hdc, old_pen)
    gdi32.SelectObject(hdc, old_brush)
    gdi32.DeleteObject(brush)


def draw_profile_picture(hdc, profile_dir, area):
    """Draw a cached Facebook image at native display resolution in a circle."""
    if not GDIPLUS_READY:
        return False
    path = profile_avatar_file(profile_dir)
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return False
    key = str(path)
    cached = PROFILE_IMAGE_CACHE.get(key)
    if cached is None or cached[0] != stamp:
        image_handle = ctypes.c_void_p()
        if gdiplus.GdipLoadImageFromFile(key, ctypes.byref(image_handle)) != 0:
            return False
        if cached is not None:
            gdiplus.GdipDisposeImage(cached[1])
        PROFILE_IMAGE_CACHE[key] = (stamp, image_handle)
    else:
        image_handle = cached[1]

    left, top, right, bottom = shifted_area(area)
    diameter = min(right - left, bottom - top)
    left = (left + right - diameter) // 2
    top = (top + bottom - diameter) // 2
    graphics = ctypes.c_void_p()
    path_handle = ctypes.c_void_p()
    if gdiplus.GdipCreateFromHDC(hdc, ctypes.byref(graphics)) != 0:
        return False
    try:
        gdiplus.GdipSetInterpolationMode(graphics, 7)  # HighQualityBicubic
        gdiplus.GdipSetSmoothingMode(graphics, 4)  # AntiAlias
        if gdiplus.GdipCreatePath(0, ctypes.byref(path_handle)) != 0:
            return False
        gdiplus.GdipAddPathEllipseI(path_handle, left, top, diameter, diameter)
        gdiplus.GdipSetClipPath(graphics, path_handle, 0)
        return gdiplus.GdipDrawImageRectI(
            graphics, image_handle, left, top, diameter, diameter
        ) == 0
    finally:
        if path_handle:
            gdiplus.GdipDeletePath(path_handle)
        gdiplus.GdipDeleteGraphics(graphics)


def draw_auto_mascot(hdc, area):
    global AUTO_IMAGE_HANDLE
    if not GDIPLUS_READY or not AUTO_MODE_MASCOT.exists():
        return
    if AUTO_IMAGE_HANDLE is None:
        image_handle = ctypes.c_void_p()
        if gdiplus.GdipLoadImageFromFile(str(AUTO_MODE_MASCOT), ctypes.byref(image_handle)) != 0:
            return
        AUTO_IMAGE_HANDLE = image_handle
    left, top, right, bottom = shifted_area(area)
    graphics = ctypes.c_void_p()
    if gdiplus.GdipCreateFromHDC(hdc, ctypes.byref(graphics)) != 0:
        return
    try:
        gdiplus.GdipSetInterpolationMode(graphics, 7)
        gdiplus.GdipDrawImageRectI(graphics, AUTO_IMAGE_HANDLE, left, top, right - left, bottom - top)
    finally:
        gdiplus.GdipDeleteGraphics(graphics)


def draw_line(hdc, x1, y1, x2, y2, color, width=2):
    pen = gdi32.CreatePen(0, max(1, int(round(width * visual_scale()))), color)
    old_pen = gdi32.SelectObject(hdc, pen)
    x1, y1 = shifted_point(x1, y1)
    x2, y2 = shifted_point(x2, y2)
    gdi32.MoveToEx(hdc, x1, y1, None)
    gdi32.LineTo(hdc, x2, y2)
    gdi32.SelectObject(hdc, old_pen)
    gdi32.DeleteObject(pen)


def point_in_rect(x, y, area):
    area = shifted_area(area)
    return area[0] <= x <= area[2] and area[1] <= y <= area[3]


def url_input_y(index):
    return below_accounts_area((0, 371 + index * 56, 0, 0))[1]


def url_box_rect(index):
    y = 364 + index * 56
    return below_accounts_area((32, y, 564, y + 48))


def url_status_rect(index):
    area = url_box_rect(index)
    return (354, area[1] + 18, 363, area[1] + 27)


def url_start_rect(index):
    area = url_box_rect(index)
    return (432, area[1] + 8, 490, area[3] - 8)


def url_stop_rect(index):
    area = url_box_rect(index)
    return (498, area[1] + 8, 556, area[3] - 8)


def current_add_url_rect():
    y = 364 + url_field_count * 56
    return below_accounts_area((32, y, 564, y + 48))


def source_card_rect():
    last_row = current_add_url_rect() if url_field_count < MAX_URL_FIELDS else url_box_rect(url_field_count - 1)
    return (16, 300 + account_field_extra_y(), 580, last_row[3] + 16)


def favorite_card_rect():
    top = source_card_rect()[3] + 12
    return (16, top, 580, top + 194)


def favorite_row_rect(index):
    card = favorite_card_rect()
    top = card[1] + 74 + index * 38
    return (32, top, 564, top + 36)


def favorite_go_rect(index):
    row = favorite_row_rect(index)
    return (442, row[1] + 5, 496, row[3] - 5)


def favorite_delete_rect(index):
    row = favorite_row_rect(index)
    return (508, row[1] + 5, 562, row[3] - 5)


def account_row_count():
    visible_cards = len(PROFILES) + (1 if len(PROFILES) < MAX_ACCOUNTS else 0)
    return max(1, (visible_cards + 3) // 4)


def profile_rects():
    base_areas = list(PROFILE_RECTS.values())
    result = {}
    for index, name in enumerate(PROFILES):
        base = base_areas[index % 4]
        row = index // 4
        result[name] = (
            base[0], base[1] + row * ACCOUNT_ROW_SPACING,
            base[2], base[3] + row * ACCOUNT_ROW_SPACING,
        )
    return result


def current_add_account_rect():
    index = len(PROFILES)
    base = list(PROFILE_RECTS.values())[index % 4]
    row = index // 4
    return (
        base[0], base[1] + row * ACCOUNT_ROW_SPACING,
        base[2], base[3] + row * ACCOUNT_ROW_SPACING,
    )


def profile_delete_rect(area):
    return (area[2] - 29, area[1] + 8, area[2] - 8, area[1] + 29)


def profile_avatar_rect(area):
    center_x = (area[0] + area[2]) // 2
    return (center_x - 18, area[1] + 14, center_x + 18, area[1] + 50)


def profile_name_rect(area):
    return (area[0] + 3, area[1] + 80, area[2] - 3, area[1] + 102)


def position_profile_name_editor():
    area = profile_rects().get(editing_profile_name)
    if not area:
        return
    name_area = profile_name_rect(area)
    move_control(hwnd_profile_name_edit, name_area[0], name_area[1], 82, 22)
    move_control(hwnd_profile_name_save, name_area[0] + 84, name_area[1], 30, 22)


def current_phrase_label_y():
    if run_mode == "one_to_many":
        return below_accounts_area((0, 397 + url_field_count * 39 + 48, 0, 0))[1]
    return below_accounts_area((0, 444, 0, 0))[1]


def current_phrase_box_y():
    if run_mode == "one_to_many":
        return current_phrase_label_y() + 23
    return below_accounts_area((0, 518, 0, 0))[1]


def current_channel_label_y():
    if run_mode == "one_to_many":
        return below_accounts_area((0, 397 + url_field_count * 39 + 48, 0, 0))[1]
    return below_accounts_area((0, 444, 0, 0))[1]


def current_channel_box_y():
    return current_channel_label_y() + 23


def current_channel_input_y():
    return current_channel_box_y() + 8


def current_phrase_input_y():
    return current_phrase_box_y() + 8


def current_save_phrase_rect():
    return SAVE_PHRASE_RECT


def current_start_rect():
    if run_mode == "one_to_many":
        y = max(608, current_save_phrase_rect()[3] + 8)
        return (140, y, 290, y + 50)
    return below_accounts_area((140, START_RECT[1] + 47, 290, START_RECT[3] + 47))


def current_stop_rect():
    start = current_start_rect()
    return (300, start[1], 434, start[3])


def get_control_text(hwnd):
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value.strip()


def start_profile_name_edit(profile_name):
    global editing_profile_name
    if profile_name not in PROFILES:
        return
    editing_profile_name = profile_name
    profile_dir = PROFILES[profile_name]
    current = normalize_display_name(PROFILE_DISPLAY_NAMES.get(profile_dir))
    user32.SetWindowTextW(hwnd_profile_name_edit, current)
    position_profile_name_editor()
    show_control(hwnd_profile_name_edit, True)
    show_control(hwnd_profile_name_save, True)
    user32.SetFocus(hwnd_profile_name_edit)
    set_status(f"請輸入 {profile_name} 的顯示名稱，按 ✓ 儲存。")


def finish_profile_name_edit(save):
    global editing_profile_name
    profile_name = editing_profile_name
    if not profile_name:
        return
    if save:
        name = normalize_display_name(get_control_text(hwnd_profile_name_edit))
        if not name:
            show_message("名稱無效", "請輸入有效的使用者名稱。", error=True)
            user32.SetFocus(hwnd_profile_name_edit)
            return
        profile_dir = PROFILES[profile_name]
        try:
            with profile_state_lock:
                names = {**PROFILE_DISPLAY_NAMES, profile_dir: name}
                save_robot_cards(names=names)
                PROFILE_DISPLAY_NAMES[profile_dir] = name
                _, color = PROFILE_AVATARS.get(profile_dir, ("FB", COLOR_GREEN))
                PROFILE_AVATARS[profile_dir] = (name[:1], color)
        except OSError as exc:
            show_message("無法儲存名稱", str(exc), error=True)
            return
    editing_profile_name = ""
    show_control(hwnd_profile_name_edit, False)
    show_control(hwnd_profile_name_save, False)
    set_status(f"已儲存 {profile_name} 的名稱。" if save else "已取消編輯名稱。")


def selected_profile_dir():
    return PROFILES.get(selected_profile, "Default")


def set_status(text):
    global status_text
    status_text = text
    if hwnd_main:
        user32.InvalidateRect(hwnd_main, None, True)


def set_latest_reply(reply, source):
    global latest_reply_text, latest_input_text
    clean_reply = str(reply or "").strip()
    if not clean_reply:
        clean_reply = "尚未產生留言。"
    elif clean_reply.lower() == "ignore":
        clean_reply = "本次不留言（ignore）"
    else:
        clean_reply = f"準備留言：{clean_reply}"

    latest_reply_text = clean_reply
    latest_input_text = str(source or "").strip()
    if hwnd_main:
        user32.InvalidateRect(hwnd_main, None, True)


def read_latest_reply():
    files = []
    if REPLY_DIR.exists():
        files.extend(REPLY_DIR.glob("*_reply.jsonl"))
        files.extend(REPLY_DIR.glob("*__Reply.jsonl"))
    legacy_file = BASE_DIR / "Reply.jsonl"
    if legacy_file.exists():
        files.append(legacy_file)
    if not files:
        return None
    latest_file = max(files, key=lambda path: path.stat().st_mtime)
    lines = latest_file.read_text(encoding="utf-8", errors="ignore").splitlines()
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


def monitor_reply_file():
    global last_reply_mtimes
    while not stop_monitor:
        try:
            files = list(REPLY_DIR.glob("*_reply.jsonl")) if REPLY_DIR.exists() else []
            if REPLY_DIR.exists():
                files.extend(REPLY_DIR.glob("*__Reply.jsonl"))
            legacy_file = BASE_DIR / "Reply.jsonl"
            if legacy_file.exists():
                files.append(legacy_file)
            for reply_file in files:
                key = str(reply_file)
                mtime = reply_file.stat().st_mtime
                if mtime != last_reply_mtimes.get(key):
                    last_reply_mtimes[key] = mtime
                    data = read_latest_reply()
                    if data:
                        set_latest_reply(data.get("reply"), data.get("raw_text") or data.get("input"))
        except Exception:
            pass
        time.sleep(1)


def clean_live_title(value):
    title = str(value or "").strip()
    if "|" in title:
        prefix, remainder = title.split("|", 1)
        prefix_lower = prefix.lower()
        if "view" in prefix_lower or "reaction" in prefix_lower or "觀看" in prefix:
            title = remainder.strip()
    return title


def detected_streamer_name(data):
    uploader = str(data.get("live_uploader") or "").strip()
    if uploader:
        return uploader
    title = clean_live_title(data.get("live_title"))
    if "|" in title:
        title = title.rsplit("|", 1)[-1].strip()
    return title


def remember_favorite_streamer(name, live_url, uploader_url="", uploader_id="", entered_at=None):
    global favorite_streamers
    uploader_id = str(uploader_id or "")
    id_url = f"https://www.facebook.com/{uploader_id}" if re.fullmatch(r"[A-Za-z0-9._-]{2,100}", uploader_id) else ""
    profile_url = resolve_profile_url(live_url, uploader_url, id_url)
    if not name or not profile_url:
        return False
    with favorite_lock:
        timestamp = entered_at or url_entered_at.get(live_url) or time.time_ns()
        updated = upsert_favorite(favorite_streamers, name, profile_url, timestamp)
        if updated == favorite_streamers:
            return True
        save_favorites(FAVORITES_FILE, updated)
        favorite_streamers = updated
    if hwnd_main:
        user32.InvalidateRect(hwnd_main, None, True)
    return True


def open_favorite_streamer(index):
    with favorite_lock:
        if index >= len(favorite_streamers):
            return
        item = dict(favorite_streamers[index])

    def worker():
        try:
            open_chrome(item["url"], selected_profile_dir())
            set_status(f"已開啟 {item['name']} 的 Facebook 個人主頁。")
        except Exception as exc:
            set_status(f"無法開啟直播主主頁：{exc}")

    threading.Thread(target=worker, daemon=True).start()


def delete_favorite_streamer(index):
    global favorite_streamers
    with favorite_lock:
        if index >= len(favorite_streamers):
            return
        updated = [item for position, item in enumerate(favorite_streamers) if position != index]
        try:
            save_favorites(FAVORITES_FILE, updated)
        except OSError as exc:
            show_message("無法刪除常用直播主", str(exc), error=True)
            return
        favorite_streamers = updated
    set_status("已刪除常用直播主。")


def probe_streamer_metadata(url, profile_dir):
    if STT_EXE.exists():
        command = [str(STT_EXE), "--probe", "--url", url, "--chrome-profile", profile_dir]
        working_dir = BASE_DIR
    else:
        command = [sys.executable, str(STT_SCRIPT), "--probe", "--url", url, "--chrome-profile", profile_dir]
        working_dir = STT_SCRIPT.parent
    completed = subprocess.run(
        command, cwd=str(working_dir), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=45,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0:
        raise RuntimeError("STT 無法取得這個直播的資料")
    for line in reversed(completed.stdout.splitlines()):
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and (data.get("live_uploader") or data.get("live_title")):
            return data
    raise RuntimeError("STT 沒有回傳直播主名稱")


def schedule_favorite_probe(index, url=None):
    if index >= len(url_controls):
        return
    live_url = (url or get_control_text(url_controls[index])).strip().strip("\"'")
    host = (urlsplit(live_url).hostname or "").lower()
    if not live_url.startswith("https://") or not (host == "facebook.com" or host.endswith(".facebook.com")):
        return
    with favorite_lock:
        url_entered_at[live_url] = time.time_ns()
        cached = favorite_probe_cache.get(live_url)
        if live_url in favorite_probe_pending:
            return
        if not cached:
            favorite_probe_pending.add(live_url)
    if cached:
        remember_favorite_streamer(
            detected_streamer_name(cached), live_url,
            cached.get("live_uploader_url", ""), cached.get("live_uploader_id", ""),
        )
        return
    set_status("正在辨識直播主與 Facebook 個人主頁...")

    def worker():
        try:
            data = probe_streamer_metadata(live_url, selected_profile_dir())
            with favorite_lock:
                favorite_probe_cache[live_url] = data
            if remember_favorite_streamer(
                detected_streamer_name(data), live_url,
                data.get("live_uploader_url", ""), data.get("live_uploader_id", ""),
            ):
                set_status("已加入常用直播主。")
            else:
                set_status("已辨識直播主，但找不到可儲存的 Facebook 個人主頁。")
        except Exception as exc:
            set_status(f"暫時無法儲存常用直播主：{exc}")
        finally:
            with favorite_lock:
                favorite_probe_pending.discard(live_url)

    threading.Thread(target=worker, name="FavoriteStreamerProbe", daemon=True).start()


def apply_detected_stream_title(text_file, data):
    """Reflect STT's detected title in the read-only UI and session metadata."""
    title = detected_streamer_name(data)
    if not title:
        return

    user32.SetWindowTextW(hwnd_channel, title)
    match = re.search(r"_live_(\d+)$", text_file.stem)
    if match:
        index = int(match.group(1)) - 1
        if 0 <= index < len(url_controls) and (
            index in starting_stream_indexes
            or url_states[index] in {"running", "paused"}
            or (index == 0 and pipeline_state in {"running", "paused"})
        ):
            user32.SetWindowTextW(url_controls[index], title)
            user32.SendMessageW(url_controls[index], EM_SETREADONLY, True, 0)

    metadata_file = SESSION_META_DIR / f"{text_file.stem}.json"
    try:
        metadata = json.loads(metadata_file.read_text(encoding="utf-8")) if metadata_file.exists() else {}
        live_url = metadata.get("facebook_url", "")
        if live_url:
            remember_favorite_streamer(
                title, live_url, data.get("live_uploader_url", ""), data.get("live_uploader_id", ""),
            )
        metadata["channel_name"] = title
        metadata["live_title"] = title
        metadata["live_uploader"] = title
        metadata_file.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, ValueError):
        pass
    set_status(f"已偵測直播名稱：{title}")


def monitor_stream_titles():
    """Consume STT metadata records without sending them through the LLM."""
    global last_stream_mtimes
    while not stop_monitor:
        try:
            files = list(SESSION_TEXT_DIR.glob("*.jsonl")) if SESSION_TEXT_DIR.exists() else []
            for text_file in files:
                key = str(text_file)
                mtime = text_file.stat().st_mtime
                if mtime < APP_START_TIME:
                    last_stream_mtimes[key] = mtime
                    continue
                if mtime == last_stream_mtimes.get(key):
                    continue
                last_stream_mtimes[key] = mtime
                for line in text_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                    try:
                        data = json.loads(line)
                    except (TypeError, json.JSONDecodeError):
                        continue
                    if data.get("event") == "stream_metadata" or data.get("live_title"):
                        apply_detected_stream_title(text_file, data)
                        break
        except Exception:
            pass
        time.sleep(0.5)


def populate_reply_list(items, organize=False):
    global reply_items, selected_reply_index, reply_scroll_offset
    if organize:
        reply_items = sorted(
            items,
            key=lambda item: (
                0 if item.get("source") == "default" else 1,
                0 if item.get("enabled", True) else 1,
            ),
        )
    else:
        reply_items = list(items)
    filtered_count = sum(
        1
        for item in reply_items
        if (
            reply_filter == "all"
            or (reply_filter == "enabled" and item.get("enabled", True))
            or item.get("source") == reply_filter
        )
    )
    reply_scroll_offset = min(
        reply_scroll_offset,
        max(0, filtered_count - reply_visible_count()),
    )
    if selected_reply_index >= len(reply_items):
        selected_reply_index = -1
    if hwnd_main:
        user32.InvalidateRect(hwnd_main, None, True)
    return
    if not hwnd_reply_list:
        return
    user32.SendMessageW(hwnd_reply_list, LB_RESETCONTENT, 0, 0)
    for item in reply_items:
        text = item.get("text", "")
        enabled = item.get("enabled", True)
        label = text if enabled else f"[停用] {text}"
        user32.SendMessageW(hwnd_reply_list, LB_ADDSTRING, 0, label)


def refresh_reply_list(organize=False):
    global stream_reply_mode, pending_reply_mode
    if reply_filter == "crowd":
        stream_data = request_json(f"{current_llm_base()}/stream_replies?stream_id={current_reply_stream_id()}", method="GET")
        if stream_data.get("status") != "success":
            raise RuntimeError(stream_data.get("error", "無法讀取回覆模式"))
        stream_reply_mode = stream_data.get("reply_mode", "manual")
        if not mode_selection_dirty:
            pending_reply_mode = stream_reply_mode
        show_control(hwnd_phrase, True)
        data = request_json(f"{DB_API_BASE}/crowd_slogans", method="GET")
        if data.get("status") != "success":
            raise RuntimeError(data.get("error", "無法讀取衝人氣語句"))
        items = [dict(item, source="crowd", locked_text=False) for item in data.get("items", [])]
        populate_reply_list(items, organize=False)
        set_status(f"衝人氣語句：{len(items)} 筆；手動與自動模式都會使用")
        return
    default_data = request_json(f"{DB_API_BASE}/default_replies", method="GET")
    user_data = request_json(f"{DB_API_BASE}/user_input", method="GET")
    if default_data.get("status") != "success":
        raise RuntimeError(default_data.get("error", "讀取預設語句失敗"))
    if user_data.get("status") != "success":
        raise RuntimeError(user_data.get("error", "讀取自訂語句失敗"))

    items = []
    seen_default_texts = set()
    for item in default_data.get("items", []):
        text = item.get("text", "")
        if text in seen_default_texts:
            continue
        seen_default_texts.add(text)
        item["source"] = "default"
        item["locked_text"] = True
        items.append(item)
    for item in user_data.get("items", []):
        item["source"] = "user"
        item["locked_text"] = False
        items.append(item)

    stream_id = current_reply_stream_id()
    stream_data = request_json(f"{current_llm_base()}/stream_replies?stream_id={stream_id}", method="GET")
    if stream_data.get("status") != "success":
        raise RuntimeError(stream_data.get("error", "讀取直播語句設定失敗"))
    enabled_keys = set(stream_data.get("enabled_keys", []))
    stream_reply_mode = stream_data.get("reply_mode", "manual")
    if not mode_selection_dirty:
        pending_reply_mode = stream_reply_mode
    set_control_enabled(hwnd_phrase, not is_busy)
    show_control(hwnd_phrase, reply_filter != "default")
    for item in items:
        item["reply_key"] = f"{item.get('source')}:{item.get('id')}"
        item["enabled"] = item["reply_key"] in enabled_keys
        item["stream_id"] = stream_id

    populate_reply_list(items, organize=organize)
    set_status(f"已載入直播 {active_reply_stream_index + 1} 的語句設定，共 {len(items)} 筆。")


def set_stream_reply_mode(mode, stream_id=None):
    global stream_reply_mode
    target_stream_id = stream_id or current_reply_stream_id()
    data = request_json(
        f"{current_llm_base()}/stream_replies",
        {"stream_id": target_stream_id, "reply_mode": mode},
        method="PATCH",
    )
    if data.get("status") != "success":
        raise RuntimeError(data.get("error", "切換回覆模式失敗"))
    if target_stream_id != current_reply_stream_id():
        return
    stream_reply_mode = mode
    set_control_enabled(hwnd_phrase, not is_busy)
    show_control(hwnd_phrase, reply_filter != "default")
    set_status({
        "manual": "手動模式：以向量比對從已啟用的現有語句中選擇。",
        "auto": "自動模式：模型會參考三類語句，也可自然生成新語句。",
    }.get(mode, "已更新回覆模式。"))
    if hwnd_main:
        user32.InvalidateRect(hwnd_main, None, True)


def current_reply_stream_id():
    if url_stream_ids[active_reply_stream_index]:
        return url_stream_ids[active_reply_stream_index]
    profile_name = next((name for name in PROFILES if name in selected_profiles), selected_profile)
    account = PROFILE_ACCOUNTS.get(profile_name, "fb_account_000")
    return f"{account}_live_{active_reply_stream_index + 1:02d}"


def select_reply_stream(index):
    global active_reply_stream_index, selected_reply_index, reply_scroll_offset
    global mode_selection_dirty, pending_phrase_category
    if index < 0 or index >= url_field_count or index == active_reply_stream_index:
        return
    active_reply_stream_index = index
    selected_reply_index = -1
    reply_scroll_offset = 0
    mode_selection_dirty = False
    pending_phrase_category = phrase_category

    def worker():
        try:
            ensure_llm_ready()
            refresh_reply_list()
        except Exception as exc:
            set_status(f"切換直播語句失敗：{exc}")

    threading.Thread(target=worker, daemon=True).start()


def reload_llm_replies():
    try:
        request_json(f"{current_llm_base()}/reload_replies", method="POST", timeout=5)
    except Exception:
        pass


def draw_logo(hdc):
    fill_rect(hdc, (0, 0, DESIGN_WIDTH, 64), rgb(21, 29, 40))
    draw_line(hdc, 0, 63, DESIGN_WIDTH, 63, rgb(48, 59, 74), 1)
    fill_ellipse(hdc, (18, 12, 56, 50), COLOR_GREEN)
    draw_text(hdc, "FB", (18, 19, 56, 43), FONT_TAB, COLOR_TEXT, DT_CENTER | DT_VCENTER | DT_SINGLELINE)
    draw_text(hdc, "直播自動留言", (72, 12, 270, 38), FONT_TITLE, COLOR_TEXT, DT_LEFT | DT_VCENTER | DT_SINGLELINE)
    draw_text(hdc, "Facebook 直播自動留言系統", (72, 39, 310, 59), FONT_SMALL, COLOR_MUTED, DT_LEFT | DT_VCENTER | DT_SINGLELINE)
    running = any(state == "running" for state in url_states)
    dot_color = COLOR_GREEN if running else COLOR_MUTED
    fill_ellipse(hdc, (1118, 28, 1125, 35), dot_color)
    draw_text(hdc, "系統運行中" if running else "系統待命", (1132, 20, 1228, 44), FONT_SMALL, COLOR_MUTED, DT_LEFT | DT_VCENTER | DT_SINGLELINE)
    draw_line(hdc, 1230, 16, 1230, 49, rgb(53, 65, 80), 1)
    draw_text(hdc, "⚙", (1237, 17, 1271, 48), FONT_HEADING, COLOR_MUTED, DT_CENTER | DT_VCENTER | DT_SINGLELINE)


def draw_profile_tabs(hdc):
    for name, area in profile_rects().items():
        active = name in selected_profiles
        fill_round_rect(hdc, area, rgb(23, 39, 42) if active else rgb(27, 36, 48), 10)
        stroke_round_rect(hdc, area, COLOR_GREEN if active else rgb(61, 73, 88), 10, 2 if active else 1)
        profile_dir = PROFILES[name]
        avatar, avatar_color = PROFILE_AVATARS.get(profile_dir, (name[-1:], COLOR_GREEN))
        center_x = (area[0] + area[2]) // 2
        avatar_area = profile_avatar_rect(area)
        fill_ellipse(hdc, avatar_area, avatar_color)
        draw_text(hdc, avatar[:2], avatar_area, FONT_TAB, COLOR_TEXT, DT_CENTER | DT_VCENTER | DT_SINGLELINE)
        draw_text(hdc, name, (area[0] + 4, area[1] + 58, area[2] - 4, area[1] + 81), FONT_TAB, COLOR_TEXT, DT_CENTER | DT_VCENTER | DT_SINGLELINE)
        display_name = normalize_display_name(PROFILE_DISPLAY_NAMES.get(profile_dir)) or "點擊輸入名稱"
        draw_text(hdc, display_name, profile_name_rect(area), FONT_TINY, COLOR_MUTED, DT_CENTER | DT_VCENTER | DT_SINGLELINE | DT_END_ELLIPSIS)
        delete_area = profile_delete_rect(area)
        fill_ellipse(hdc, delete_area, rgb(55, 39, 51))
        draw_text(hdc, "×", delete_area, FONT_TAB, rgb(237, 122, 134), DT_CENTER | DT_VCENTER | DT_SINGLELINE)
        running = active and any(state == "running" for state in url_states)
        fill_ellipse(hdc, (center_x - 21, area[1] + 106, center_x - 14, area[1] + 113), COLOR_GREEN if running else rgb(122, 136, 154))
        draw_text(hdc, "運行中" if running else "已停止", (center_x - 11, area[1] + 101, center_x + 35, area[1] + 119), FONT_TINY, COLOR_GREEN if running else COLOR_MUTED, DT_LEFT | DT_VCENTER | DT_SINGLELINE)

    if len(PROFILES) < MAX_ACCOUNTS:
        area = current_add_account_rect()
        fill_round_rect(hdc, area, rgb(24, 32, 43), 10)
        stroke_round_rect(hdc, area, rgb(66, 79, 96), 10)
        fill_ellipse(hdc, ((area[0] + area[2]) // 2 - 17, area[1] + 23, (area[0] + area[2]) // 2 + 17, area[1] + 57), COLOR_GRAY_BUTTON)
        draw_text(hdc, "+", (area[0], area[1] + 22, area[2], area[1] + 58), FONT_TITLE, COLOR_TEXT, DT_CENTER | DT_VCENTER | DT_SINGLELINE)
        draw_text(hdc, "新增帳號", (area[0], area[1] + 70, area[2], area[1] + 94), FONT_TAB, COLOR_TEXT, DT_CENTER | DT_VCENTER | DT_SINGLELINE)
        draw_text(hdc, "新增 Facebook 帳號", (area[0], area[1] + 96, area[2], area[1] + 115), FONT_TINY, COLOR_MUTED, DT_CENTER | DT_VCENTER | DT_SINGLELINE)


def draw_avatar(hdc):
    avatar_text, avatar_color = PROFILE_AVATARS.get(selected_profile_dir(), ("FB", COLOR_GREEN))
    fill_ellipse(hdc, below_accounts_area((245, 246, 329, 330)), avatar_color)
    draw_text(
        hdc, avatar_text, below_accounts_area((245, 246, 329, 330)),
        FONT_AVATAR, rgb(255, 255, 255), DT_CENTER | DT_VCENTER | DT_SINGLELINE,
    )
    display_name = PROFILE_DISPLAY_NAMES.get(
        selected_profile_dir(), selected_profile_dir()
    )
    draw_text(
        hdc, display_name, below_accounts_area((145, 336, 429, 360)),
        FONT_SMALL, COLOR_MUTED, DT_CENTER | DT_VCENTER | DT_SINGLELINE,
    )


def draw_reply_preview(hdc):
    fill_round_rect(hdc, below_accounts_area((104, 572, 442, 600)), COLOR_PANEL, 14)
    stroke_round_rect(hdc, below_accounts_area((104, 572, 442, 600)), rgb(68, 74, 82), 14, 1)
    draw_text(hdc, latest_reply_text, below_accounts_area((118, 579, 428, 596)), FONT_TINY, COLOR_TEXT, DT_CENTER | DT_WORDBREAK)


def draw_llm_current_panel(hdc):
    fill_round_rect(hdc, (502, 218, 828, 326), COLOR_PANEL, 12)
    stroke_round_rect(hdc, (502, 218, 828, 326), rgb(68, 74, 82), 12, 1)
    draw_text(hdc, "目前 LLM 處理", (526, 230, 804, 250), FONT_TINY, COLOR_MUTED, DT_CENTER)
    source = latest_input_text or "尚未收到 STT 文字"
    draw_text(hdc, f"主播：{source}", (526, 256, 804, 288), FONT_TINY, COLOR_TEXT, DT_WORDBREAK)
    draw_text(hdc, latest_reply_text, (526, 294, 804, 318), FONT_TINY, COLOR_GREEN, DT_CENTER | DT_WORDBREAK)


def reply_list_inner_rect():
    return REPLY_LIST_RECT


def reply_visible_count():
    return max(1, (REPLY_LIST_RECT[3] - REPLY_LIST_RECT[1] - 68) // 35)


def reply_row_rect(index):
    top = REPLY_LIST_RECT[1] + 68 + index * 35
    return (REPLY_LIST_RECT[0], top, REPLY_LIST_RECT[2], top + 35)


def visible_reply_items():
    return [item for _, item in visible_reply_entries()]


def filtered_reply_entries():
    return [
        (index, item)
        for index, item in enumerate(reply_items)
        if (
            reply_filter == "all"
            or (reply_filter == "enabled" and item.get("enabled", True))
            or item.get("source") == reply_filter
        )
    ]


class MINMAXINFO(ctypes.Structure):
    _fields_ = [
        ("ptReserved", wintypes.POINT),
        ("ptMaxSize", wintypes.POINT),
        ("ptMaxPosition", wintypes.POINT),
        ("ptMinTrackSize", wintypes.POINT),
        ("ptMaxTrackSize", wintypes.POINT),
    ]


def visible_reply_entries():
    count = reply_visible_count()
    entries = filtered_reply_entries()
    return entries[reply_scroll_offset:reply_scroll_offset + count]


def set_reply_filter(source):
    global reply_filter, phrase_category, reply_scroll_offset, selected_reply_index, editing_reply_id
    if source not in {"default", "user", "crowd", "all", "enabled"}:
        return
    if source != reply_filter:
        editing_reply_id = None
        if hwnd_phrase:
            user32.SetWindowTextW(hwnd_phrase, "")
    reply_filter = source
    if source in {"default", "user", "crowd"}:
        phrase_category = source
    reply_scroll_offset = 0
    selected_reply_index = -1
    if hwnd_main:
        user32.InvalidateRect(hwnd_main, None, True)


def show_phrase_category(source):
    global pending_phrase_category, mode_selection_dirty
    if source not in {"default", "user", "crowd"}:
        return
    pending_phrase_category = source
    set_reply_filter(source)
    mode_selection_dirty = pending_reply_mode != stream_reply_mode
    set_status({
        "default": "已顯示預設語句。",
        "user": "已顯示自訂語句。",
        "crowd": "已顯示獨立資料庫中的衝人氣語句。",
    }[source])

    def worker():
        try:
            ensure_llm_ready()
            refresh_reply_list()
        except Exception as exc:
            set_status(f"更新語句列表失敗：{exc}")

    threading.Thread(target=worker, daemon=True).start()


def toggle_reply_option(option):
    global pending_reply_mode, mode_selection_dirty
    if option not in {"manual", "auto"}:
        return
    pending_reply_mode = option
    mode_selection_dirty = pending_reply_mode != stream_reply_mode
    set_status("已選擇回覆模式，按「套用」後生效。")


def draw_checkbox(hdc, area, checked):
    fill_round_rect(hdc, area, COLOR_GREEN if checked else rgb(28, 39, 52), 4)
    stroke_round_rect(hdc, area, COLOR_GREEN if checked else rgb(100, 116, 137), 4, 1)
    if checked:
        draw_text(hdc, "✓", area, FONT_TINY, COLOR_TEXT, DT_CENTER | DT_VCENTER | DT_SINGLELINE)


def draw_reply_library(hdc):
    fill_round_rect(hdc, REPLY_LIST_RECT, rgb(24, 32, 43), 8)
    stroke_round_rect(hdc, REPLY_LIST_RECT, rgb(61, 75, 93), 8, 1)
    fill_rect(hdc, (612, 270, 1248, 306), rgb(24, 33, 44))
    draw_text(hdc, "▤  留言語句列表", (628, 273, 930, 302), FONT_TAB, COLOR_TEXT, DT_LEFT | DT_VCENTER | DT_SINGLELINE)
    draw_line(hdc, 612, 305, 1248, 305, rgb(58, 72, 89), 1)
    fill_rect(hdc, (612, 306, 1248, 338), rgb(29, 39, 52))
    draw_checkbox(hdc, (631, 314, 645, 328), False)
    draw_text(hdc, "勾選", (666, 308, 725, 336), FONT_TINY, COLOR_MUTED, DT_LEFT | DT_VCENTER | DT_SINGLELINE)
    draw_text(hdc, "語句類型", (724, 308, 811, 336), FONT_TINY, COLOR_MUTED, DT_LEFT | DT_VCENTER | DT_SINGLELINE)
    draw_text(hdc, "語句內容", (819, 308, 1130, 336), FONT_TINY, COLOR_MUTED, DT_LEFT | DT_VCENTER | DT_SINGLELINE)
    draw_text(hdc, "操作", (1171, 308, 1238, 336), FONT_TINY, COLOR_MUTED, DT_CENTER | DT_VCENTER | DT_SINGLELINE)
    draw_line(hdc, 612, 338, 1248, 338, rgb(58, 72, 89), 1)

    for index, (actual_index, item) in enumerate(visible_reply_entries()):
        area = reply_row_rect(index)
        checked = bool(item.get("enabled", True))
        if actual_index == selected_reply_index:
            fill_rect(hdc, area, rgb(30, 54, 54))
        elif index % 2:
            fill_rect(hdc, area, rgb(22, 30, 41))
        draw_line(hdc, area[0], area[3], area[2], area[3], rgb(51, 63, 78), 1)
        draw_checkbox(hdc, (631, area[1] + 10, 645, area[1] + 24), checked)
        source = item.get("source")
        badge_text = "預設" if source == "default" else "自訂" if source == "user" else "衝人氣"
        badge_color = rgb(39, 77, 115) if source == "default" else rgb(74, 58, 111) if source == "user" else rgb(30, 91, 70)
        fill_round_rect(hdc, (724, area[1] + 7, 779, area[1] + 28), badge_color, 12)
        draw_text(hdc, badge_text, (724, area[1] + 6, 779, area[1] + 29), FONT_TINY, rgb(217, 228, 245), DT_CENTER | DT_VCENTER | DT_SINGLELINE)
        draw_text(hdc, item.get("text", ""), (819, area[1] + 4, 1158, area[3] - 3), FONT_SMALL, COLOR_TEXT if checked else COLOR_MUTED, DT_LEFT | DT_VCENTER | DT_SINGLELINE | DT_END_ELLIPSIS)
        if source != "default":
            draw_text(hdc, "✎", (1170, area[1] + 4, 1204, area[3] - 3), FONT_TAB, COLOR_MUTED, DT_CENTER | DT_VCENTER | DT_SINGLELINE)
        draw_text(hdc, "•••", (1204, area[1] + 4, 1239, area[3] - 3), FONT_TAB, COLOR_MUTED, DT_CENTER | DT_VCENTER | DT_SINGLELINE)

    filtered_count = len(filtered_reply_entries())
    if filtered_count > reply_visible_count():
        draw_text(hdc, f"{reply_scroll_offset + 1}–{reply_scroll_offset + len(visible_reply_entries())} / {filtered_count}", (1050, 605, 1240, 627), FONT_TINY, COLOR_MUTED, DT_RIGHT | DT_VCENTER | DT_SINGLELINE)


def draw_button(hdc, area, text, color, text_color=rgb(255, 255, 255), font=FONT_BODY_BOLD):
    fill_round_rect(hdc, area, color, 22)
    draw_text(
        hdc,
        text,
        area,
        font,
        text_color,
        DT_CENTER | DT_VCENTER | DT_SINGLELINE,
    )


def draw_favorite_streamers(hdc):
    card = favorite_card_rect()
    fill_round_rect(hdc, card, COLOR_CARD, 10)
    stroke_round_rect(hdc, card, rgb(56, 70, 87), 10, 1)
    draw_text(hdc, "★  常用直播主", (32, card[1] + 8, 260, card[1] + 36), FONT_HEADING, COLOR_TEXT, DT_LEFT | DT_VCENTER | DT_SINGLELINE)
    draw_text(hdc, "最近輸入的 3 位直播主", (194, card[1] + 13, 470, card[1] + 33), FONT_TINY, COLOR_MUTED, DT_LEFT | DT_VCENTER | DT_SINGLELINE)
    fill_rect(hdc, (32, card[1] + 47, 564, card[1] + 74), rgb(29, 39, 52))
    draw_text(hdc, "直播主名稱", (70, card[1] + 48, 186, card[1] + 73), FONT_TINY, COLOR_MUTED, DT_LEFT | DT_VCENTER | DT_SINGLELINE)
    draw_text(hdc, "Facebook 個人主頁", (192, card[1] + 48, 422, card[1] + 73), FONT_TINY, COLOR_MUTED, DT_LEFT | DT_VCENTER | DT_SINGLELINE)
    draw_text(hdc, "操作", (440, card[1] + 48, 562, card[1] + 73), FONT_TINY, COLOR_MUTED, DT_CENTER | DT_VCENTER | DT_SINGLELINE)
    with favorite_lock:
        entries = list(favorite_streamers)
    if not entries:
        draw_text(hdc, "輸入直播網址後，會自動加入最近的直播主。", (44, card[1] + 87, 552, card[3] - 10), FONT_SMALL, COLOR_MUTED, DT_CENTER | DT_VCENTER | DT_SINGLELINE)
    for index, item in enumerate(entries):
        row = favorite_row_rect(index)
        fill_round_rect(hdc, row, rgb(27, 37, 49), 7)
        stroke_round_rect(hdc, row, rgb(49, 62, 78), 7, 1)
        fill_ellipse(hdc, (40, row[1] + 6, 64, row[1] + 30), rgb(68, 93, 122))
        draw_text(hdc, item["name"][:1], (40, row[1] + 6, 64, row[1] + 30), FONT_TINY, COLOR_TEXT, DT_CENTER | DT_VCENTER | DT_SINGLELINE)
        draw_text(hdc, item["name"], (70, row[1] + 2, 188, row[3] - 2), FONT_SMALL, COLOR_TEXT, DT_LEFT | DT_VCENTER | DT_SINGLELINE | DT_END_ELLIPSIS)
        draw_text(hdc, item["url"], (192, row[1] + 2, 430, row[3] - 2), FONT_TINY, COLOR_MUTED, DT_LEFT | DT_VCENTER | DT_SINGLELINE | DT_END_ELLIPSIS)
        fill_round_rect(hdc, favorite_go_rect(index), rgb(43, 59, 78), 7)
        draw_text(hdc, "前往", favorite_go_rect(index), FONT_TINY, COLOR_TEXT, DT_CENTER | DT_VCENTER | DT_SINGLELINE)
        fill_round_rect(hdc, favorite_delete_rect(index), rgb(58, 38, 49), 7)
        draw_text(hdc, "刪除", favorite_delete_rect(index), FONT_TINY, rgb(239, 113, 131), DT_CENTER | DT_VCENTER | DT_SINGLELINE)


def paint_window(hwnd):
    update_client_size(hwnd)
    ps = PAINTSTRUCT()
    hdc = user32.BeginPaint(hwnd, ctypes.byref(ps))
    try:
        fill_raw_rect(hdc, (0, 0, client_width, client_height), COLOR_BG)
        draw_logo(hdc)

        extra = account_field_extra_y()
        robot_card = (16, 82, 580, 286 + extra)
        source_card = source_card_rect()
        phrase_card = (596, 82, 1264, 188)
        right_panel = (596, 198, 1264, max(704, favorite_card_rect()[3] + 4))
        for area in (robot_card, source_card, phrase_card, right_panel):
            fill_round_rect(hdc, area, COLOR_CARD, 10)
            stroke_round_rect(hdc, area, rgb(56, 70, 87), 10, 1)

        draw_text(hdc, "▣  機器人管理", (32, 96, 240, 129), FONT_HEADING, COLOR_TEXT, DT_LEFT | DT_VCENTER | DT_SINGLELINE)
        draw_text(hdc, "選擇要使用的機器人帳號", (195, 99, 456, 128), FONT_SMALL, COLOR_MUTED, DT_LEFT | DT_VCENTER | DT_SINGLELINE)
        draw_profile_tabs(hdc)

        draw_text(hdc, "↗  直播來源管理", below_accounts_area((32, 309, 290, 340)), FONT_HEADING, COLOR_TEXT, DT_LEFT | DT_VCENTER | DT_SINGLELINE)
        draw_text(hdc, "管理多個 Facebook 直播網址", below_accounts_area((69, 336, 398, 355)), FONT_SMALL, COLOR_MUTED, DT_LEFT | DT_VCENTER | DT_SINGLELINE)
        for index in range(url_field_count):
            area = url_box_rect(index)
            fill_round_rect(hdc, area, rgb(30, 40, 53) if index == active_reply_stream_index else rgb(26, 35, 47), 8)
            stroke_round_rect(hdc, area, COLOR_GREEN if index == active_reply_stream_index else rgb(66, 79, 96), 8, 1)
            draw_checkbox(hdc, (area[0] + 12, area[1] + 17, area[0] + 26, area[1] + 31), index == active_reply_stream_index)
            draw_text(hdc, f"直播 {index + 1} ·", (area[0] + 38, area[1] + 7, area[0] + 91, area[1] + 29), FONT_SMALL, COLOR_TEXT, DT_LEFT | DT_VCENTER | DT_SINGLELINE)
            if url_originals[index]:
                draw_text(hdc, url_originals[index], (area[0] + 39, area[1] + 27, area[0] + 300, area[3] - 3), FONT_TINY, COLOR_MUTED, DT_LEFT | DT_VCENTER | DT_SINGLELINE | DT_END_ELLIPSIS)
            state = url_states[index]
            state_color = COLOR_GREEN if state == "running" else rgb(233, 185, 72) if state == "paused" else rgb(132, 146, 165)
            fill_ellipse(hdc, url_status_rect(index), state_color)
            draw_text(hdc, "運行中" if state == "running" else "已暫停" if state == "paused" else "已停止", (366, area[1] + 10, 427, area[3] - 10), FONT_TINY, state_color, DT_LEFT | DT_VCENTER | DT_SINGLELINE)
            draw_button(hdc, url_start_rect(index), {"idle": "▶ 開始", "running": "暫停", "paused": "繼續"}.get(state, "開始"), COLOR_GREEN if state != "running" else COLOR_GRAY_BUTTON, font=FONT_SMALL)
            fill_round_rect(hdc, url_stop_rect(index), rgb(51, 35, 48), 8)
            stroke_round_rect(hdc, url_stop_rect(index), COLOR_DANGER, 8, 1)
            draw_text(hdc, "■ 停止", url_stop_rect(index), FONT_SMALL, rgb(240, 106, 117), DT_CENTER | DT_VCENTER | DT_SINGLELINE)
        if url_field_count < len(url_controls):
            fill_round_rect(hdc, current_add_url_rect(), rgb(23, 32, 43), 8)
            stroke_round_rect(hdc, current_add_url_rect(), rgb(78, 92, 110), 8, 1)
            draw_text(hdc, "＋  新增網址列", current_add_url_rect(), FONT_TAB, COLOR_TEXT, DT_CENTER | DT_VCENTER | DT_SINGLELINE)

        draw_favorite_streamers(hdc)

        if reply_filter == "default":
            draw_text(hdc, "▣  預設語句（唯讀）", (612, 92, 930, 123), FONT_HEADING, COLOR_TEXT, DT_LEFT | DT_VCENTER | DT_SINGLELINE)
            draw_text(hdc, "可勾選是否提供給手動與自動模式", (643, 118, 1085, 135), FONT_SMALL, COLOR_MUTED, DT_LEFT | DT_VCENTER | DT_SINGLELINE)
        else:
            phrase_title = "衝人氣語句" if reply_filter == "crowd" else "自訂語句"
            draw_text(hdc, f"▣  新增{phrase_title}", (612, 92, 930, 123), FONT_HEADING, COLOR_TEXT, DT_LEFT | DT_VCENTER | DT_SINGLELINE)
            draw_text(hdc, f"輸入新的{phrase_title}並建立", (643, 118, 1030, 135), FONT_SMALL, COLOR_MUTED, DT_LEFT | DT_VCENTER | DT_SINGLELINE)
            fill_round_rect(hdc, (612, 136, 1098, 174), COLOR_CARD_2, 8)
            stroke_round_rect(hdc, (612, 136, 1098, 174), rgb(73, 87, 105), 8, 1)
            draw_button(hdc, current_save_phrase_rect(), "儲存修改" if editing_reply_id else "＋  新增語句", COLOR_GREEN)

        draw_text(hdc, f"管理語句 · 直播 {active_reply_stream_index + 1}  ⌄", REPLY_HEADING_RECT, FONT_TAB, COLOR_TEXT, DT_LEFT | DT_VCENTER | DT_SINGLELINE)
        draw_line(hdc, 783, 216, 783, 255, rgb(63, 76, 93), 1)
        draw_text(hdc, "語句分類", (793, 220, 864, 250), FONT_SMALL, COLOR_TEXT, DT_CENTER | DT_VCENTER | DT_SINGLELINE)
        draw_button(
            hdc, REPLY_DEFAULT_TAB_RECT, "預設",
            COLOR_GREEN if reply_filter == "default" else COLOR_GRAY_BUTTON,
            COLOR_TEXT,
        )
        draw_button(
            hdc, REPLY_USER_TAB_RECT, "自訂",
            COLOR_GREEN if reply_filter == "user" else COLOR_GRAY_BUTTON,
            COLOR_TEXT,
        )
        draw_button(
            hdc, REPLY_CROWD_TAB_RECT, "衝人氣",
            COLOR_GREEN if reply_filter == "crowd" else COLOR_GRAY_BUTTON,
            COLOR_TEXT,
        )
        draw_line(hdc, 1055, 216, 1055, 255, rgb(63, 76, 93), 1)
        draw_text(hdc, "回覆模式", (1064, 201, 1248, 223), FONT_TINY, COLOR_TEXT, DT_CENTER | DT_VCENTER | DT_SINGLELINE)
        draw_button(
            hdc, REPLY_MANUAL_TAB_RECT, "手動",
            COLOR_GREEN if pending_reply_mode == "manual" else COLOR_GRAY_BUTTON,
            COLOR_TEXT,
        )
        draw_button(
            hdc, REPLY_AUTO_TAB_RECT, "自動",
            COLOR_GREEN if pending_reply_mode == "auto" else COLOR_GRAY_BUTTON,
            COLOR_TEXT,
        )
        draw_reply_library(hdc)
        draw_button(
            hdc,
            REFRESH_RECT,
            "✓  套用",
            COLOR_GREEN if mode_selection_dirty or reply_selection_dirty else COLOR_GRAY_BUTTON,
            COLOR_TEXT,
        )
        if reply_filter != "default":
            draw_button(hdc, DELETE_RECT, "▣  刪除選取", COLOR_DANGER, COLOR_TEXT)
        status_top = max(704 + extra, favorite_card_rect()[3] + 8)
        draw_text(hdc, status_text, (32, status_top, 1248, status_top + 16), FONT_TINY, COLOR_MUTED, DT_CENTER | DT_VCENTER | DT_SINGLELINE | DT_END_ELLIPSIS)
    finally:
        user32.EndPaint(hwnd, ctypes.byref(ps))


def show_message(title, text, error=False):
    flags = 0x10 if error else 0x40
    user32.MessageBoxW(hwnd_main, text, title, flags)


def set_busy(value):
    global is_busy
    is_busy = value
    for control in (*url_controls, hwnd_channel):
        set_control_enabled(control, not value)
    set_control_enabled(hwnd_phrase, not value)
    user32.InvalidateRect(hwnd_main, None, True)


def set_process_suspended(proc, suspended):
    if not process_running(proc) or os.name != "nt":
        return
    access = 0x0800
    handle = kernel32.OpenProcess(access, False, proc.pid)
    if not handle:
        return
    try:
        ntdll = ctypes.windll.ntdll
        if suspended:
            ntdll.NtSuspendProcess(handle)
        else:
            ntdll.NtResumeProcess(handle)
    finally:
        kernel32.CloseHandle(handle)


def get_chrome_windows():
    windows = []
    enum_proc_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
    )

    @enum_proc_type
    def enum_proc(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        class_name = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_name, len(class_name))
        if class_name.value.startswith("Chrome_WidgetWin"):
            windows.append(hwnd)
        return True

    user32.EnumWindows(enum_proc, 0)
    return windows


def terminate_process_tree(proc):
    if not process_running(proc):
        return
    try:
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass


def terminate_processes(items):
    for proc in items:
        terminate_process_tree(proc)


def stop_llm_targets(targets):
    with service_start_lock:
        for key in list(targets):
            proc = llm_processes.pop(key, None)
            if proc is None:
                continue
            terminate_process_tree(proc)
            if proc in processes:
                processes.remove(proc)


def close_browser_resources(resources):
    for resource in resources:
        if not isinstance(resource, dict):
            terminate_process_tree(resource)
            continue
        for hwnd in resource.get("windows", []):
            try:
                if user32.IsWindow(hwnd):
                    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            except Exception:
                pass
        proc = resource.get("process")
        # Chrome 若把網址交給既有程序，這個 proc 會自行結束；不強制
        # taskkill，避免誤關使用者其他 Chrome 視窗。
        if proc and process_running(proc) and resource.get("windows"):
            deadline = time.time() + 3
            while time.time() < deadline:
                if all(
                    not user32.IsWindow(hwnd)
                    for hwnd in resource.get("windows", [])
                ):
                    break
                time.sleep(0.1)


def stop_clicked():
    global pipeline_state
    if pipeline_state == "idle":
        stop_llm_targets(key for key in llm_processes if key[1] == 0)
        return
    for proc in url_processes[0]:
        try:
            if process_running(proc):
                if pipeline_state == "paused":
                    set_process_suspended(proc, False)
                terminate_process_tree(proc)
        except Exception:
            pass
    url_processes[0] = []
    close_browser_resources(url_browser_processes[0])
    url_browser_processes[0] = []
    stop_llm_targets(url_llm_keys[0])
    stop_llm_targets(key for key in llm_processes if key[1] == 0)
    url_llm_keys[0].clear()
    pipeline_state = "idle"
    starting_stream_indexes.discard(0)
    if url_originals[0]:
        user32.SetWindowTextW(hwnd_url, url_originals[0])
    user32.SendMessageW(hwnd_url, EM_SETREADONLY, False, 0)
    set_status("共用直播流程已完全停止，網址已恢復。")
    user32.InvalidateRect(hwnd_main, None, True)


def start_url_row(index):
    global active_reply_stream_index
    if index >= url_field_count or is_busy:
        return
    active_reply_stream_index = index
    if url_states[index] == "running":
        for proc in url_processes[index]:
            set_process_suspended(proc, True)
        url_states[index] = "paused"
        set_status(f"第 {index + 1} 列已暫停（黃燈）；按「繼續」接續 STT／LLM。")
        user32.InvalidateRect(hwnd_main, None, True)
        return
    if url_states[index] == "paused":
        for proc in url_processes[index]:
            set_process_suspended(proc, False)
        url_states[index] = "running"
        set_status(f"第 {index + 1} 列已繼續執行（綠燈）。")
        user32.InvalidateRect(hwnd_main, None, True)
        return

    url = get_control_text(url_controls[index])
    url = url.strip().strip("\"'")
    if not url.startswith(("http://", "https://")):
        show_message("網址錯誤", f"請在第 {index + 1} 列輸入有效的 Facebook 直播網址。", error=True)
        return
    schedule_favorite_probe(index, url)
    url_originals[index] = url
    url_start_tokens[index] += 1
    start_token = url_start_tokens[index]
    starting_stream_indexes.add(index)

    def worker():
        if start_token != url_start_tokens[index]:
            starting_stream_indexes.discard(index)
            return
        set_busy(True)
        set_status(f"正在啟動第 {index + 1} 列直播...")
        try:
            profile_names = [name for name in PROFILES if name in selected_profiles]
            if not profile_names:
                raise RuntimeError("請至少選擇一個機器人帳號。")
            url_stream_ids[index] = f"{PROFILE_ACCOUNTS[profile_names[0]]}_live_{index + 1:02d}"
            tasks = [
                {
                    "profile_name": profile_name,
                    "url": url,
                    "channel_name": "",
                    "stream_index": index + 1,
                }
                for profile_name in profile_names
            ]
            workers, browsers, targets = start_pipeline(
                tasks, cancelled=lambda: start_token != url_start_tokens[index]
            )
            if start_token != url_start_tokens[index]:
                close_browser_resources(browsers)
                terminate_processes(workers)
                stop_llm_targets(targets)
                raise InterruptedError("直播啟動已取消")
            url_processes[index] = workers
            url_browser_processes[index] = browsers
            url_llm_keys[index] = set(targets)
            url_active_profiles[index] = set(profile_names)
            url_states[index] = "running"
            set_status(f"第 {index + 1} 列已啟動，共套用 {len(profile_names)} 個帳號。")
        except InterruptedError:
            url_states[index] = "idle"
            url_active_profiles[index].clear()
            url_stream_ids[index] = ""
            user32.SetWindowTextW(url_controls[index], url)
            user32.SendMessageW(url_controls[index], EM_SETREADONLY, False, 0)
            set_status(f"直播 {index + 1} 已停止")
        except Exception as exc:
            url_states[index] = "idle"
            url_active_profiles[index].clear()
            url_stream_ids[index] = ""
            user32.SetWindowTextW(url_controls[index], url)
            user32.SendMessageW(url_controls[index], EM_SETREADONLY, False, 0)
            if start_token != url_start_tokens[index]:
                set_status(f"直播 {index + 1} 已停止")
            else:
                show_message("啟動失敗", str(exc), error=True)
                set_status(f"直播 {index + 1} 啟動失敗")
        finally:
            starting_stream_indexes.discard(index)
            set_busy(False)

    threading.Thread(target=worker, daemon=True).start()


def stop_url_row(index):
    if index >= url_field_count:
        return
    if url_states[index] == "idle":
        if index in starting_stream_indexes:
            url_start_tokens[index] += 1
        stop_llm_targets(key for key in llm_processes if key[1] == index)
        return
    for proc in url_processes[index]:
        try:
            if process_running(proc):
                if url_states[index] == "paused":
                    set_process_suspended(proc, False)
                terminate_process_tree(proc)
        except Exception:
            pass
    url_processes[index] = []
    close_browser_resources(url_browser_processes[index])
    url_browser_processes[index] = []
    stop_llm_targets(key for key in llm_processes if key[1] == index)
    url_llm_keys[index].clear()
    url_active_profiles[index].clear()
    url_stream_ids[index] = ""
    url_states[index] = "idle"
    starting_stream_indexes.discard(index)
    if url_originals[index]:
        user32.SetWindowTextW(url_controls[index], url_originals[index])
    user32.SendMessageW(url_controls[index], EM_SETREADONLY, False, 0)
    set_status(f"第 {index + 1} 列已完全停止（紅燈），網址已恢復；可再次按「開始」。")
    user32.InvalidateRect(hwnd_main, None, True)


def set_reply_enabled(item, enabled):
    global reply_selection_dirty
    if item.get("source") == "crowd":
        data = request_json(f"{DB_API_BASE}/crowd_slogans/{item['id']}", {"enabled": enabled}, method="PATCH")
        if data.get("status") != "success":
            raise RuntimeError(data.get("error", "衝人氣語句更新失敗"))
        item["enabled"] = enabled
        reply_selection_dirty = True
        if hwnd_main:
            user32.InvalidateRect(hwnd_main, None, True)
        return
    data = request_json(
        f"{current_llm_base()}/stream_replies",
        {
            "stream_id": item.get("stream_id") or current_reply_stream_id(),
            "reply_key": item.get("reply_key"),
            "enabled": enabled,
        },
        method="PATCH",
    )
    if data.get("status") != "success":
        raise RuntimeError(data.get("error", "更新語句狀態失敗"))
    item["enabled"] = enabled
    reply_selection_dirty = True
    if hwnd_main:
        user32.InvalidateRect(hwnd_main, None, True)


def reply_list_clicked(x, y):
    global selected_reply_index, editing_reply_id
    for index, (actual_index, item) in enumerate(visible_reply_entries()):
        if point_in_rect(x, y, reply_row_rect(index)):
            selected_reply_index = actual_index
            if item.get("source") != "default" and point_in_rect(x, y, (1170, reply_row_rect(index)[1], 1204, reply_row_rect(index)[3])):
                editing_reply_id = (item.get("source"), item.get("id"))
                user32.SetWindowTextW(hwnd_phrase, item.get("text", ""))
                if hwnd_main:
                    user32.InvalidateRect(hwnd_main, None, True)
                return True
            if point_in_rect(x, y, (reply_row_rect(index)[0] + 14, reply_row_rect(index)[1], reply_row_rect(index)[0] + 38, reply_row_rect(index)[3])):
                def worker():
                    try:
                        set_reply_enabled(item, not bool(item.get("enabled", True)))
                        if hwnd_main:
                            user32.InvalidateRect(hwnd_main, None, True)
                    except Exception as exc:
                        set_status("更新語句狀態失敗。")
                        show_message("更新失敗", str(exc), error=True)

                threading.Thread(target=worker, daemon=True).start()
            elif hwnd_main:
                user32.InvalidateRect(hwnd_main, None, True)
            return True
    return False


def scroll_reply_list(delta):
    global reply_scroll_offset
    max_offset = max(0, len(filtered_reply_entries()) - reply_visible_count())
    if max_offset == 0:
        return False
    step = -1 if delta > 0 else 1
    next_offset = max(0, min(max_offset, reply_scroll_offset + step))
    if next_offset == reply_scroll_offset:
        return False
    reply_scroll_offset = next_offset
    if hwnd_main:
        user32.InvalidateRect(hwnd_main, None, True)
    return True


def apply_clicked():
    def worker():
        global mode_selection_dirty, reply_selection_dirty
        target_mode = pending_reply_mode
        target_stream_id = current_reply_stream_id()
        set_status("正在套用模式與語句設定...")
        try:
            with reply_mode_service_lock:
                ensure_llm_ready()
                mode_changed = target_mode != stream_reply_mode
                if mode_changed:
                    set_stream_reply_mode(target_mode, target_stream_id)
                if target_stream_id != current_reply_stream_id():
                    return
                reload_llm_replies()
                mode_selection_dirty = pending_reply_mode != target_mode
                refresh_reply_list(organize=True)
            reply_selection_dirty = False
            set_status("已套用模式設定。")
            if hwnd_main:
                user32.InvalidateRect(hwnd_main, None, True)
        except Exception as exc:
            set_status("套用模式失敗。")
            show_message("套用失敗", str(exc), error=True)

    threading.Thread(target=worker, daemon=True).start()


def save_phrase_clicked():
    global editing_reply_id
    phrase = get_control_text(hwnd_phrase)
    if not phrase:
        show_message("缺少語句", "請先輸入要新增的語句。", error=True)
        return

    def worker():
        global editing_reply_id
        set_status("正在新增衝人氣口號..." if reply_filter == "crowd" else "正在新增自訂留言語句...")
        try:
            ensure_llm_ready()
            edit_target = editing_reply_id
            if edit_target:
                source, item_id = edit_target
                endpoint = "crowd_slogans" if source == "crowd" else "user_input"
                data = request_json(f"{DB_API_BASE}/{endpoint}/{item_id}", {"new_text": phrase}, method="PATCH")
            elif reply_filter == "crowd":
                data = request_json(f"{DB_API_BASE}/crowd_slogans", {"text": phrase, "enabled": True, "meaning": "", "response_mode": "same"}, method="POST")
            else:
                data = request_json(f"{DB_API_BASE}/user_input", {"text": phrase, "weight": 1.0, "enabled": True}, method="POST")
            if data.get("status") != "success":
                raise RuntimeError(data.get("error", "儲存失敗"))
            if not edit_target and reply_filter != "crowd" and data.get("item", {}).get("id"):
                selection = request_json(
                    f"{current_llm_base()}/stream_replies",
                    {
                        "stream_id": current_reply_stream_id(),
                        "reply_key": f"user:{data['item']['id']}",
                        "enabled": True,
                    },
                    method="PATCH",
                )
                if selection.get("status") != "success":
                    raise RuntimeError(selection.get("error", "無法勾選新語句"))
            reload_llm_replies()
            refresh_reply_list()
            editing_reply_id = None
            user32.SetWindowTextW(hwnd_phrase, "")
            set_status(f"已{'更新' if edit_target else '新增'}語句：{phrase}")
            if hwnd_main:
                user32.InvalidateRect(hwnd_main, None, True)
        except Exception as exc:
            set_status("儲存失敗。")
            show_message("儲存失敗", str(exc), error=True)

    threading.Thread(target=worker, daemon=True).start()


def delete_selected_clicked():
    selected = selected_reply_index
    if selected < 0 or selected >= len(reply_items):
        show_message("尚未選取", "請先在右側清單選取要刪除的語句。", error=True)
        return

    item = reply_items[selected]
    if item.get("source") == "default":
        show_message("無法刪除", "預設六個語句不能刪除；可以取消勾選停用。", error=True)
        return

    text = item.get("text", "")
    if not text:
        return

    def worker():
        set_status("正在刪除選取語句...")
        try:
            ensure_llm_ready()
            endpoint = "crowd_slogans" if item.get("source") == "crowd" else "user_input"
            data = request_json(f"{DB_API_BASE}/{endpoint}/by_text", {"text": text}, method="DELETE")
            if data.get("status") != "success":
                raise RuntimeError(data.get("error", "刪除失敗"))
            refresh_reply_list()
            set_status(f"已刪除：{text}")
        except Exception as exc:
            set_status("刪除失敗。")
            show_message("刪除失敗", str(exc), error=True)

    threading.Thread(target=worker, daemon=True).start()


def start_clicked():
    global pipeline_state
    if is_busy:
        return
    if pipeline_state == "running":
        for proc in url_processes[0]:
            set_process_suspended(proc, True)
        pipeline_state = "paused"
        set_status("共用直播流程已暫停；按「繼續」接續執行。")
        user32.InvalidateRect(hwnd_main, None, True)
        return
    if pipeline_state == "paused":
        for proc in processes:
            set_process_suspended(proc, False)
        pipeline_state = "running"
        set_status("流程已繼續執行（綠燈）。")
        user32.InvalidateRect(hwnd_main, None, True)
        return

    primary_url = get_control_text(hwnd_url)
    tasks = []
    if run_mode == "one_to_many":
        urls = [get_control_text(control) for control in url_controls[:url_field_count]]
        tasks = [
            {"profile_name": selected_profile, "url": url, "channel_name": "", "stream_index": index + 1}
            for index, url in enumerate(urls)
            if url
        ]
    elif run_mode == "many_to_one":
        tasks = [
            {"profile_name": profile_name, "url": primary_url, "channel_name": ""}
            for profile_name in PROFILES
            if profile_name in selected_profiles
        ]
    else:
        tasks = [{"profile_name": selected_profile, "url": primary_url, "channel_name": ""}]

    for index, task in enumerate(tasks):
        schedule_favorite_probe(min(index, MAX_URL_FIELDS - 1), task["url"])
    url_originals[0] = primary_url
    starting_stream_indexes.add(0)

    def worker():
        global pipeline_state
        set_busy(True)
        set_status("正在啟動 LLM、STT 與 Chrome...")
        try:
            workers, browsers, targets = start_pipeline(tasks)
            url_processes[0] = workers
            url_browser_processes[0] = browsers
            url_llm_keys[0] = set(targets)
            pipeline_state = "running"
            set_status(f"流程已啟動，共 {len(tasks)} 組帳號/直播任務，等待 LLM 產生留言。")
            show_message("啟動完成", f"已啟動 {len(tasks)} 組 LLM、STT 與 Chrome 任務，請確認 Chrome extension 已開啟。")
        except Exception as exc:
            set_status("啟動失敗，請查看錯誤訊息。")
            show_message("啟動失敗", str(exc), error=True)
        finally:
            starting_stream_indexes.discard(0)
            set_busy(False)

    threading.Thread(target=worker, daemon=True).start()


@WNDPROC
def wnd_proc(hwnd, msg, wparam, lparam):
    global stop_monitor

    if msg == WM_PAINT:
        paint_window(hwnd)
        return 0

    if msg == WM_GETMINMAXINFO:
        info = ctypes.cast(lparam, ctypes.POINTER(MINMAXINFO)).contents
        info.ptMinTrackSize.x = 976
        info.ptMinTrackSize.y = 579
        return 0

    if msg == WM_SIZE:
        update_client_size(hwnd)
        apply_mode_layout()
        return 0

    if msg in (WM_CTLCOLOREDIT, WM_CTLCOLORSTATIC):
        gdi32.SetBkColor(wparam, COLOR_CARD_2)
        gdi32.SetTextColor(wparam, COLOR_TEXT)
        return EDIT_BRUSH

    if msg == WM_MOUSEWHEEL:
        delta = ctypes.c_short((wparam >> 16) & 0xFFFF).value
        point = wintypes.POINT(
            ctypes.c_short(lparam & 0xFFFF).value,
            ctypes.c_short((lparam >> 16) & 0xFFFF).value,
        )
        user32.ScreenToClient(hwnd, ctypes.byref(point))
        if point_in_rect(point.x, point.y, reply_list_inner_rect()) and scroll_reply_list(delta):
            return 0

    if msg == WM_LBUTTONUP:
        x = lparam & 0xFFFF
        y = (lparam >> 16) & 0xFFFF
        if point_in_rect(x, y, REPLY_DEFAULT_TAB_RECT):
            show_phrase_category("default")
            return 0
        if point_in_rect(x, y, REPLY_USER_TAB_RECT):
            show_phrase_category("user")
            return 0
        if point_in_rect(x, y, REPLY_CROWD_TAB_RECT):
            show_phrase_category("crowd")
            return 0
        if point_in_rect(x, y, REPLY_MANUAL_TAB_RECT):
            toggle_reply_option("manual")
            return 0
        if point_in_rect(x, y, REPLY_AUTO_TAB_RECT):
            toggle_reply_option("auto")
            return 0
        if run_mode == "one_to_many":
            for index in range(url_field_count):
                if point_in_rect(x, y, url_start_rect(index)):
                    start_url_row(index)
                    return 0
                if point_in_rect(x, y, url_stop_rect(index)):
                    stop_url_row(index)
                    return 0
                if point_in_rect(x, y, url_box_rect(index)):
                    select_reply_stream(index)
                    return 0
        if run_mode != "one_to_many" and point_in_rect(x, y, current_start_rect()):
            start_clicked()
            return 0
        if run_mode != "one_to_many" and point_in_rect(x, y, current_stop_rect()):
            stop_clicked()
            return 0
        if run_mode == "one_to_many" and url_field_count < len(url_controls) and point_in_rect(x, y, current_add_url_rect()):
            add_url_field()
            return 0
        with favorite_lock:
            favorite_count = len(favorite_streamers)
        for index in range(favorite_count):
            if point_in_rect(x, y, favorite_go_rect(index)):
                open_favorite_streamer(index)
                return 0
            if point_in_rect(x, y, favorite_delete_rect(index)):
                delete_favorite_streamer(index)
                return 0
        if len(PROFILES) < MAX_ACCOUNTS and point_in_rect(x, y, current_add_account_rect()):
            add_account_profile()
            return 0
        if reply_filter != "default" and point_in_rect(x, y, current_save_phrase_rect()):
            save_phrase_clicked()
            return 0
        if point_in_rect(x, y, REFRESH_RECT):
            apply_clicked()
            return 0
        if reply_filter != "default" and point_in_rect(x, y, DELETE_RECT):
            delete_selected_clicked()
            return 0
        if reply_list_clicked(x, y):
            return 0
        for profile_name, area in profile_rects().items():
            if point_in_rect(x, y, profile_delete_rect(area)):
                remove_account_profile(profile_name)
                return 0
            if point_in_rect(x, y, profile_avatar_rect(area)) or point_in_rect(x, y, profile_name_rect(area)):
                start_profile_name_edit(profile_name)
                return 0
            if point_in_rect(x, y, area):
                if editing_profile_name:
                    finish_profile_name_edit(False)
                set_profile_selection(profile_name)
                return 0

    if msg == WM_COMMAND:
        control_id = wparam & 0xFFFF
        notification = (wparam >> 16) & 0xFFFF
        if notification == EN_KILLFOCUS and ID_URL <= control_id < ID_URL + MAX_URL_FIELDS:
            schedule_favorite_probe(control_id - ID_URL)
        if control_id == ID_PROFILE_NAME_SAVE and notification == 0:
            finish_profile_name_edit(True)
            return 0
        if notification == EN_SETFOCUS and ID_URL <= control_id < ID_URL + MAX_URL_FIELDS:
            select_reply_stream(control_id - ID_URL)
        return 0

    if msg == WM_DESTROY:
        stop_monitor = True
        if profile_bridge:
            profile_bridge.shutdown()
            profile_bridge.server_close()
        for resources in url_browser_processes:
            close_browser_resources(resources)
        terminate_processes(processes)
        for _stamp, image_handle in PROFILE_IMAGE_CACHE.values():
            gdiplus.GdipDisposeImage(image_handle)
        PROFILE_IMAGE_CACHE.clear()
        if AUTO_IMAGE_HANDLE:
            gdiplus.GdipDisposeImage(AUTO_IMAGE_HANDLE)
        if GDIPLUS_READY:
            gdiplus.GdiplusShutdown(GDIPLUS_TOKEN)
        user32.PostQuitMessage(0)
        return 0

    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def create_child(class_name, text, style, x, y, width, height, control_id):
    return user32.CreateWindowExW(
        0,
        class_name,
        text,
        WS_CHILD | WS_VISIBLE | style,
        x,
        y,
        width,
        height,
        hwnd_main,
        control_id,
        hinst,
        None,
    )


hinst = kernel32.GetModuleHandleW(None)
class_name = "FBLiveAutoCommentWindow"

wc = WNDCLASS()
wc.style = CS_HREDRAW | CS_VREDRAW
wc.lpfnWndProc = wnd_proc
wc.cbClsExtra = 0
wc.cbWndExtra = 0
wc.hInstance = hinst
wc.hIcon = user32.LoadIconW(None, 32512)
wc.hCursor = user32.LoadCursorW(None, 32512)
wc.hbrBackground = gdi32.CreateSolidBrush(COLOR_BG)
wc.lpszMenuName = None
wc.lpszClassName = class_name
user32.RegisterClassW(ctypes.byref(wc))

hwnd_main = user32.CreateWindowExW(
    0,
    class_name,
    "FB 直播自動留言系統",
    WS_OVERLAPPEDWINDOW,
    CW_USEDEFAULT,
    CW_USEDEFAULT,
    WIDTH + 16,
    HEIGHT + 39,
    None,
    None,
    hinst,
    None,
)

hwnd_profile_name_edit = create_child(
    "EDIT", "", WS_BORDER | ES_AUTOHSCROLL | WS_TABSTOP, 0, 0, 82, 22, ID_PROFILE_NAME_EDIT
)
hwnd_profile_name_save = create_child(
    "BUTTON", "✓", WS_TABSTOP, 0, 0, 30, 22, ID_PROFILE_NAME_SAVE
)
set_control_font(hwnd_profile_name_edit, FONT_TINY)
set_control_font(hwnd_profile_name_save, FONT_TINY)
set_input_placeholder(hwnd_profile_name_edit, "使用者名稱")
show_control(hwnd_profile_name_edit, False)
show_control(hwnd_profile_name_save, False)

for index in range(MAX_URL_FIELDS):
    control = create_child("EDIT", "", ES_AUTOHSCROLL | WS_TABSTOP, 128, url_input_y(index), 292, 20, ID_URL + index)
    set_control_font(control, FONT_BODY)
    set_input_placeholder(control, "請填入直播網址")
    url_controls.append(control)

hwnd_url = url_controls[0]
hwnd_url_2 = url_controls[1]
hwnd_url_3 = url_controls[2]

hwnd_phrase = create_child("EDIT", "", ES_AUTOHSCROLL | WS_TABSTOP, 128, 479, 292, 20, ID_PHRASE)
set_control_font(hwnd_phrase, FONT_BODY)
set_input_placeholder(hwnd_phrase, "請填入語句")

hwnd_channel = create_child("EDIT", "", ES_AUTOHSCROLL | ES_READONLY, 128, 452, 292, 20, ID_CHANNEL)
set_control_font(hwnd_channel, FONT_BODY)
set_input_placeholder(hwnd_channel, "正在偵測直播名稱")

hwnd_reply_list = create_child("LISTBOX", "", LBS_NOTIFY | WS_BORDER | WS_VSCROLL | WS_TABSTOP, 526, 374, 278, 178, ID_REPLY_LIST)
set_control_font(hwnd_reply_list, FONT_SMALL)

apply_mode_layout()

threading.Thread(target=monitor_reply_file, daemon=True).start()
threading.Thread(target=monitor_stream_titles, daemon=True).start()

user32.ShowWindow(hwnd_main, SW_SHOW)
user32.UpdateWindow(hwnd_main)

msg = wintypes.MSG()
while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
    user32.TranslateMessage(ctypes.byref(msg))
    user32.DispatchMessageW(ctypes.byref(msg))
