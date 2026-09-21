"""不依賴 Tkinter 的原生 Windows 登入介面。"""

import ctypes
import hashlib
import json
import os
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path


BASE_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
ACCOUNT_FILE = BASE_DIR / "login_accounts.json"


def load_accounts():
    if not ACCOUNT_FILE.exists():
        return {}
    try:
        data = json.loads(ACCOUNT_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_accounts(accounts):
    ACCOUNT_FILE.write_text(json.dumps(accounts, ensure_ascii=False, indent=2), encoding="utf-8")


def password_hash(account, password):
    return hashlib.sha256(f"{account}\0{password}".encode("utf-8")).hexdigest()


user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(
    LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)

user32.DefWindowProcW.restype = LRESULT
user32.CreateWindowExW.restype = wintypes.HWND
user32.CallWindowProcW.restype = LRESULT

WS_OVERLAPPED = 0x00000000
WS_CAPTION = 0x00C00000
WS_SYSMENU = 0x00080000
WS_MINIMIZEBOX = 0x00020000
WS_VISIBLE = 0x10000000
WS_CHILD = 0x40000000
WS_TABSTOP = 0x00010000
WS_BORDER = 0x00800000
ES_AUTOHSCROLL = 0x0080
ES_PASSWORD = 0x0020
BS_PUSHBUTTON = 0
CW_USEDEFAULT = 0x80000000

WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
WM_CTLCOLORSTATIC = 0x0138
WM_CTLCOLOREDIT = 0x0133
WM_SETFONT = 0x0030
EM_SETPASSWORDCHAR = 0x00CC
EN_UPDATE = 0x0400
BN_CLICKED = 0
SW_SHOW = 5

ID_ACCOUNT = 1001
ID_PASSWORD = 1002
ID_EYE = 1003
ID_LOGIN = 1004
ID_SAVE = 1005

COLOR_BG = 0x00342F2D
COLOR_TEXT = 0x00FFFFFF
COLOR_MUTED = 0x00A69E9A


class WNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HANDLE),
        ("hIcon", wintypes.HANDLE),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HANDLE),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


def make_font(size, weight=400):
    return gdi32.CreateFontW(
        -size, 0, 0, 0, weight, 0, 0, 0, 0, 0, 0, 0, 0, "Microsoft JhengHei UI"
    )


FONT_TITLE = make_font(28, 700)
FONT_BODY = make_font(17)
FONT_BUTTON = make_font(16, 700)
FONT_SMALL = make_font(13)
BG_BRUSH = gdi32.CreateSolidBrush(COLOR_BG)

hwnd_main = None
hwnd_account = None
hwnd_password = None
hwnd_eye = None
password_visible = False


def control_text(hwnd):
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def message(title, text, error=False):
    user32.MessageBoxW(hwnd_main, text, title, 0x10 if error else 0x40)


def register_account():
    account = control_text(hwnd_account).strip()
    password = control_text(hwnd_password)
    if not account or not password:
        message("資料不完整", "帳號與密碼皆為必填。", True)
        return
    accounts = load_accounts()
    accounts[account] = {"password_hash": password_hash(account, password)}
    save_accounts(accounts)
    message("儲存完成", "帳號已儲存於本機；密碼不會以明文保存。")


def login():
    account = control_text(hwnd_account).strip()
    password = control_text(hwnd_password)
    record = load_accounts().get(account, {})
    if not account or record.get("password_hash") != password_hash(account, password):
        message("登入失敗", "帳號或密碼錯誤。", True)
        return
    env = os.environ.copy()
    env["FB_AUTO_LOGIN_ACCOUNT"] = account
    exe = BASE_DIR / "FB_Live_Auto_Comment.exe"
    command = [str(exe)] if exe.exists() else [sys.executable, str(BASE_DIR / "launcher.py")]
    subprocess.Popen(command, cwd=str(BASE_DIR), env=env)
    user32.DestroyWindow(hwnd_main)


def toggle_password():
    global password_visible
    password_visible = not password_visible
    user32.SendMessageW(hwnd_password, EM_SETPASSWORDCHAR, 0 if password_visible else ord("*"), 0)
    user32.SetWindowTextW(hwnd_eye, "隱藏" if password_visible else "顯示")
    user32.InvalidateRect(hwnd_password, None, True)


