import subprocess
import queue
import threading
import os
import sys
from pathlib import Path
import yt_dlp

CHUNK_SIZE = 3200

# =========================
# ffmpeg 指定路徑
# =========================
def get_base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BASE_DIR = get_base_dir()
FFMPEG_PATH = os.environ.get("FFMPEG_PATH", str(BASE_DIR / "ffmpeg.exe"))

if not os.path.exists(FFMPEG_PATH):
    FFMPEG_PATH = r"D:\ffmpeg-8.1.1-essentials_build\bin\ffmpeg.exe"

if not os.path.exists(FFMPEG_PATH):
    try:
        import imageio_ffmpeg
        FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass

if not os.path.exists(FFMPEG_PATH):
    raise RuntimeError(f"❌ ffmpeg not found: {FFMPEG_PATH}")


# =========================
# 使用 yt_dlp API 取得直播串流 URL
# =========================
def _extract_stream_url(url, chrome_profile=None):

    ydl_opts = {
        "format": "bestaudio",
        "quiet": True,
        "noplaylist": True
    }

    if chrome_profile:
        ydl_opts["cookiesfrombrowser"] = ("chrome", chrome_profile, None, None)

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:

        info = ydl.extract_info(url, download=False)

        # 有些 FB live 在 formats 裡
        if "url" in info:
            return info["url"]

        if "formats" in info and len(info["formats"]) > 0:
            return info["formats"][0]["url"]

        raise RuntimeError("❌ 無法取得 stream URL")


# =========================
# 主函式
# =========================
def get_stream_url(url, chrome_profile=None):
    if chrome_profile:
        try:
            return _extract_stream_url(url, chrome_profile)
        except Exception as e:
            if "Could not copy Chrome cookie database" in str(e):
                print(
                    "[STT] Chrome cookie database is locked. Retrying without browser cookies...",
                    flush=True,
                )
                return _extract_stream_url(url, None)
            raise

    return _extract_stream_url(url, None)


def start_streaming(stream_id, url, chrome_profile=None):

    audio_queue = queue.Queue(maxsize=200)

    print(f"🚀 [{stream_id}] starting stream...")

    # =========================
    # 1. 取得直播音訊 URL
    # =========================
    try:

        stream_url = get_stream_url(url, chrome_profile)

        print(f"✅ [{stream_id}] stream URL ready")

    except Exception as e:

        raise RuntimeError(
            f"❌ [{stream_id}] yt_dlp failed: {e}"
        )

    # =========================
    # 2. ffmpeg 轉 PCM
    # =========================
    ffmpeg = subprocess.Popen(
        [
            FFMPEG_PATH,
            "-hide_banner",
            "-loglevel", "warning",
            "-fflags", "+genpts",
            "-use_wallclock_as_timestamps", "1",
            "-avoid_negative_ts", "make_zero",
            
            "-i", stream_url,
            "-af", "asetpts=N/SR/TB",
            "-vn",
            "-ac", "1",
            "-ar", "16000",
            "-f", "s16le",
            "pipe:1"
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0
    )

    # =========================
    # 3. ffmpeg error logger
    # =========================
    def ffmpeg_logger():

        while True:

            line = ffmpeg.stderr.readline()

            if not line:
                break

            print(
                f"[{stream_id} ffmpeg] "
                f"{line.decode(errors='ignore').strip()}"
            )

    threading.Thread(
        target=ffmpeg_logger,
        daemon=True
    ).start()

    # =========================
    # 4. reader thread
    # =========================
    def reader():

        print(f"📡 [{stream_id}] reader started")

        while True:

            try:

                chunk = ffmpeg.stdout.read(CHUNK_SIZE)

                if not chunk:
                    print(f"⚠️ [{stream_id}] ffmpeg stream ended")
                    break

                audio_queue.put(chunk, timeout=1)

            except queue.Full:

                print(
                    f"⚠️ [{stream_id}] queue full "
                    f"(drop audio)"
                )

            except Exception as e:

                print(
                    f"❌ [{stream_id}] reader error: {e}"
                )

                break

    threading.Thread(
        target=reader,
        daemon=True
    ).start()

    return audio_queue
