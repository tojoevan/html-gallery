#!/usr/bin/env bash
# 异地备份新鲜度检查：offsite.ok 的时间戳超过 26h 即告警。
# 防止「备份脚本静默失败 + 旧文件被当新鲜」的隐蔽断供（pyWork 教训）。
set -euo pipefail

ENV_FILE="${ENV_FILE:-/etc/gallery-backup/ntfy.env}"
OK_FILE="${OK_FILE:-/var/lib/gallery-backup/offsite.ok}"
MAX_AGE_HOURS="${MAX_AGE_HOURS:-26}"

_env_get() {
  [ -f "$ENV_FILE" ] || return 0
  grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '\r"' || true
}

[ -f "$OK_FILE" ] || {
  echo "offsite.ok 不存在，异地备份从未成功过" >&2
  exit 1
}
AT=$(head -1 "$OK_FILE")
TS=$(date -j -f '%Y-%m-%dT%H:%M:%SZ' "$AT" +%s 2>/dev/null || date -d "$AT" +%s 2>/dev/null || echo 0)
[ "$TS" -gt 0 ] || { echo "offsite.ok 时间格式异常: $AT" >&2; exit 1; }
AGE_H=$(( ($(date +%s) - TS) / 3600 ))
if [ "$AGE_H" -gt "$MAX_AGE_HOURS" ]; then
  url=$(_env_get NTFY_URL); topic=$(_env_get NTFY_TOPIC); token=$(_env_get NTFY_TOKEN)
  if [ -n "$url" ] && [ -n "$topic" ]; then
    payload=$(printf '{"topic":"%s","title":"%s","message":"%s","priority":5,"tags":["rotating_light"]}' \
      "$topic" "画廊异地备份静默" "最近一次成功备份在 ${AGE_H} 小时前（阈值 ${MAX_AGE_HOURS}h），请检查定时任务与备份日志。")
    local_hdrs=(-H "Content-Type: application/json; charset=utf-8")
    [ -n "$token" ] && local_hdrs+=(-H "Authorization: Bearer $token")
    curl -fsS -m 10 -X POST "${local_hdrs[@]}" -d "$payload" "${url%/}/" >/dev/null 2>&1 || true
  fi
  echo "异地备份静默 ${AGE_H}h（阈值 ${MAX_AGE_HOURS}h）" >&2
  exit 1
fi
echo "异地备份新鲜度 ok：${AGE_H}h 前成功"
