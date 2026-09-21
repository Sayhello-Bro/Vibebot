import subprocess
import queue
import threading
import time
import os
import sys
from pathlib import Path
import yt_dlp

CHUNK_SIZE = 3200
COOKIE_RETRY_SECONDS = 600
_cookieless_until = {}


def clean_live_title(value):
    title = str(value or "").strip()
    if "|" in title:
        prefix, remainder = title.split("|", 1)
        prefix_lower = prefix.lower()
        if "view" in prefix_lower or "reaction" in prefix_lower or "觀看" in prefix:
            title = remainder.strip()
    return title


class QuietYtdlpLogger:
    def debug(self, message):
        pass

    def warning(self, message):
        pass

    def error(self, message):
        pass

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
# 使用 yt_dlp API取得直播串流資訊
# =========================
def _extract_stream_info(url, chrome_profile=None):

    ydl_opts = {
        "format": "bestaudio",
        "quiet": True,
        "no_warnings": True,
        "logger": QuietYtdlpLogger(),
        "noplaylist": True
    }

    if chrome_profile:
        ydl_opts["cookiesfrombrowser"] = ("chrome", chrome_profile, None, None)

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:

        info = ydl.extract_info(url, download=False)

        # Prefer HLS when available. A DASH URL can keep pointing at signed
        # fragments that have already expired during a long Facebook live.
        hls_formats = [
            item for item in info.get("formats") or []
            if item.get("url")
            and "m3u8" in str(item.get("protocol") or "").lower()
            and item.get("acodec") != "none"
        ]
        hls_formats.sort(
            key=lambda item: (item.get("vcodec") != "none", -float(item.get("abr") or item.get("tbr") or 0))
        )
        selected_format = hls_formats[0] if hls_formats else None
        stream_url = (
            (selected_format or {}).get("url")
            or info.get("url")
            or next((item.get("url") for item in info.get("formats") or [] if item.get("url")), None)
        )

        if not stream_url:
            raise RuntimeError("❌ 無法取得 stream URL")

        return {
            "stream_url": stream_url,
            "http_headers": (selected_format or {}).get("http_headers") or info.get("http_headers") or {},
            "title": clean_live_title(info.get("title")),
            "uploader": info.get("uploader"),
            "uploader_id": info.get("uploader_id"),
            "uploader_url": info.get("uploader_url"),
            "channel": info.get("channel"),
            "channel_url": info.get("channel_url"),
            "channel_id": info.get("channel_id"),
            "thumbnail": info.get("thumbnail"),
            "description": info.get("description"),
            "live_status": info.get("live_status"),
        }


# =========================
# 主函式
# =========================
def get_stream_info(url, chrome_profile=None):
    if chrome_profile:
        if time.monotonic() < _cookieless_until.get(chrome_profile, 0):
            return _extract_stream_info(url, None)
        try:
            return _extract_stream_info(url, chrome_profile)
        except Exception as e:
            error_text = str(e).lower()
            cookie_error_markers = (
                "cookie",
                "dpapi",
                "decrypt",
                "could not copy chrome",
            )
            if any(marker in error_text for marker in cookie_error_markers):
                _cookieless_until[chrome_profile] = time.monotonic() + COOKIE_RETRY_SECONDS
                print(
                    "[STT] Chrome cookies are unavailable. Retrying without browser cookies...",
                    flush=True,
                )
                return _extract_stream_info(url, None)
            raise

    return _extract_stream_info(url, None)


def get_stream_url(url, chrome_profile=None):
    return get_stream_info(url, chrome_profile)["stream_url"]


def _start_ffmpeg(stream_url, http_headers=None):
    command = [
        FFMPEG_PATH,
        "-hide_banner",
        "-loglevel", "warning",
        "-fflags", "+genpts",
        "-use_wallclock_as_timestamps", "1",
        "-avoid_negative_ts", "make_zero",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_on_network_error", "1",
        # A 404 on a signed DASH fragment needs a *new* Facebook URL, not
        # repeated requests to the same expired fragment.
        "-reconnect_on_http_error", "5xx",
        "-reconnect_delay_max", "5",
        "-rw_timeout", "15000000",
    ]
    if http_headers:
        header_text = "".join(
            f"{key}: {value}\r\n" for key, value in http_headers.items()
        )
        command.extend(["-headers", header_text])
    command.extend([
        "-i", stream_url,
        "-af", "asetpts=N/SR/TB",
        "-vn",
        "-ac", "1",
        "-ar", "16000",
        "-f", "s16le",
        "pipe:1",
    ])
    return subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )


