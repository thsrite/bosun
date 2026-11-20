#!/bin/sh
# Forced SSH command: only fixed SQLite SELECTs and bounded reads of known logs.
# Parsing, correlation and storage run on the monitoring server, not these hosts.
set -eu
set -f
export LC_ALL=C
role=${1:-}
set -- ${SSH_ORIGINAL_COMMAND:-}
[ "$#" -ge 1 ] || exit 64
command=$1
shift
number() { case "$1" in ''|*[!0-9]*) exit 64;; esac; [ "${#1}" -le 16 ] || exit 64; }
chunk() {
    file=$1
    offset=$2
    number "$offset"
    [ -f "$file" ] && [ ! -L "$file" ] || exit 66
    if [ "$role" = mac ]; then
        /usr/bin/stat -f '%i %z %m' "$file"
    else
        /usr/bin/stat -c '%i %s %Y' "$file"
    fi
    # tail seeks to the byte offset; head caps traffic without scanning the file.
    /usr/bin/tail -c "+$((offset + 1))" "$file" | /usr/bin/head -c 1048576
}
case "$role:$command" in
mac:tasks)
    [ "$#" -eq 0 ] || exit 64
    exec /usr/bin/sqlite3 -readonly -json -cmd '.timeout 2000' "$HOME/.bosun/bosun.db" "SELECT id,status,session_uid,started_at,ended_at FROM task WHERE engine='omp' AND deleted=0 AND session_uid IS NOT NULL AND (status IN ('running','waiting','queued','paused') OR ended_at >= CAST(strftime('%s','now') AS INTEGER)-86400) ORDER BY id DESC LIMIT 24;"
    ;;
mac:session)
    [ "$#" -eq 2 ] || exit 64
    uid=$1
    offset=$2
    [ "${#uid}" -eq 36 ] || exit 64
    case "$uid" in *[!0-9a-f-]*) exit 64;; esac
    number "$offset"
    # Expand only this fixed directory and a validated UUID; no caller-supplied path.
    set +f
    found=
    for file in "$HOME"/.omp/agent/sessions/*/*_"$uid".jsonl; do
        [ -f "$file" ] || continue
        [ -z "$found" ] || exit 65
        found=$file
    done
    [ -n "$found" ] || exit 66
    chunk "$found" "$offset"
    ;;
cpa:usage)
    [ "$#" -eq 1 ] || exit 64
    number "$1"
    exec /usr/bin/sqlite3 -readonly -json -cmd '.timeout 2000' /mnt/user/appdata/cliproxyapi/auth/usage-report.sqlite "SELECT id,json_extract(payload,'\$.timestamp') AS timestamp,json_extract(payload,'\$.model') AS model,json_extract(payload,'\$.source') AS source,json_extract(payload,'\$.tokens.input_tokens') AS input_tokens,json_extract(payload,'\$.tokens.output_tokens') AS output_tokens,json_extract(payload,'\$.tokens.cached_tokens') AS cache_tokens,json_extract(payload,'\$.tokens.reasoning_tokens') AS reasoning_tokens,json_extract(payload,'\$.latency_ms') AS duration_ms,json_extract(payload,'\$.ttft_ms') AS ttft_ms,json_extract(payload,'\$.failed') AS failed FROM usage_events WHERE id > $1 AND ts >= datetime('now','+8 hours','-1 day') ORDER BY id LIMIT 2000;"
    ;;
cpa:log)
    [ "$#" -eq 1 ] || exit 64
    chunk /mnt/user/appdata/cliproxyapi/auth/logs/main.log "$1"
    ;;
*) exit 64;;
esac
