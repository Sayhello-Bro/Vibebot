#!/bin/bash
set -eu

# Resolve relative to this launcher, even when called from another folder.
json_test_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$json_test_root"

if [ ! -x "$json_test_root/.venv-cls/bin/python" ]; then
    printf '%s\n' '找不到 .venv-cls/bin/python，請先依 README 建立專案的 Python 環境。' >&2
    exit 1
fi

export USE_CLS_MEMORY="${USE_CLS_MEMORY:-1}"
export CLS_OFFLINE="${CLS_OFFLINE:-1}"
export OLLAMA_THINK="${OLLAMA_THINK:-0}"
# Formal launches must always use MongoDB, even with an old USE_MONGO=0 env.
export USE_MONGO=1

# Defaults confirmed from Compass. Explicit environment settings take priority.
export MONGO_HOST="${MONGO_HOST:-127.0.0.1}"
export MONGO_PORT="${MONGO_PORT:-27017}"
export MONGO_USERNAME="${MONGO_USERNAME:-admin}"
export MONGO_AUTH_SOURCE="${MONGO_AUTH_SOURCE:-admin}"
export MONGO_DB="${MONGO_DB:-live_reply_bot}"
export MONGO_COLLECTION="${MONGO_COLLECTION:-reply_examples_v2}"
export MONGO_DOCKER_CONTAINER="${MONGO_DOCKER_CONTAINER:-mongodb-rag}"

# Do not save passwords in this file. MONGO_URI still takes priority in Python.
# Mock/help runs must not ask for credentials or connect to the database.
json_test_needs_password=1
for json_test_arg in "$@"; do
    case "$json_test_arg" in
        --) break ;;
        --mock|-h|--help) json_test_needs_password=0 ;;
    esac
done

# Only borrow root credentials for the local/default Mongo endpoint. Never
# replace an explicit URI/password or use these credentials for another host.
if [ "$json_test_needs_password" = 1 ] && [ -z "${MONGO_URI:-}" ] && [ -z "${MONGO_PASSWORD:-}" ] &&
   { [ "$MONGO_HOST" = "127.0.0.1" ] || [ "$MONGO_HOST" = "localhost" ]; } &&
   [ "$MONGO_PORT" = "27017" ] && [ "$MONGO_AUTH_SOURCE" = "admin" ] && command -v docker >/dev/null 2>&1; then
    # Capture stdout inside this process, never print Docker's environment or
    # put a password in argv/a file. JSON parsing preserves special characters.
    if MONGO_PASSWORD=$("$json_test_root/.venv-cls/bin/python" - <<'PY'
import json
import os
import subprocess
import sys

try:
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{json .Config.Env}}", os.environ["MONGO_DOCKER_CONTAINER"]],
        capture_output=True, text=True, timeout=5, check=True,
    )
    entries = json.loads(result.stdout)
    if not isinstance(entries, list) or not all(isinstance(item, str) for item in entries):
        raise ValueError("Invalid container environment")
    settings = dict(item.split("=", 1) for item in entries if "=" in item)
    # Atlas Local uses MONGODB_; the official mongo image uses MONGO_.
    for prefix in ("MONGODB_INITDB_ROOT_", "MONGO_INITDB_ROOT_"):
        password = settings.get(prefix + "PASSWORD", "")
        if settings.get(prefix + "USERNAME") == os.environ["MONGO_USERNAME"] and password:
            # Shell command substitution removes trailing newlines; decline
            # these unusual credentials rather than silently changing them.
            if "\n" in password or "\r" in password or "\0" in password:
                break
            sys.stdout.write(password)
            sys.exit(0)
except (OSError, ValueError, subprocess.SubprocessError):
    pass
sys.exit(1)
PY
    ); then
        export MONGO_PASSWORD
    fi
fi

if [ "$json_test_needs_password" = 1 ] && [ -z "${MONGO_URI:-}" ] && [ -z "${MONGO_PASSWORD:-}" ]; then
    printf '%s\n' '未能自動取得 MongoDB 密碼；請確認 Docker Desktop／容器名稱，或提供連線帳密。' >&2
    if [ ! -t 0 ]; then
        printf '%s\n' '尚未設定 MongoDB 密碼。請在終端機直接執行以隱藏輸入密碼，或先設定 MONGO_PASSWORD／MONGO_URI。' >&2
        exit 1
    fi
    if ! IFS= read -r -s -p "MongoDB 密碼（帳號 ${MONGO_USERNAME}）： " MONGO_PASSWORD; then
        printf '\n%s\n' '未取得密碼，已取消啟動。' >&2
        exit 1
    fi
    printf '\n'
    if [ -z "$MONGO_PASSWORD" ]; then
        printf '%s\n' '密碼不能為空，已取消啟動。' >&2
        exit 1
    fi
    export MONGO_PASSWORD
fi

# Keep model output in this terminal; all extra CLI options are forwarded.
exec "$json_test_root/.venv-cls/bin/python" -u "$json_test_root/replay_json.py" \
    --style "${STYLE_ID:-short}" --temperature 0 "$@"
