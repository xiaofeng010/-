# -*- coding: utf-8 -*-
"""
lrcutil.py —— 网易云歌词转换工具

touchuan 接口 (/api/getSongLyric) 返回的歌词有两种形态：
  1) 新版逐字 JSON：每行一个 JSON 对象，形如
     {"t":17830,"c":[{"tx":"从前从前"},{"tx":"有个人爱你很久"}]}
     （t 为毫秒，c 为片段数组，第二个起常带 li/or 富文本链接）
  2) 直接就是标准 LRC 文本，例如 "[00:05.00]纯音乐，请欣赏"

本模块把两种形态统一转成标准 LRC / SRT 文本，并支持把翻译歌词合并成
"原文（译文）" 的形式，同时过滤作词/作曲/编曲等元数据头。
"""

import json
import re

__all__ = ["to_lrc", "to_srt", "merge_translation", "parse_lines", "clean_metadata"]

_LRC_TIME = re.compile(r"^\s*\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
# 元数据标签（中英文常见）
_META_KEYS = (
    "作词", "作曲", "编曲", "制作人", "混音", "母带", "监制", "录音", "和声", "吉他",
    "贝斯", "鼓", "钢琴", "弦乐", "统筹", "企划", "出品", "发行", "OP", "SP", "词",
    "曲", "演唱", "原唱", "翻唱", "策划", "封面", "文案", "特别感谢", "版权",
)


def _fmt_time(ms, sep="."):
    """毫秒 -> [mm:ss.xx]"""
    if ms is None or ms < 0:
        ms = 0
    total_cs = int(round(ms / 10.0))
    m, rem = divmod(total_cs, 6000)
    s, cs = divmod(rem, 100)
    return "[%02d:%02d%s%02d]" % (m, s, sep, cs)


def _is_metadata(text):
    t = text.strip()
    if not t:
        return False
    for k in _META_KEYS:
        if t.startswith(k + ":") or t.startswith(k + "：") or t.startswith("[" + k):
            return True
    return False


def parse_lines(raw):
    """把接口返回的歌词文本解析为 [(时间毫秒, 文本), ...]（按时间排序）。"""
    if not raw:
        return []
    raw = raw.strip()
    if not raw:
        return []

    # 形态 2：标准 LRC 文本
    if raw.startswith("[") and not raw.startswith('[{"'):
        out = []
        for line in raw.splitlines():
            m = _LRC_TIME.match(line)
            if not m:
                continue
            mm, ss, frac = m.group(1), m.group(2), m.group(3) or "0"
            if len(frac) == 1:
                frac_ms = int(frac) * 100
            elif len(frac) == 2:
                frac_ms = int(frac) * 10
            else:
                frac_ms = int(frac[:3])
            text = line[m.end():].strip()
            out.append((int(mm) * 60000 + int(ss) * 1000 + frac_ms, text))
        out.sort(key=lambda x: x[0])
        return out

    # 形态 1：逐行 JSON
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            # 容错：按标准 LRC 处理该行
            m = _LRC_TIME.match(line)
            if m:
                out.append((int(m.group(1)) * 60000 + int(m.group(2)) * 1000, line[m.end():].strip()))
            continue
        t = obj.get("t", 0)
        chunks = obj.get("c") or []
        text = "".join((c.get("tx") or "") for c in chunks if isinstance(c, dict)).strip()
        if text:
            out.append((int(t), text))
    out.sort(key=lambda x: x[0])
    return out


def clean_metadata(lines, keep_metadata=False):
    """过滤掉作词/作曲等元数据行（默认只删除开头的连续元数据，避免误删正文）。"""
    if keep_metadata:
        return lines
    out = []
    for i, (t, txt) in enumerate(lines):
        if i < 12 and _is_metadata(txt):
            continue
        out.append((t, txt))
    return out


def merge_translation(orig_lines, trans_lines):
    """把翻译按时间就近合并进原文：'原文（译文）'。"""
    if not trans_lines:
        return orig_lines
    merged = []
    for t, txt in orig_lines:
        best, best_d = None, 10 ** 9
        for tt, ttxt in trans_lines:
            d = abs(tt - t)
            if d < best_d:
                best, best_d = ttxt, d
        if best and best_d <= 2500 and best.strip() != txt.strip():
            merged.append((t, "%s（%s）" % (txt, best)))
        else:
            merged.append((t, txt))
    return merged


def to_lrc(lines, offset_ms=0):
    """生成标准 LRC 文本。"""
    out = []
    for t, txt in lines:
        out.append("%s%s" % (_fmt_time(t + offset_ms), txt))
    return "\n".join(out) + ("\n" if out else "")


def to_srt(lines):
    """生成 SRT 字幕文本。"""

    def ts(ms):
        ms = max(0, int(ms))
        h, rem = divmod(ms, 3600000)
        m, rem = divmod(rem, 60000)
        s, ms = divmod(rem, 1000)
        return "%02d:%02d:%02d,%03d" % (h, m, s, ms)

    out = []
    for i, (t, txt) in enumerate(lines, 1):
        end = lines[i][0] if i < len(lines) else t + 4000
        if end <= t:
            end = t + 2000
        out.append("%d\n%s --> %s\n%s\n" % (i, ts(t), ts(end), txt))
    return "\n".join(out)


def build(raw_lrc, raw_tlyric="", raw_yrc="", include_translation=True):
    """一站式：返回 (lrc文本, srt文本, 行数)。"""
    main_raw = raw_yrc if (raw_yrc and not raw_lrc) else raw_lrc
    lines = clean_metadata(parse_lines(main_raw))
    if include_translation and raw_tlyric:
        lines = merge_translation(lines, parse_lines(raw_tlyric))
    return to_lrc(lines), to_srt(lines), len(lines)


if __name__ == "__main__":
    demo = ('{"t":0,"c":[{"tx":"作词: "},{"tx":"米果"}]}\n'
            '{"t":1000,"c":[{"tx":"作曲: "},{"tx":"高橋優"}]}\n'
            '{"t":17830,"c":[{"tx":"我在"},{"tx":"十平米的"}]}\n'
            '{"t":21000,"c":[{"tx":"房间里"}]}\n')
    lrc, srt, n = build(demo, '{"t":17830,"c":[{"tx":"I am in a ten square meter room"}]}')
    print(lrc)
    print(srt)
    print('lines:', n)
