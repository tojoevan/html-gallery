"""把 samples/*.html 作为公开示例作品写入画廊首页（幂等）。

用法：
  python seed_samples.py                  # 写入或修改示例作品
  python seed_samples.py --dry-run        # 只预览将要写入的内容，不动数据库
  python seed_samples.py --db <path>      # 指定数据库路径（默认 instance/gallery.db）

设计约束：
  - 示例归属 username='tojoevan'（按用户名查 id，不写死数字）
  - 固定 slug（前缀 sample-），重复运行只更新已有条目，不产生重复数据
  - 全部走 moderation.scan 审核，命中即跳过并报错，绝不入库违规内容
  - is_public=1 / review_status='approved' / share_expires_at=NULL（永久展示）
  - 只碰自己创建的 sample-* 作品，绝不修改用户真实作品
"""
import argparse
import os
import sqlite3
import sys
from datetime import datetime

import moderation

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
SAMPLES_DIR = os.path.join(PROJECT_ROOT, 'samples')
OWNER_USERNAME = 'tojoevan'
SLUG_PREFIX = 'sample-'

# 文件名 -> (标题, 描述, 固定 slug)
SAMPLES = [
    ('01-portfolio.html', '个人作品集 · 林间', '深色渐变风格的个人名片与项目展示', 'portfolio'),
    ('02-dashboard.html', '数据概览看板', '纯 CSS 绘制的运营数据仪表盘', 'dashboard'),
    ('03-birthday.html', '生日祝福卡', '带漂浮动画与蛋糕的祝福页面', 'birthday'),
    ('04-travel.html', '京都三日漫游', '时间线式旅行行程计划', 'travel'),
    ('05-todo.html', '极简待办清单', '可交互的前端小工具示例', 'todo'),
]


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument('--db', default=os.path.join(PROJECT_ROOT, 'instance', 'gallery.db'))
    p.add_argument('--dry-run', action='store_true')
    return p.parse_args()


def main():
    args = parse_args()
    if not os.path.isdir(SAMPLES_DIR):
        print('✗ 缺少 samples/ 目录')
        return 1

    # 预检：读取 + 审核，全部通过才动数据库（避免半途失败留下残缺数据）
    prepared = []
    for filename, title, description, slug_suffix in SAMPLES:
        path = os.path.join(SAMPLES_DIR, filename)
        if not os.path.exists(path):
            print('✗ 缺少样例文件:', filename)
            return 1
        html = open(path, encoding='utf-8').read()
        if len(html.encode('utf-8')) > 3 * 1024 * 1024:
            print('✗ 超过 3MB:', filename)
            return 1
        hit, word, field = moderation.scan(title, description, html)
        if hit:
            print('✗ 审核未通过:', filename, field, word)
            return 1
        prepared.append((title, description, SLUG_PREFIX + slug_suffix, html))

    if not os.path.exists(args.db):
        print('✗ 数据库不存在:', args.db)
        return 1

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    owner = db.execute('SELECT id FROM users WHERE username=?', (OWNER_USERNAME,)).fetchone()
    if not owner:
        print('✗ 找不到用户:', OWNER_USERNAME)
        db.close()
        return 1
    owner_id = owner['id']

    now = datetime.now().isoformat()
    created = updated = 0
    print(('[DRY-RUN] ' if args.dry_run else '') + '示例作品写入计划（归属用户 ' + OWNER_USERNAME + '）')
    for title, description, slug, html in prepared:
        existing = db.execute(
            'SELECT id FROM pages WHERE user_id=? AND slug=?', (owner_id, slug)
        ).fetchone()
        action = '更新' if existing else '新建'
        print('  ' + action + ': ' + title + '  [/' + slug + ']  ' + str(len(html)) + ' 字节')
        if args.dry_run:
            continue
        if existing:
            db.execute(
                '''UPDATE pages SET title=?, html_source=?, description=?, is_public=1,
                   review_status='approved', share_expires_at=NULL, updated=? WHERE id=?''',
                (title, html, description, now, existing['id'])
            )
            updated += 1
        else:
            db.execute(
                '''INSERT INTO pages (user_id, title, slug, html_source, description,
                   is_public, review_status, share_expires_at, created, updated)
                   VALUES (?,?,?,?,?,1,'approved',NULL,?,?)''',
                (owner_id, title, slug, html, description, now, now)
            )
            created += 1

    if not args.dry_run:
        db.commit()
        print('完成：新建 ' + str(created) + ' 个，更新 ' + str(updated) + ' 个')
    db.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
