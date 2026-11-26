"""动态 OG 封面图（SEO P2，2026-11-27）。

为作品页生成 1200x630 的分享封面：浅色渐变底 + 站点名 + 作品标题（自动折行）
+ 作者署名。纯 Pillow 生成，不依赖外部渲染服务或字体下载。
字体优先用服务器自带的文泉驿微米黑（中文字体），找不到则退回 DejaVu。
"""
import io
import os
import re

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 1200, 630
_FONT_CANDIDATES = [
    '/usr/share/fonts/truetype/wqy/wqy-microhei.ttc',
    '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
    '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
]


def _font(size):
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _wrap(draw, text, font, max_width):
    """按像素宽度折行，最多 3 行，超出用省略号。"""
    text = re.sub(r'\s+', ' ', (text or '').strip()) or '未命名作品'
    lines, cur = [], ''
    for ch in text:
        trial = cur + ch
        if draw.textlength(trial, font=font) <= max_width:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = ch
            if len(lines) == 3:
                break
    if cur and len(lines) < 3:
        lines.append(cur)
    if len(lines) == 3:
        last = lines[2]
        while draw.textlength(last + '…', font=font) > max_width and last:
            last = last[:-1]
        lines[2] = last.rstrip() + '…'
    return lines


def generate(title, author='', site='HTML Gallery'):
    """返回 PNG 字节。任何异常由调用方兜底到静态默认图。"""
    img = Image.new('RGB', (WIDTH, HEIGHT))
    d = ImageDraw.Draw(img)
    # 竖向渐变：浅靛蓝 → 浅紫，文字始终深色，社交平台缩略图仍清晰
    top, bottom = (244, 246, 255), (232, 234, 250)
    for y in range(HEIGHT):
        t = y / HEIGHT
        rgb = tuple(int(top[i] * (1 - t) + bottom[i] * t) for i in range(3))
        d.line([(0, y), (WIDTH, y)], fill=rgb)

    brand_font = _font(34)
    title_font = _font(66)
    sub_font = _font(30)

    d.rounded_rectangle((64, 56, 78, 106), radius=7, fill=(91, 106, 240))
    d.text((98, 58), site, font=brand_font, fill=(38, 42, 78))

    lines = _wrap(d, title, title_font, WIDTH - 128)
    y = 190
    for line in lines:
        d.text((64, y), line, font=title_font, fill=(26, 28, 56))
        y += 92

    if author:
        d.text((64, HEIGHT - 92), 'by ' + author[:40], font=sub_font, fill=(96, 100, 138))

    buf = io.BytesIO()
    img.save(buf, format='PNG', optimize=True)
    return buf.getvalue()
