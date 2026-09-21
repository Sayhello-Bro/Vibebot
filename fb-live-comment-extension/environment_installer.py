import ctypes
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


CHROME_EXE = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
MONGODB_SERVICE = "MongoDB"
OLLAMA_MODEL = "nomic-embed-text"


def base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BASE_DIR = base_dir()
PROJECT_DIR = BASE_DIR.parent
EXTENSION_DIR = PROJECT_DIR / "fb-live-comment-extension"
CHROME_EXTENSION_DIR = EXTENSION_DIR / "chrome_extension"
DIST_DIR = EXTENSION_DIR / "dist"
LOG_FILE = DIST_DIR / "environment_setup.log"


def write_log(message):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(message + "\n")


def say(message=""):
    print(message, flush=True)
    write_log(message)


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin():
    params = " ".join(f'"{arg}"' for arg in sys.argv[1:])
    rc = ctypes.windll.shell32.ShellExecuteW(
        None,
        "runas",
        sys.executable,
        params,
        str(BASE_DIR),
        1,
    )
    return rc > 32


def run(command, check=False, timeout=None):
    say(f"> {' '.join(command)}")
    completed = subprocess.run(
        command,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )
    if completed.stdout:
        say(completed.stdout.rstrip())
    if check and completed.returncode != 0:
        raise RuntimeError(f"Command failed with exit code {completed.returncode}: {' '.join(command)}")
    return completed.returncode == 0


def find_executable(name, extra_paths=None):
    found = shutil.which(name)
    if found:
        return found
    for folder in extra_paths or []:
        candidate = Path(folder) / name
        if candidate.exists():
            return str(candidate)
    return None


def require_winget():
    if shutil.which("winget"):
        return True
    say("[ERROR] 找不到 winget。請先更新 Windows App Installer，或手動安裝 Chrome / MongoDB / Ollama。")
    return False


def winget_install(package_id, display_name):
    if not require_winget():
        return False
    say(f"[INSTALL] 正在安裝 {display_name}...")
    return run([
        "winget",
        "install",
        "--id",
        package_id,
        "--silent",
        "--accept-package-agreements",
        "--accept-source-agreements",
    ])


def ensure_chrome():
    say("\n[CHECK] Google Chrome")
    if CHROME_EXE.exists():
        say(f"[OK] Chrome 已安裝：{CHROME_EXE}")
        return True
    ok = winget_install("Google.Chrome", "Google Chrome")
    if CHROME_EXE.exists():
        say("[OK] Chrome 安裝完成。")
        return True
    if not ok:
        say("[WARN] Chrome 自動安裝失敗，請手動安裝 Google Chrome。")
    return ok


def service_exists(name):
    result = subprocess.run(["sc", "query", name], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return result.returncode == 0


def ensure_mongodb():
    say("\n[CHECK] MongoDB")
    if not service_exists(MONGODB_SERVICE):
        ok = winget_install("MongoDB.Server", "MongoDB Community Server")
        time.sleep(2)
        if not ok and not service_exists(MONGODB_SERVICE):
            say("[WARN] MongoDB 自動安裝失敗，請手動安裝 MongoDB Community Server。")
            return False

    say("[OK] MongoDB 已安裝。")
    run(["sc", "start", MONGODB_SERVICE])
    return True


def ollama_path():
    return find_executable(
        "ollama.exe",
        [
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama",
            Path(os.environ.get("PROGRAMFILES", "")) / "Ollama",
        ],
    )


def ensure_ollama():
    say("\n[CHECK] Ollama")
    exe = ollama_path()
    if not exe:
        ok = winget_install("Ollama.Ollama", "Ollama")
        time.sleep(2)
        exe = ollama_path()
        if not ok and not exe:
            say("[WARN] Ollama 自動安裝失敗，請手動安裝 Ollama。")
            return False

    say(f"[OK] Ollama 已安裝：{exe}")
    say(f"[INSTALL] 正在下載 Ollama 模型：{OLLAMA_MODEL}")
    return run([exe, "pull", OLLAMA_MODEL])


def check_project_files():
    say("\n[CHECK] 專案檔案")
    required = [
        DIST_DIR / "FB_Live_Auto_Comment.exe",
        DIST_DIR / "llm_server.exe",
        DIST_DIR / "stt_worker.exe",
        CHROME_EXTENSION_DIR / "manifest.json",
        CHROME_EXTENSION_DIR / "content.js",
    ]
    ok = True
    for path in required:
        if path.exists():
            say(f"[OK] {path}")
        else:
            say(f"[MISSING] {path}")
            ok = False
    return ok


def open_extension_page():
    say("\n[INFO] Chrome extension 需要手動載入一次。")
    say(f"[INFO] 請在 Chrome 擴充功能頁面載入這個資料夾：{CHROME_EXTENSION_DIR}")
    if CHROME_EXE.exists():
        subprocess.Popen([str(CHROME_EXE), "chrome://extensions"])


def main():
    os.system("title FB Live Environment Setup")
    say("FB 直播自動留言系統 - 環境安裝器")
    say("=" * 60)

    if os.name == "nt" and not is_admin():
        say("[INFO] 需要系統管理員權限安裝 MongoDB / Chrome / Ollama。")
        say("[INFO] 正在重新以系統管理員身分開啟...")
        if relaunch_as_admin():
            return
        say("[ERROR] 無法取得系統管理員權限。")
        input("按 Enter 關閉...")
        return

    results = [
        ("專案檔案", check_project_files()),
        ("Chrome", ensure_chrome()),
        ("MongoDB", ensure_mongodb()),
        ("Ollama + model", ensure_ollama()),
    ]

    say("\n" + "=" * 60)
    say("安裝檢查結果")
    for name, ok in results:
        say(f"{'[OK]' if ok else '[WARN]'} {name}")

    open_extension_page()

    say("\n下一步：")
    say("1. 在 Chrome 擴充功能頁面打開「開發人員模式」。")
    say(f"2. 點「載入未封裝項目」，選擇：{CHROME_EXTENSION_DIR}")
    say(f"3. 執行主程式：{DIST_DIR / 'FB_Live_Auto_Comment.exe'}")
    say(f"\n安裝紀錄：{LOG_FILE}")
    input("\n按 Enter 關閉...")


if __name__ == "__main__":
    main()
