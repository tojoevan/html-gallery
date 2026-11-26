"""存量作品敏感词重扫（幂等，可重复跑）。

用法：
  python3.11 rescan.py           # 只报告命中，不改库
  python3.11 rescan.py --apply   # 命中作品转 pending_review（对外 404）

词库更新后执行；外置词库放 instance/sensitive_words.txt 会覆盖内置种子库。
"""
import os
import sqlite3
import sys

import moderation

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'instance', 'gallery.db')


def main():
    apply = '--apply' in sys.argv
    count = moderation.reload_words()
    source = {'external': '外置', 'seed': '内置种子',
              'none': '无（审核已失效）'}.get(moderation._loaded_source, '未知')
    print(f'词库加载: {count} 词（来源: {source}）')
    if not os.path.exists(DB):
        print('数据库不存在:', DB)
        return
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    rows = db.execute("SELECT id, title, description, html_source, review_status FROM pages").fetchall()
    hits = []
    for row in rows:
        hit, word, field = moderation.scan(row['title'], row['description'], row['html_source'])
        if hit:
            hits.append((row['id'], row['title'], field, word, row['review_status']))
    print(f'扫描 {len(rows)} 个作品，命中 {len(hits)} 个')
    for page_id, title, field, word, status in hits:
        print(f'  #{page_id} [{status}] {title[:30]} — {field}命中「{word}」')
    if apply and hits:
        ids = [h[0] for h in hits]
        placeholders = ','.join('?' for _ in ids)
        db.execute(f"UPDATE pages SET review_status='pending_review' WHERE id IN ({placeholders})", ids)
        db.commit()
        print(f'已将 {len(ids)} 个命中作品转入复审')
    db.close()


if __name__ == '__main__':
    main()
