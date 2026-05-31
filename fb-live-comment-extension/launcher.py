import ctypes
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path
from urllib.request import urlopen


CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

PROFILES = {
    "帳號 A": "Default",
    "帳號 B": "Profile 1",
    "帳號 C": "Profile 2",
}

PROFILE_LABELS = {
    "帳號 A": "Default",
    "帳號 B": "Profile 1",
    "帳號 C": "Profile 2",
}

PROFILE_AVATARS = {
    "Default": ("A", 0xE8A14A),
    "Profile 1": ("碩軒", 0x1ED760),
    "Profile 2": ("C", 0xB65A7A),
}


def get_base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BASE_DIR = get_base_dir()
PROJECT_DIR = BASE_DIR.parent
SHARED_TEXT_FILE = BASE_DIR / "Text.jsonl"
STT_EXE = BASE_DIR / "stt_worker.exe"
LLM_EXE = BASE_DIR / "llm_server.exe"
STT_SCRIPT = PROJECT_DIR / "stt" / "WASAPI_test.py"
LLM_SCRIPT = PROJECT_DIR / "test_LLM" / "test_llm_4" / "rag_chat.py"

processes = []


def run_process(command, cwd, env=None, new_console=True):
    flags = subprocess.CREATE_NEW_CONSOLE if new_console and os.name == "nt" else 0
    proc = subprocess.Popen(command, cwd=str(cwd), env=env, creationflags=flags)
    processes.append(proc)
    return proc


def wait_for_llm(timeout_sec=30):
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            with urlopen("http://127.0.0.1:5000/health", timeout=1) as response:
                if response.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def start_llm():
    env = os.environ.copy()
    env["LLM_TEXT_JSONL"] = str(SHARED_TEXT_FILE)

    if LLM_EXE.exists():
        return run_process([str(LLM_EXE)], BASE_DIR, env)

    if not LLM_SCRIPT.exists():
        raise FileNotFoundError(f"找不到 LLM 程式：{LLM_SCRIPT}")

    return run_process([sys.executable, str(LLM_SCRIPT)], LLM_SCRIPT.parent, env)


def start_stt(url, profile_dir):
    env = os.environ.copy()
    env["STT_STREAM_URL"] = url
    env["STT_OUTPUT_JSONL"] = str(SHARED_TEXT_FILE)
    env["STT_CHROME_PROFILE"] = profile_dir

    if STT_EXE.exists():
        return run_process([
            str(STT_EXE),
            "--url", url,
            "--output", str(SHARED_TEXT_FILE),
            "--chrome-profile", profile_dir,
        ], BASE_DIR, env)

    if not STT_SCRIPT.exists():
        raise FileNotFoundError(f"找不到 STT 程式：{STT_SCRIPT}")

    return run_process([
        sys.executable,
        str(STT_SCRIPT),
        "--url", url,
        "--output", str(SHARED_TEXT_FILE),
        "--chrome-profile", profile_dir,
    ], STT_SCRIPT.parent, env)


def open_chrome(url, profile_dir):
    if not Path(CHROME_PATH).exists():
        raise FileNotFoundError(f"找不到 Chrome：{CHROME_PATH}")

    subprocess.Popen([
        CHROME_PATH,
        f"--profile-directory={profile_dir}",
        url,
    ])


def start_pipeline(url, profile_name):
    if not url.startswith(("http://", "https://")):
        raise ValueError("請輸入有效的直播網址，需以 http 或 https 開頭。")

    SHARED_TEXT_FILE.write_text("", encoding="utf-8")
    start_llm()

    if not wait_for_llm():
        raise RuntimeError("LLM server 30 秒內沒有啟動成功，請確認 Ollama / Supabase 設定。")

    start_stt(url, PROFILES[profile_name])
    open_chrome(url, PROFILES[profile_name])


user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

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

CS_HREDRAW = 0x0002
CS_VREDRAW = 0x0001
CW_USEDEFAULT = 0x80000000
WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_VISIBLE = 0x10000000
WS_CHILD = 0x40000000
WS_TABSTOP = 0x00010000
ES_AUTOHSCROLL = 0x0080

WM_DESTROY = 0x0002
WM_PAINT = 0x000F
WM_COMMAND = 0x0111
WM_SETFONT = 0x0030
WM_LBUTTONUP = 0x0202
SW_SHOW = 5
DT_LEFT = 0x00000000
DT_CENTER = 0x00000001
DT_VCENTER = 0x00000004
DT_WORDBREAK = 0x00000010
TRANSPARENT = 1

ID_URL = 2000

WIDTH = 560
HEIGHT = 640
CARD_RECT = (62, 42, 498, 586)
START_RECT = (128, 492, 432, 546)
PROFILE_RECTS = {
    "帳號 A": (126, 228, 220, 262),
    "帳號 B": (234, 228, 328, 262),
    "帳號 C": (342, 228, 436, 262),
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
        -size, 0, 0, 0, weight, 0, 0, 0, 0, 0, 0, 0, 0, "Microsoft JhengHei UI"
    )