def start_streaming(stream_id, url, chrome_profile=None):

    audio_queue = queue.Queue(maxsize=100)

    print(f"🚀 [{stream_id}] starting stream...")

    # =========================
    # 1. 取得直播音訊 URL
    # =========================
    try:

        stream_info = get_stream_info(url, chrome_profile)
        stream_url = stream_info["stream_url"]
        uploader = (
            stream_info.get("uploader")
            or stream_info.get("channel")
            or "unknown"
        )

        print(f"✅ [{stream_id}] stream URL ready (主播: {uploader})")

    except Exception as e:

        raise RuntimeError(
            f"❌ [{stream_id}] yt_dlp failed: {e}"
        )

    # Facebook's signed DASH fragment URLs can expire while a live is still
    # running. Re-run yt-dlp and replace ffmpeg whenever fragments repeatedly
    # return 404, instead of silently feeding less and less audio to STT.
    def reader():
        print(f"📡 [{stream_id}] reader started")
        current_info = stream_info
        reconnect_attempt = 0
        while True:
            try:
                if reconnect_attempt:
                    current_info = get_stream_info(url, chrome_profile)
                    stream_info.update(current_info)
                    print(
                        f"✅ [{stream_id}] refreshed Facebook stream URL "
                        f"(attempt {reconnect_attempt})",
                        flush=True,
                    )

                ffmpeg = _start_ffmpeg(
                    current_info["stream_url"], current_info.get("http_headers")
                )
                stale_url = threading.Event()
                ffmpeg_done = threading.Event()
                error_count = [0]
                last_audio_at = [time.monotonic()]

                def ffmpeg_logger():
                    while True:
                        line = ffmpeg.stderr.readline()
                        if not line:
                            break
                        message = line.decode(errors="ignore").strip()
                        if "http error 404" not in message.lower() or error_count[0] == 0:
                            print(f"[{stream_id} ffmpeg] {message}", flush=True)
                        lowered = message.lower()
                        if "http error 404" in lowered or "failed to open fragment" in lowered:
                            error_count[0] += 1

                def ffmpeg_watchdog():
                    while not ffmpeg_done.wait(1):
                        silent_for = time.monotonic() - last_audio_at[0]
                        if (error_count[0] >= 2 and silent_for >= 6) or silent_for >= 18:
                            stale_url.set()
                            print(
                                f"♻️ [{stream_id}] live audio stalled for {silent_for:.0f}s; "
                                "refreshing stream URL...",
                                flush=True,
                            )
                            try:
                                ffmpeg.terminate()
                            except Exception:
                                pass
                            return

                threading.Thread(target=ffmpeg_logger, daemon=True).start()
                threading.Thread(target=ffmpeg_watchdog, daemon=True).start()

                try:
                    while True:
                        chunk = ffmpeg.stdout.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        last_audio_at[0] = time.monotonic()
                        error_count[0] = 0
                        try:
                            audio_queue.put_nowait(chunk)
                        except queue.Full:
                            # Keep recognition close to live instead of replaying a
                            # growing backlog after a network interruption.
                            try:
                                audio_queue.get_nowait()
                                audio_queue.put_nowait(chunk)
                            except queue.Empty:
                                pass
                finally:
                    ffmpeg_done.set()
                    try:
                        if ffmpeg.poll() is None:
                            ffmpeg.terminate()
                        ffmpeg.wait(timeout=3)
                    except Exception:
                        try:
                            ffmpeg.kill()
                        except Exception:
                            pass

                reconnect_attempt += 1
                reason = "expired fragments" if stale_url.is_set() else "stream ended"
                print(
                    f"⚠️ [{stream_id}] ffmpeg {reason}; reconnecting...",
                    flush=True,
                )
                time_to_wait = min(5, 1 + reconnect_attempt)
                threading.Event().wait(time_to_wait)
            except Exception as e:
                reconnect_attempt += 1
                print(f"❌ [{stream_id}] reconnect error: {e}", flush=True)
                threading.Event().wait(min(15, 2 + reconnect_attempt))

    threading.Thread(
        target=reader,
        daemon=True
    ).start()

    return audio_queue, stream_info
