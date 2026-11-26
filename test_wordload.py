"""词库加载容错测试：外置失败回退内置、双重失败留痕、来源状态可查。"""
import io
import os
import tempfile
import logging
import unittest.mock as mock

TMP = tempfile.mkdtemp(prefix='gallery-wordload-')
os.environ['FLASK_DEBUG'] = '0'
os.environ['GALLERY_INSTANCE'] = TMP

import moderation

results = []


def check(name, cond, detail=''):
    results.append((name, cond, detail))
    print(('PASS' if cond else 'FAIL'), name, detail)


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


cap = Capture()
moderation.log.addHandler(cap)
moderation.log.setLevel(logging.DEBUG)

EXTERNAL = os.path.join(TMP, 'sensitive_words.txt')
SEED = os.path.join(TMP, 'seed_words.txt')
with open(SEED, 'w', encoding='utf-8') as f:
    f.write('# 种子\n内置敏感词\n')


def levels():
    return [r.levelno for r in cap.records]


def clear():
    cap.records.clear()


# ── 1. 正常：外置存在且可读 → 只用外置，无告警 ──────────────────────────────
with open(EXTERNAL, 'w', encoding='utf-8') as f:
    f.write('# 注释\n外置敏感词\n')
with mock.patch.object(moderation, 'SEED_WORDS', SEED):
    words = moderation.load_words()
check('外置词库优先', '外置敏感词' in words and '内置敏感词' not in words, str(words))
check('来源标记 external', moderation._loaded_source == 'external', moderation._loaded_source)
check('正常路径无 error 日志', not any(l >= logging.ERROR for l in levels()))


# ── 2. 外置 OSError → 回退内置 + error 日志 ─────────────────────────────────
clear()
real_read = moderation._read_word_file


def fail_external(path):
    if path == EXTERNAL:
        raise OSError('模拟权限错误')
    return real_read(path)


with mock.patch.object(moderation, 'SEED_WORDS', SEED), \
        mock.patch.object(moderation, '_read_word_file', fail_external):
    words = moderation.load_words()
check('外置失败回退内置', '内置敏感词' in words and '外置敏感词' not in words, str(words))
check('回退来源 seed', moderation._loaded_source == 'seed', moderation._loaded_source)
check('记录 error 日志', any(l >= logging.ERROR for l in levels()), str(levels()))


# ── 3. 外置解码失败（非 UTF-8）→ 回退内置 + error 日志 ──────────────────────
clear()
with open(EXTERNAL, 'wb') as f:
    f.write('乱码词'.encode('gbk'))
with mock.patch.object(moderation, 'SEED_WORDS', SEED):
    words = moderation.load_words()
check('解码失败回退内置', '内置敏感词' in words, str(words))
check('解码失败记 error', any(l >= logging.ERROR for l in levels()), str(levels()))


# ── 4. 两级都失败 → 空集 + critical 日志 + 来源 none ────────────────────────
clear()


def fail_all(path):
    raise OSError('模拟全失败')


with mock.patch.object(moderation, 'SEED_WORDS', SEED), \
        mock.patch.object(moderation, '_read_word_file', fail_all):
    words = moderation.load_words()
check('双重失败返回空集', words == set(), str(words))
check('来源标记 none', moderation._loaded_source == 'none', moderation._loaded_source)
check('记录 critical 日志', any(l >= logging.CRITICAL for l in levels()), str(levels()))


# ── 5. 外置空文件 → warning（审核会失效）────────────────────────────────────
clear()
with open(EXTERNAL, 'w', encoding='utf-8') as f:
    f.write('# 只有注释，无词条\n')
with mock.patch.object(moderation, 'SEED_WORDS', SEED):
    words = moderation.load_words()
check('空词库返回空集', words == set(), str(words))
check('空词库记 warning', any(l >= logging.WARNING for l in levels()), str(levels()))


# ── 6. 兼容签名：load_words 仍返回 set，reload_words 仍返回 int ──────────────
with open(EXTERNAL, 'w', encoding='utf-8') as f:
    f.write('外置敏感词\n')
with mock.patch.object(moderation, 'SEED_WORDS', SEED):
    check('load_words 返回 set', isinstance(moderation.load_words(), set))
    n = moderation.reload_words()
check('reload_words 返回 int', isinstance(n, int), str(n))

moderation.log.removeHandler(cap)

failed = [n for n, c, _ in results if not c]
print('\n%d/%d passed' % (len(results) - len(failed), len(results)))
if failed:
    raise SystemExit('FAILED: ' + ', '.join(failed))