FONT_TITLE = make_font(25, 700)
FONT_TAB = make_font(15, 700)
FONT_BODY = make_font(16, 400)
FONT_BODY_BOLD = make_font(16, 700)
FONT_SMALL = make_font(13, 400)
FONT_AVATAR = make_font(21, 700)

COLOR_BG = rgb(224, 226, 230)
COLOR_BG_SHADOW = rgb(196, 199, 205)
COLOR_CARD = rgb(45, 47, 52)
COLOR_CARD_2 = rgb(39, 41, 46)
COLOR_TEXT = rgb(250, 250, 250)
COLOR_MUTED = rgb(154, 158, 166)
COLOR_LINE = rgb(83, 87, 96)
COLOR_GREEN = rgb(30, 215, 96)
COLOR_GREEN_DARK = rgb(21, 178, 76)

hwnd_main = None
hwnd_url = None
selected_profile = "帳號 B"
is_busy = False
status_text = "就緒，請輸入直播網址。"


def set_control_font(hwnd, font):
    user32.SendMessageW(hwnd, WM_SETFONT, font, True)


def draw_text(hdc, text, area, font, color, flags=DT_LEFT):
    old_font = gdi32.SelectObject(hdc, font)
    old_mode = gdi32.SetBkMode(hdc, TRANSPARENT)
    gdi32.SetTextColor(hdc, color)
    rc = rect(*area)
    user32.DrawTextW(hdc, text, -1, ctypes.byref(rc), flags)
    gdi32.SetBkMode(hdc, old_mode)
    gdi32.SelectObject(hdc, old_font)


def fill_rect(hdc, area, color):
    brush = gdi32.CreateSolidBrush(color)
    rc = rect(*area)
    user32.FillRect(hdc, ctypes.byref(rc), brush)
    gdi32.DeleteObject(brush)


def fill_round_rect(hdc, area, color, radius=18):
    brush = gdi32.CreateSolidBrush(color)
    old_brush = gdi32.SelectObject(hdc, brush)
    old_pen = gdi32.SelectObject(hdc, gdi32.GetStockObject(8))
    gdi32.RoundRect(hdc, area[0], area[1], area[2], area[3], radius, radius)
    gdi32.SelectObject(hdc, old_pen)
    gdi32.SelectObject(hdc, old_brush)
    gdi32.DeleteObject(brush)


def stroke_round_rect(hdc, area, color, radius=18, width=1):
    pen = gdi32.CreatePen(0, width, color)
    old_pen = gdi32.SelectObject(hdc, pen)
    old_brush = gdi32.SelectObject(hdc, gdi32.GetStockObject(5))
    gdi32.RoundRect(hdc, area[0], area[1], area[2], area[3], radius, radius)
    gdi32.SelectObject(hdc, old_brush)
    gdi32.SelectObject(hdc, old_pen)
    gdi32.DeleteObject(pen)


def fill_ellipse(hdc, area, color):
    brush = gdi32.CreateSolidBrush(color)
    old_brush = gdi32.SelectObject(hdc, brush)
    old_pen = gdi32.SelectObject(hdc, gdi32.GetStockObject(8))
    gdi32.Ellipse(hdc, area[0], area[1], area[2], area[3])
    gdi32.SelectObject(hdc, old_pen)
    gdi32.SelectObject(hdc, old_brush)
    gdi32.DeleteObject(brush)


def draw_line(hdc, x1, y1, x2, y2, color, width=2):
    pen = gdi32.CreatePen(0, width, color)
    old_pen = gdi32.SelectObject(hdc, pen)
    gdi32.MoveToEx(hdc, x1, y1, None)
    gdi32.LineTo(hdc, x2, y2)
    gdi32.SelectObject(hdc, old_pen)
    gdi32.DeleteObject(pen)


def point_in_rect(x, y, area):
    return area[0] <= x <= area[2] and area[1] <= y <= area[3]


def selected_profile_dir():
    return PROFILES[selected_profile]


def draw_logo(hdc):
    fill_ellipse(hdc, (180, 90, 222, 132), COLOR_GREEN)
    draw_text(hdc, "FB", (180, 101, 222, 124), FONT_SMALL, COLOR_CARD, DT_CENTER | DT_VCENTER)
    draw_text(hdc, "直播自動留言", (232, 91, 392, 124), FONT_TITLE, COLOR_TEXT, DT_CENTER)


def draw_profile_tabs(hdc):
    for name, area in PROFILE_RECTS.items():
        active = name == selected_profile
        if active:
            fill_round_rect(hdc, area, rgb(58, 61, 68), 14)
            draw_line(hdc, area[0] + 16, area[3] + 3, area[2] - 16, area[3] + 3, COLOR_GREEN, 3)
        draw_text(hdc, name, (area[0], area[1] + 8, area[2], area[3]), FONT_TAB, COLOR_TEXT if active else rgb(205, 207, 212), DT_CENTER)


