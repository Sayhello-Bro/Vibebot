"""Persistent robot cards and a localhost-only bridge for Facebook identity hints."""

import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


PROFILE_ORIGINS = {"https://www.facebook.com", "https://web.facebook.com"}
PROFILE_PATTERN = re.compile(r"^Profile ([1-9][0-9]*)$")
ACCOUNT_PATTERN = re.compile(r"^fb_account_[0-9]{3}$")
GENERIC_NAMES = {
    "facebook", "facebook 使用者", "log in", "log into facebook",
    "登入 facebook", "登入或註冊", "登入", "profile", "你的個人檔案",
    "your", "your profile", "你的",
}


def profile_number(profile_dir):
    if profile_dir == "Default":
        return 0
    match = PROFILE_PATTERN.fullmatch(str(profile_dir))
    return int(match.group(1)) if match else None


def profile_label(profile_dir):
    number = profile_number(profile_dir)
    if number is None or number > 25:
        return None
    return f"機器人 {chr(ord('A') + number)}"


def account_id_for_profile(profile_dir):
    number = profile_number(profile_dir)
    if number is None or number > 998:
        return None
    return f"fb_account_{number + 1:03d}"


def next_profile_to_show(active_profiles, next_profile_number):
    """Restore the lowest-numbered closed card before allocating a new profile."""
    active = {profile_number(item) for item in active_profiles}
    for number in range(next_profile_number):
        if number not in active:
            return number, True
    return next_profile_number, False


def load_card_state(path, defaults):
    """An existing saved list is authoritative, even when it is empty."""
    saved = {}
    try:
        saved = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        pass
    candidates = saved.get("profiles", defaults) if isinstance(saved, dict) else defaults
    if not isinstance(candidates, list):
        candidates = defaults
    profiles = []
    for item in candidates:
        if profile_label(item) and item not in profiles:
            profiles.append(item)
        if len(profiles) == 8:
            break
    names = saved.get("names", {}) if isinstance(saved, dict) else {}
    if not isinstance(names, dict):
        names = {}
    names = {
        key: normalize_display_name(value)
        for key, value in names.items()
        if profile_label(key) and normalize_display_name(value)
    }
    profiles.sort(key=profile_number)
    highest = max((profile_number(item) or 0 for item in profiles + list(defaults)), default=0)
    saved_next = saved.get("next_profile_number", 0) if isinstance(saved, dict) else 0
    next_number = max(highest + 1, saved_next if isinstance(saved_next, int) else 0)
    return {"profiles": profiles, "names": names, "next_profile_number": next_number}


def save_card_state(path, profiles, names, next_profile_number):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "profiles": sorted(profiles, key=profile_number),
        "names": {
            key: normalize_display_name(value)
            for key, value in names.items()
            if profile_label(key) and normalize_display_name(value)
        },
        "next_profile_number": next_profile_number,
    }
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, destination)


def normalize_display_name(value):
    if not isinstance(value, str):
        return ""
    name = " ".join(value.strip().split())[:80]
    if not name or name.casefold() in GENERIC_NAMES or any(ord(char) < 32 for char in name):
        return ""
    return name


def allowed_avatar_url(value):
    if not isinstance(value, str) or len(value) > 4096:
        return False
    parts = urlsplit(value)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and (
        host == "fbcdn.net" or host.endswith(".fbcdn.net")
        or host == "fbsbx.com" or host.endswith(".fbsbx.com")
    )


class _FacebookRedirectsOnly(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        if not allowed_avatar_url(new_url):
            raise HTTPError(new_url, 403, "avatar redirect outside Facebook", headers, file_pointer)
        return super().redirect_request(request, file_pointer, code, message, headers, new_url)


def download_avatar(url, destination):
    if not allowed_avatar_url(url):
        return False
    request = Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "image/jpeg,image/png,image/gif"})
    try:
        with build_opener(_FacebookRedirectsOnly()).open(request, timeout=6) as response:
            data = response.read(1_000_001)
        if len(data) > 1_000_000 or not (
            data.startswith(b"\xff\xd8\xff")
            or data.startswith(b"\x89PNG\r\n\x1a\n")
            or data.startswith((b"GIF87a", b"GIF89a"))
        ):
            return False
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_bytes(data)
        os.replace(temporary, destination)
        return True
    except (OSError, ValueError, HTTPError):
        return False


class ProfileBridgeHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def _allowed_origin(self):
        return self.headers.get("Origin", "") in PROFILE_ORIGINS

    def _reply(self, status, body=b"{}"):
        self.send_response(status)
        if self._allowed_origin():
            self.send_header("Access-Control-Allow-Origin", self.headers["Origin"])
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            if self.headers.get("Access-Control-Request-Private-Network") == "true":
                self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._reply(204 if self._allowed_origin() and self.path == "/account_profile" else 403, b"")

    def do_POST(self):
        if not self._allowed_origin() or self.path != "/account_profile":
            self._reply(403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 8192:
                raise ValueError("invalid body size")
            data = json.loads(self.rfile.read(length))
            account_id = str(data.get("account_id", ""))
            name = normalize_display_name(data.get("display_name"))
            avatar_url = data.get("avatar_url", "")
            avatar_url = avatar_url if allowed_avatar_url(avatar_url) else ""
            if not ACCOUNT_PATTERN.fullmatch(account_id) or not (name or avatar_url):
                raise ValueError("invalid profile data")
        except (TypeError, ValueError, json.JSONDecodeError):
            self._reply(400)
            return
        if not self.server.profile_callback(account_id, name, avatar_url):
            self._reply(404)
            return
        self._reply(200, b'{"status":"ok"}')


def start_profile_bridge(profile_callback, port=5003):
    server = ThreadingHTTPServer(("127.0.0.1", port), ProfileBridgeHandler)
    server.daemon_threads = True
    server.profile_callback = profile_callback
    thread = threading.Thread(target=server.serve_forever, name="FacebookProfileBridge", daemon=True)
    thread.start()
    return server
