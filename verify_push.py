#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""推送后自检：确认 raw / gh-proxy / github.io 三条路都能拉到正确内容。"""
import ssl, sys, time, urllib.request

ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
op = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ctx))
UA = {'User-Agent': 'Mozilla/5.0 (Linux; Android 4.2.2) AppleWebKit/537.36'}

REPO = sys.argv[1] if len(sys.argv) > 1 else 'tommy4x/iptvlist'
BRANCH = sys.argv[2] if len(sys.argv) > 2 else 'main'

URLS = [
    ('raw 直连        ', f'https://raw.githubusercontent.com/{REPO}/{BRANCH}/car-tv.m3u'),
    ('gh-proxy.com    ', f'https://gh-proxy.com/https://raw.githubusercontent.com/{REPO}/{BRANCH}/car-tv.m3u'),
    ('ghproxy.net     ', f'https://ghproxy.net/https://raw.githubusercontent.com/{REPO}/{BRANCH}/car-tv.m3u'),
    ('ghfast.top      ', f'https://ghfast.top/https://raw.githubusercontent.com/{REPO}/{BRANCH}/car-tv.m3u'),
    ('github.io pages ', f'https://{REPO.split("/")[0]}.github.io/{REPO.split("/")[1]}/car-tv.m3u'),
    ('README          ', f'https://gh-proxy.com/https://raw.githubusercontent.com/{REPO}/{BRANCH}/README.md'),
    ('使用说明.md      ', f'https://gh-proxy.com/https://raw.githubusercontent.com/{REPO}/{BRANCH}/使用说明.md'),
    ('lists/car-tv.m3u', f'https://gh-proxy.com/https://raw.githubusercontent.com/{REPO}/{BRANCH}/lists/car-tv.m3u'),
]

ok_cnt = 0
for label, u in URLS:
    try:
        t0 = time.time()
        r = op.open(urllib.request.Request(u, headers=UA), timeout=25)
        d = r.read(2_000_000)
        dt = time.time() - t0
        n = d.count(b'#EXTINF:')
        good = (n > 0) if u.endswith('.m3u') else (len(d) > 100)
        print(f'  {label} {r.getcode()} {len(d):8d}B {dt:5.1f}s 条目={n:4d} {"OK" if good else "内容异常"}')
        ok_cnt += good
    except Exception as e:
        print(f'  {label} FAIL {repr(e)[:70]}')

print(f'\n可用: {ok_cnt}/{len(URLS)}')
print('\n车机填这个 URL:')
print(f'  https://gh-proxy.com/https://raw.githubusercontent.com/{REPO}/{BRANCH}/car-tv.m3u')
