#!/usr/bin/env bash
# gallery.njit.top 生产部署脚本
# 用法: ./deploy.sh            同步代码 → 备份 → 重启 → 健康检查
#       ./deploy.sh --dry-run  只预演同步，不备份、不重启
set -euo pipefail

# 服务器连接信息从 .deploy.env 读取（该文件不入库），或直接导出环境变量
#   .deploy.env 内容示例：GALLERY_HOST="<用户名@IP>"  GALLERY_SSH_PORT="<端口>"
ENV_FILE="$(cd "$(dirname "$0")" && pwd)/.deploy.env"
if [ -f "$ENV_FILE" ]; then
  # shellcheck disable=SC1090
  source "$ENV_FILE"
fi
HOST="${GALLERY_HOST:-}"
PORT="${GALLERY_SSH_PORT:-22}"
if [ -z "$HOST" ]; then
  echo "✗ 未配置服务器地址：请创建 .deploy.env 写入 GALLERY_HOST 和 GALLERY_SSH_PORT" >&2
  exit 1
fi
REMOTE_DIR="${GALLERY_REMOTE_DIR:-/www/wwwroot/gallery_njit_top}"
LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"
SSH="ssh -p $PORT -o ConnectTimeout=10 $HOST"
DRY_RUN="${1:-}"

cd "$LOCAL_DIR"

# 只同步代码与文档，绝不覆盖生产资产
EXCLUDES=(
  --exclude 'venv/' --exclude 'instance/'
  --exclude '.env' --exclude '.deploy.env'
  --exclude 'gallery.log' --exclude '.deploy-backup/'
  --exclude '__pycache__/' --exclude '*.pyc' --exclude '.git/'
  --exclude '.DS_Store' --exclude '*.md' --exclude 'deploy.sh'
)

echo "==> [1/5] 检查服务器连通"
$SSH "true"

if [ "$DRY_RUN" = "--dry-run" ]; then
  echo "==> [DRY-RUN] 预演同步文件列表"
  rsync -avzn -e "ssh -p $PORT" "${EXCLUDES[@]}" \
    "$LOCAL_DIR/" "$HOST:$REMOTE_DIR/"
  echo "==> 预演完成（未做任何改动）"
  exit 0
fi

echo "==> [2/5] 备份线上代码（py + templates + static + 词库）"
$SSH "cd '$REMOTE_DIR' && mkdir -p .deploy-backup && \
  EXTRA=''; for f in moderation.py rescan.py link_import.py metrics.py sensitive_words.txt; do [ -e \"\$f\" ] && EXTRA=\"\$EXTRA \$f\"; done; \
  tar czf \".deploy-backup/code-\$(date +%Y%m%d-%H%M%S).tar.gz\" \
  app.py templates static \$EXTRA && \
  echo '备份已存 .deploy-backup/'"

echo "==> [3/5] 同步代码到服务器"
rsync -avz -e "ssh -p $PORT" "${EXCLUDES[@]}" \
  "$LOCAL_DIR/" "$HOST:$REMOTE_DIR/"

echo "==> [4/5] 修正属主 + 语法检查 + 重启"
$SSH "cd '$REMOTE_DIR' && \
  chown www:www *.py templates/*.html && \
  ./venv/bin/python -c \"import ast; ast.parse(open('app.py', encoding='utf-8').read()); ast.parse(open('moderation.py', encoding='utf-8').read()); ast.parse(open('link_import.py', encoding='utf-8').read()); print('  语法 OK')\" && \
  systemctl restart gallery && sleep 2 && \
  echo \"  service: \$(systemctl is-active gallery)\""

echo "==> [5/5] 健康检查"
if $SSH "curl -fsS -o /dev/null http://127.0.0.1:5050/"; then
  echo "  本地 5050: OK"
else
  echo "  ✗ 本地 5050 无响应，请检查 journalctl -u gallery -n 20"
  exit 1
fi
if curl -ksS -o /dev/null -m 12 https://gallery.njit.top/; then
  echo "  公网反代: OK"
else
  echo "  ⚠ 公网反代未通过（可能 DNS/证书/反代配置问题，服务本身正常）"
fi

echo "==> 部署完成"
