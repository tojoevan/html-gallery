#!/usr/bin/env bash
# gallery.njit.top 异地备份：SQLite 一致性快照 → 推到 backups.oahubs.com（项目 gallery）→ 读键拉回比 sha256。
# 失败即告警：任何非零退出都推 ntfy（topic gallery），不搞状态机——静默失败会让旧文件被当成新鲜备份。
set -euo pipefail

ENV_FILE="${ENV_FILE:-/etc/gallery-backup/ntfy.env}"
_env_get() {
  [ -f "$ENV_FILE" ] || return 0
  grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '\r"' || true
}
notify() {
  local title="$1" msg="$2" tags="${3:-}" url topic token payload
  url=$(_env_get NTFY_URL); topic=$(_env_get NTFY_TOPIC); token=$(_env_get NTFY_TOKEN)
  if [ -n "$url" ] && [ -n "$topic" ]; then
    local tags_json=''
    [ -n "$tags" ] && tags_json=",\"tags\":[\"$tags\"]"
    payload=$(printf '{"topic":"%s","title":"%s","message":"%s","priority":%s%s}' \
      "$topic" "$title" "$msg" "${NTFY_PRIORITY:-5}" "$tags_json")
    local -a hdrs=(-H "Content-Type: application/json; charset=utf-8")
    [ -n "$token" ] && hdrs+=(-H "Authorization: Bearer $token")
    curl -fsS -m 10 -X POST "${hdrs[@]}" -d "$payload" "${url%/}/" >/dev/null 2>&1 || true
  fi
}
on_exit() {
  local code=$?
  if [ "$code" -ne 0 ]; then
    notify "画廊异地备份失败" "备份脚本异常退出（exit=${code}），请登录 156.254.6.7 检查备份日志。" "rotating_light"
  fi
}
trap on_exit EXIT

# ── 配置 ─────────────────────────────────────────────
ROOT="${ROOT:-/www/wwwroot/gallery_njit_top}"
DB="$ROOT/instance/gallery.db"
LOCAL_DIR="${LOCAL_DIR:-/var/backups/gallery}"
KEYDIR="${KEYDIR:-/etc/gallery-backup/keys}"
HUB="${HUB:-https://backup.oahubs.com}"   # 数据面域名，勿改成控制面 backups.oahubs.com
PROJECT=gallery
OK_FILE="${OK_FILE:-/var/lib/gallery-backup/offsite.ok}"
KEEP="${KEEP:-14}"
MAX_AGE_HOURS="${MAX_AGE_HOURS:-26}"

[ -f "$DB" ] || { echo "数据库不存在: $DB" >&2; exit 1; }
[ -f "$KEYDIR/bkp-gallery-write" ] || { echo "缺少写键" >&2; exit 1; }
[ -f "$KEYDIR/bkp-gallery-read" ] || { echo "缺少读键" >&2; exit 1; }
mkdir -p "$LOCAL_DIR" "$(dirname "$OK_FILE")"

# ── 1. 本地一致性快照（SQLite online backup，避免直接 cp 热库）─────────────
STAMP=$(date +%Y%m%d-%H%M%S)
SNAP="$LOCAL_DIR/gallery_${STAMP}.db"
"$ROOT/venv/bin/python" - "$DB" "$SNAP" <<'PY'
import sqlite3, sys
src = sqlite3.connect('file:' + sys.argv[1] + '?mode=ro', uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
ok = dst.execute('PRAGMA integrity_check').fetchone()[0]
dst.close(); src.close()
if ok != 'ok':
    raise SystemExit('integrity_check 失败: ' + str(ok))
PY
chmod 600 "$SNAP"

# 保留最近 KEEP 份（只清本地快照；接收端只追加，清理由备份机按保留天数做）
ls -1t "$LOCAL_DIR"/gallery_*.db 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do
  rm -f "$old"
done

# ── 2. 推送 + 读键拉回校验（bkp-agent 内含 sha256 比对）────────────────────
export BKP_KEYDIR="$KEYDIR"
/usr/local/bin/bkp-agent push --project "$PROJECT" --hub "$HUB" "$SNAP"

# ── 3. 新鲜度标记（cron 每小时检查 >26h 即告警）───────────────────────────
SHA=$(sha256sum "$SNAP" | cut -d' ' -f1)
printf '%s\n%s  %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$SHA" "$(basename "$SNAP")" > "$OK_FILE.tmp"
mv -f "$OK_FILE.tmp" "$OK_FILE"
echo "offsite ok: $(basename "$SNAP")"
