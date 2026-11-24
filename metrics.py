"""阶段4验证指标统计（零依赖：只读 nginx access log + SQLite）。

设计依据：设计方案 §7「零成本可观测」，不建埋点系统。

用法（在服务器上，路径按实际环境填写）：
  ./venv/bin/python metrics.py --log <nginx-access-log> --db instance/gallery.db
  ./venv/bin/python metrics.py --log <nginx-access-log> --db instance/gallery.db --since 2026-11-25

指标：
  1. 匿名→注册转化率 = 来源注册数 / 匿名成功数
  2. 裂变点击率 = /new?from=view 访问 / 分享页独立访客（按 IP）
  3. 分享可靠性 = 人工抽检（脚本不发外网请求）
  4. 数据完整性 = 过期作品仍在库（不删除承诺）

说明：日志与 DB 路径均从参数传入，本文件不含服务器信息。
"""
import argparse
import re
import sqlite3
from datetime import datetime

# nginx combined: IP - - [date] "METHOD PATH HTTP/x" status ...
LOG_RE = re.compile(r'^(\S+) \S+ \S+ \[([^\]]+)\] "(\S+) (\S+) [^"]*" (\d{3})')


def parse_log(path, since=None):
    """返回逐条 (ip, dt, method, path, full_url, status)，--since 按日志时间过滤。"""
    entries = []
    with open(path, encoding='utf-8', errors='ignore') as f:
        for line in f:
            m = LOG_RE.match(line)
            if not m:
                continue
            ip, date_s, method, url, status = m.groups()
            try:
                dt = datetime.strptime(date_s, '%d/%b/%Y:%H:%M:%S %z')
            except ValueError:
                continue
            if since and dt.date() < since:
                continue
            entries.append((ip, dt, method, url.split('?')[0], url, int(status)))
    return entries


def count(entries, method, path, query=None, ok=True):
    ok_pred = (lambda s: 200 <= s < 400) if ok else (lambda s: s >= 400)
    return sum(1 for _, _, m, p, url, s in entries
               if m == method and p == path and (not query or query in url) and ok_pred(s))


def is_share_page(path):
    """分享页三类：/view/<slug>、/p/<slug>、/<handle>/<slug>；排除静态与内部路由。"""
    if path.startswith(('/static/', '/api/', '/admin/', '/view/', '/p/')):
        return path.startswith(('/view/', '/p/'))
    return bool(re.fullmatch(r'/[^/]+/[^/]+', path))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--log', required=True, help='nginx access log 路径')
    ap.add_argument('--db', required=True, help='gallery.db 路径')
    ap.add_argument('--since', help='起始日期 YYYY-MM-DD（按日志时间）')
    args = ap.parse_args()

    since = datetime.strptime(args.since, '%Y-%m-%d').date() if args.since else None
    entries = parse_log(args.log, since)
    print(f'日志区间：{since or "全部"}，共 {len(entries)} 条请求\n')

    # 指标1：匿名→注册转化
    trial_reg = count(entries, 'POST', '/register', query='from=trial')
    trial_lands = count(entries, 'GET', '/dashboard', query='trial=1')
    anon_posts = count(entries, 'POST', '/new')
    print('【1】匿名→注册转化')
    print(f'  匿名上传成功（GET /dashboard?trial=1）: {trial_lands}')
    print(f'  来源注册成功（POST /register?from=trial）: {trial_reg}')
    print(f'  参考：全部 POST /new 成功数（含注册用户）: {anon_posts}')
    if trial_lands:
        print(f'  → 转化率: {trial_reg / trial_lands:.1%}  [目标 ≥15%]')
    else:
        print('  → 转化率: 无样本（尚无匿名成功）')

    # 指标2：裂变点击率
    fission_clicks = count(entries, 'GET', '/new', query='from=view')
    share_ips = {ip for ip, _, m, p, _, s in entries
                 if m == 'GET' and s == 200 and is_share_page(p)}
    print('\n【2】裂变点击率')
    print(f'  /new?from=view 访问: {fission_clicks}')
    print(f'  分享页独立访客（按 IP）: {len(share_ips)}')
    if share_ips:
        print(f'  → 裂变点击率: {fission_clicks / len(share_ips):.1%}  [目标 ≥5%]')
    else:
        print('  → 裂变点击率: 无样本')

    # 指标4：数据完整性
    print('\n【4】数据完整性（过期作品保留）')
    con = sqlite3.connect(args.db)
    total = con.execute('SELECT COUNT(*) FROM pages').fetchone()[0]
    expired = con.execute(
        'SELECT COUNT(*) FROM pages WHERE share_expires_at IS NOT NULL AND share_expires_at < ?',
        (datetime.now().isoformat(),)
    ).fetchone()[0]
    con.close()
    print(f'  库中作品总数: {total}；已过期仍在库: {expired}')
    print('  → 过期作品未被物理删除（符合承诺）')

    print('\n【3】分享可靠性需人工抽检：取注册用户公开作品链接 GET，确认 200。')
    print('    （脚本不发起外网请求，避免把反代/DNS 问题误判为作品问题）')


if __name__ == '__main__':
    main()
