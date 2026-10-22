#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用已实测通过的 URL 重新分类并生成清单（不重新测活，省时间）。

输入: test_out.m3u (refresh.py 产出的已验证清单)
输出: car-tv.m3u (修正分组与同台去重后)
"""
import re, sys, collections
sys.path.insert(0, '.')
import refresh as R

SRC = sys.argv[1] if len(sys.argv) > 1 else 'test_out.m3u'
OUT = sys.argv[2] if len(sys.argv) > 2 else 'car-tv.m3u'

rows = []
nm = g = ''
for l in open(SRC, encoding='utf-8').read().splitlines():
    l = l.strip()
    if not l:
        continue
    if l.startswith('#EXTINF'):
        nm = l.split(',', 1)[1]
        g = re.search(r'group-title="([^"]*)"', l).group(1)
    elif l.startswith('http'):
        rows.append((g, nm, l))

print('输入条目:', len(rows))

# 关键：输入里的名字可能已是中文名，而 classify() 认的是【英文原名】。
# 所以重新拉上游源池，建立 url -> 英文原名 的映射，用原名做分类。
url2name = {}
for u in R.UPSTREAMS:
    try:
        body, _ = R.http_get(u, 8_000_000, timeout=25)
        text = body.decode('utf-8', 'replace')
        if '#EXTM3U' not in text[:200]:
            continue
        for nm2, url2 in R.parse_m3u(text):
            url2name.setdefault(url2, nm2)
    except Exception:
        continue
print('上游原名映射:', len(url2name))

# 用英文原名重新分类
newrows = []
for g, nm, u in rows:
    orig = url2name.get(u, nm)
    cn, g2 = R.classify(orig, u)
    newrows.append((g2, cn, u))

text, uniq = R.build_playlist(newrows)
open(OUT, 'w', encoding='utf-8').write(text)
print(f'输出 {OUT}: {len(uniq)} 条（按台名去重后）')
for g, c in collections.Counter(r[0] for r in uniq).most_common():
    print(f'  {g}: {c}')
print('\n== 卫视/央视/国际 组 ==')
for g, cn, u in uniq:
    if g in ('卫视', '央视', '央视付费', '国际', '海外中文'):
        print(f'  [{g:5s}] {cn:22s} {u[:58]}')
