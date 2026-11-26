"""内容审核：零依赖 DFA 敏感词过滤。

词库来源（按优先级）：
  1. 外置覆盖：instance/sensitive_words.txt（运维放生产词库，git / deploy 均不碰）
  2. 内置种子：项目根目录 sensitive_words.txt（MIT 词库裁剪版，仅明确拒绝类）

匹配范围：标题 + 描述 + HTML 可见文本（去 script/style/标签/转义），不扫源码属性名。
"""
import os
import re
import logging
from html import unescape

log = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
SEED_WORDS = os.path.join(PROJECT_ROOT, 'sensitive_words.txt')

# 当前词库来源：external / seed / none，供运维诊断（rescan.py 会打印）
_loaded_source = 'none'


def _external_path():
    """外置词库路径与 Flask instance 目录保持一致（默认 instance/）。"""
    instance = os.environ.get('GALLERY_INSTANCE', os.path.join(PROJECT_ROOT, 'instance'))
    return os.path.join(instance, 'sensitive_words.txt')


def _read_word_file(path):
    """读一份词库；读取或解码失败由调用方捕获，不在本函数吞错。"""
    words = set()
    with open(path, encoding='utf-8') as f:
        for line in f:
            word = line.strip()
            if word and not word.startswith('#'):
                words.add(word)
    return words


def load_words():
    """加载词库：外置优先；外置读取失败告警并回退内置种子。

    两级都失败时返回空集（审核静默失效）但留 critical 日志——不 fail-closed
    以免词库文件问题导致全站无法发布，但绝不再静默吞错。
    """
    global _loaded_source
    external = _external_path()
    if os.path.exists(external):
        try:
            words = _read_word_file(external)
            if not words:
                log.warning('外置词库为空，审核将失效：%s', external)
            _loaded_source = 'external'
            return words
        except (OSError, UnicodeError) as e:
            log.error('外置词库读取失败，回退内置种子库：%s（%s）', external, e)
    try:
        words = _read_word_file(SEED_WORDS)
        if not words:
            log.warning('内置种子词库为空，审核将失效：%s', SEED_WORDS)
        _loaded_source = 'seed'
        return words
    except (OSError, UnicodeError) as e:
        log.critical('内置种子词库也无法读取，敏感词审核已失效：%s（%s）', SEED_WORDS, e)
        _loaded_source = 'none'
        return set()


class DFA:
    """Trie + 逐位置匹配，O(n * 最长词长)；命中立即返回。"""
    END = '\x00END'

    def __init__(self, words):
        self.root = {}
        for word in words:
            node = self.root
            for ch in word:
                node = node.setdefault(ch, {})
            node[self.END] = True
        self.max_len = max((len(w) for w in words), default=0)

    def hit(self, text):
        """返回首个命中词，未命中返回 None。"""
        if not text or not self.max_len:
            return None
        text = str(text)
        for start in range(len(text)):
            node = self.root
            for i in range(start, min(start + self.max_len, len(text))):
                ch = text[i]
                if ch not in node:
                    break
                node = node[ch]
                if self.END in node:
                    return text[start:i + 1]
        return None


# 模块级单例：首次调用时构建，避免每次请求重建 DFA
_matcher = None
_loaded_words_count = 0


def get_matcher():
    global _matcher, _loaded_words_count
    if _matcher is None:
        words = load_words()
        _loaded_words_count = len(words)
        _matcher = DFA(words)
    return _matcher


def reload_words():
    """词库更新后手动重建（重扫脚本调用）。"""
    global _matcher, _loaded_words_count
    words = load_words()
    _loaded_words_count = len(words)
    _matcher = DFA(words)
    return _loaded_words_count


_TAG_RE = re.compile(r'<(script|style)\b[^>]*>.*?</\1\s*>', re.I | re.S)
_ANY_TAG_RE = re.compile(r'<[^>]*>')


def visible_text(html):
    """提取 HTML 可见文本：去 script/style/标签，反转义实体，压缩空白。"""
    text = _TAG_RE.sub(' ', html or '')
    text = _ANY_TAG_RE.sub(' ', text)
    text = unescape(text)
    return re.sub(r'\s+', ' ', text).strip()


def scan(title='', description='', html=''):
    """扫描标题 + 描述 + HTML 可见文本。

    返回 (是否命中, 命中词, 命中字段)；无命中返回 (False, None, None)。
    """
    matcher = get_matcher()
    for field, value in (('标题', title), ('描述', description), ('作品内容', visible_text(html))):
        if not value:
            continue
        hit = matcher.hit(str(value))
        if hit:
            return True, hit, field
    return False, None, None