@WNDPROC
def wnd_proc(hwnd, msg, wparam, lparam):
    if msg in (WM_CTLCOLORSTATIC, WM_CTLCOLOREDIT):
        hdc = wparam
        user32.SetTextColor(hdc, COLOR_TEXT if msg == WM_CTLCOLORSTATIC else 0x00282020)
        user32.SetBkColor(hdc, COLOR_BG if msg == WM_CTLCOLORSTATIC else 0x00FFFFFF)
        if msg == WM_CTLCOLORSTATIC:
            return BG_BRUSH
    if msg == WM_COMMAND:
        control_id = wparam & 0xFFFF
        notification = (wparam >> 16) & 0xFFFF
        if notification == BN_CLICKED:
            if control_id == ID_EYE:
                toggle_password()
            elif control_id == ID_LOGIN:
                login()
            elif control_id == ID_SAVE:
                register_account()
        return 0
    if msg == WM_DESTROY:
        user32.PostQuitMessage(0)
        return 0
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def child(kind, text, style, x, y, width, height, control_id, font=FONT_BODY):
    control = user32.CreateWindowExW(
        0, kind, text, WS_CHILD | WS_VISIBLE | style,
        x, y, width, height, hwnd_main, control_id, hinst, None,
    )
    user32.SendMessageW(control, WM_SETFONT, font, True)
    return control


def run():
    global hwnd_main, hwnd_account, hwnd_password, hwnd_eye
    wc = WNDCLASS()
    wc.style = 3
    wc.lpfnWndProc = wnd_proc
    wc.hInstance = hinst
    wc.hIcon = user32.LoadIconW(None, 32512)
    wc.hCursor = user32.LoadCursorW(None, 32512)
    wc.hbrBackground = BG_BRUSH
    wc.lpszClassName = "FBLiveNativeLogin"
    user32.RegisterClassW(ctypes.byref(wc))

    hwnd_main = user32.CreateWindowExW(
        0, wc.lpszClassName, "登入｜FB 直播自動留言",
        WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX,
        CW_USEDEFAULT, CW_USEDEFAULT, 460, 510, None, None, hinst, None,
    )
    child("STATIC", "FB  直播自動留言", 0, 76, 40, 320, 42, 0, FONT_TITLE)
    child("STATIC", "登入介面", 0, 76, 90, 320, 34, 0, FONT_TITLE)
    child("STATIC", "帳號", 0, 76, 148, 300, 24, 0, FONT_SMALL)
    hwnd_account = child("EDIT", "", WS_BORDER | WS_TABSTOP | ES_AUTOHSCROLL,
                         76, 174, 300, 36, ID_ACCOUNT)
    child("STATIC", "密碼", 0, 76, 230, 300, 24, 0, FONT_SMALL)
    hwnd_password = child("EDIT", "", WS_BORDER | WS_TABSTOP | ES_AUTOHSCROLL | ES_PASSWORD,
                          76, 256, 226, 36, ID_PASSWORD)
    hwnd_eye = child("BUTTON", "顯示", BS_PUSHBUTTON | WS_TABSTOP,
                     307, 256, 69, 36, ID_EYE, FONT_SMALL)
    child("STATIC", "輸入時以 * 顯示；按「顯示」才會顯示密碼",
          0, 76, 302, 300, 24, 0, FONT_SMALL)
    child("BUTTON", "登入", BS_PUSHBUTTON | WS_TABSTOP,
          76, 344, 300, 44, ID_LOGIN, FONT_BUTTON)
    child("BUTTON", "儲存帳號", BS_PUSHBUTTON | WS_TABSTOP,
          76, 402, 300, 40, ID_SAVE, FONT_BUTTON)
    user32.SetFocus(hwnd_account)
    user32.ShowWindow(hwnd_main, SW_SHOW)
    user32.UpdateWindow(hwnd_main)

    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0):
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


hinst = kernel32.GetModuleHandleW(None)

if __name__ == "__main__":
    run()
