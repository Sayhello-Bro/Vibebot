"""Recent Facebook broadcasters shown beneath the live URL list."""

import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit


MAX_FAVORITES = 3
FACEBOOK_HOSTS = {"facebook.com", "www.facebook.com", "web.facebook.com", "m.facebook.com"}
NON_PROFILE_PATHS = {
    "watch", "live", "videos", "reel", "reels", "share", "shares", "groups",
    "events", "marketplace", "gaming", "stories", "story.php", "permalink.php",
    "photo.php", "login", "home.php", "settings", "messages", "notifications",
}


def facebook_profile_url(candidate):
    """Return a broadcaster's homepage, never a live/video URL."""
    if not isinstance(candidate, str):
        return ""
    parts = urlsplit(candidate.strip())
    if parts.scheme not in {"http", "https"} or (parts.hostname or "").lower() not in FACEBOOK_HOSTS:
        return ""
    segments = [part for part in parts.path.split("/") if part]
    if not segments:
        return ""
    first = segments[0]
    if first.lower() == "profile.php":
        identifier = parse_qs(parts.query).get("id", [""])[0]
        return f"https://www.facebook.com/profile.php?id={identifier}" if identifier.isdigit() else ""
    if first.lower() in NON_PROFILE_PATHS:
        return ""
    if len(segments) > 1 and segments[1].lower() not in {"videos", "video", "posts", "live"}:
        # Facebook also uses /people/Name/id and /pages/Name/id as homepages.
        if first.lower() in {"people", "pages"} and len(segments) >= 3:
            return urlunsplit(("https", "www.facebook.com", "/" + "/".join(segments[:3]), "", ""))
        return ""
    return urlunsplit(("https", "www.facebook.com", "/" + first, "", ""))


def resolve_profile_url(live_url, *candidates):
    for candidate in candidates:
        profile = facebook_profile_url(candidate)
        if profile:
            return profile
    return facebook_profile_url(live_url)


def upsert_favorite(entries, name, profile_url, entered_at=0):
    name = " ".join(str(name or "").split())[:80]
    profile_url = facebook_profile_url(profile_url)
    if not name or not profile_url:
        return list(entries)[:MAX_FAVORITES]
    previous = next((item for item in entries if item.get("url", "").casefold() == profile_url.casefold()), None)
    entered_at = max(int(entered_at or 0), int(previous.get("entered_at", 0)) if previous else 0)
    remaining = [item for item in entries if item.get("url", "").casefold() != profile_url.casefold()]
    result = [{"name": name, "url": profile_url, "entered_at": entered_at}, *remaining]
    return sorted(result, key=lambda item: int(item.get("entered_at", 0)), reverse=True)[:MAX_FAVORITES]


def load_favorites(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    entries = []
    for index, item in enumerate(reversed(data), start=1):
        if isinstance(item, dict):
            entries = upsert_favorite(entries, item.get("name"), item.get("url"), item.get("entered_at", index))
    return entries


def save_favorites(path, entries):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(entries[:MAX_FAVORITES], ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, destination)