def draw_avatar(hdc):
    avatar_text, avatar_color = PROFILE_AVATARS.get(selected_profile_dir(), ("FB", COLOR_GREEN))
    fill_ellipse(hdc, (238, 286, 322, 370), avatar_color)
    draw_text(hdc, avatar_text, (238, 309, 322, 352), FONT_AVATAR, rgb(255, 255, 255), DT_CENTER | DT_VCENTER)
    info = f"{PROFILE_LABELS[selected_profile]}"
    draw_text(hdc, info, (160, 376, 400, 402), FONT_SMALL, COLOR_MUTED, DT_CENTER)


def draw_start_button(hdc):
    color = rgb(112, 120, 128) if is_busy else COLOR_GREEN
    fill_round_rect(hdc, START_RECT, color, 26)
    text = "啟動中..." if is_busy else "開始執行"
    draw_text(hdc, text, (START_RECT[0], START_RECT[1] + 17, START_RECT[2], START_RECT[3]), FONT_BODY_BOLD, rgb(255, 255, 255), DT_CENTER)


def paint_window(hwnd):
    ps = PAINTSTRUCT()
    hdc = user32.BeginPaint(hwnd, ctypes.byref(ps))
    try:
        fill_rect(hdc, (0, 0, WIDTH, HEIGHT), COLOR_BG)
        fill_ellipse(hdc, (-160, 390, 220, 760), COLOR_BG_SHADOW)
        fill_ellipse(hdc, (360, -120, 760, 260), rgb(236, 238, 241))

        fill_round_rect(hdc, (CARD_RECT[0] + 7, CARD_RECT[1] + 10, CARD_RECT[2] + 7, CARD_RECT[3] + 10), rgb(183, 186, 193), 8)
        fill_round_rect(hdc, CARD_RECT, COLOR_CARD, 8)
        fill_round_rect(hdc, (CARD_RECT[0], CARD_RECT[1], CARD_RECT[2], CARD_RECT[1] + 90), COLOR_CARD_2, 8)

        draw_logo(hdc)
        draw_profile_tabs(hdc)
        draw_avatar(hdc)

        draw_text(hdc, "Facebook 直播網址", (128, 407, 432, 428), FONT_SMALL, COLOR_MUTED, DT_CENTER)
        fill_round_rect(hdc, (104, 434, 456, 480), rgb(255, 255, 255), 24)
        draw_start_button(hdc)

        draw_text(hdc, status_text, (94, 558, 466, 580), FONT_SMALL, COLOR_MUTED, DT_CENTER | DT_WORDBREAK)
    finally:
        user32.EndPaint(hwnd, ctypes.byref(ps))


def get_url_text():
    length = user32.GetWindowTextLengthW(hwnd_url)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd_url, buffer, length + 1)
    return buffer.value.strip()


def show_message(title, text, error=False):
    flags = 0x10 if error else 0x40
    user32.MessageBoxW(hwnd_main, text, title, flags)


def set_status(text):
    global status_text
    status_text = text
    user32.InvalidateRect(hwnd_main, None, True)


def set_busy(value):
    global is_busy
    is_busy = value
    user32.EnableWindow(hwnd_url, not value)
    user32.InvalidateRect(hwnd_main, None, True)


def start_clicked():
    if is_busy:
        return

    url = get_url_text()
    profile_name = selected_profile

    def worker():
        set_busy(True)
        set_status("正在啟動 LLM、STT 與 Chrome...")
        try:
            start_pipeline(url, profile_name)
            set_status("流程已啟動，請確認 Chrome extension 已開啟。")
            show_message("啟動完成", "LLM、STT 與 Chrome 已啟動，請確認 Chrome extension 已開啟。")
        except Exception as exc:
            set_status("啟動失敗，請查看錯誤訊息。")
            show_message("啟動失敗", str(exc), error=True)
        finally:
            set_busy(False)

    threading.Thread(target=worker, daemon=True).start()


@WNDPROC
def wnd_proc(hwnd, msg, wparam, lparam):
    global selected_profile

    if msg == WM_PAINT:
        paint_window(hwnd)
        return 0

    if msg == WM_LBUTTONUP:
        x = lparam & 0xFFFF
        y = (lparam >> 16) & 0xFFFF
        if point_in_rect(x, y, START_RECT):
            start_clicked()
            return 0
        for profile_name, area in PROFILE_RECTS.items():
            if point_in_rect(x, y, area):
                selected_profile = profile_name
                set_status(f"已選擇 {profile_name}（{PROFILE_LABELS[profile_name]}）。")
                user32.InvalidateRect(hwnd, None, True)
                return 0

    if msg == WM_COMMAND:
        return 0

    if msg == WM_DESTROY:
        for proc in processes:
            if proc.poll() is None:
                proc.terminate()
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

hwnd_url = create_child("EDIT", "", ES_AUTOHSCROLL | WS_TABSTOP, 128, 447, 304, 22, ID_URL)
set_control_font(hwnd_url, FONT_BODY)

user32.ShowWindow(hwnd_main, SW_SHOW)
user32.UpdateWindow(hwnd_main)

msg = wintypes.MSG()
while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
    user32.TranslateMessage(ctypes.byref(msg))
    user32.DispatchMessageW(ctypes.byref(msg))
