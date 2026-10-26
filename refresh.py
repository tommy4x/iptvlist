#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IPTV 直播源自动筛选器
=====================
从上游源池拉取候选，逐条实测存活情况，输出多轮复测都通过的稳定清单。

用法:
    python3 refresh.py                # 刷新主清单 car-tv.m3u
    python3 refresh.py --full         # 同时输出全量清单 car-tv-full.m3u
    python3 refresh.py --rounds 2     # 复测轮数（默认 3）

实测方法（关键，别改）:
    1) 拉 m3u8
    2) 递归下钻 master/variant playlist
    3) 取分片，校验 MPEG-TS sync byte 0x47 —— 拿到真 TS 数据才算"能播"
    4) 多轮复测取交集 —— 单次通过会混入大量偶发可用的飘源

⚠️ 踩坑记录:
    - 分片常用【相对路径】，正则只认 http 开头会造成大量假阴性。必须 urljoin(base, uri)。
    - 部分源校验 Referer / 浏览器 UA，需重试策略。
    - 码率必须按【单分片】字节×8÷该分片时长 计算，不能整表字节÷总时长。
    - raw.githubusercontent.com 在国内常超时，优先走 gh-proxy：
        https://gh-proxy.com/https://raw.githubusercontent.com/<owner>/<repo>/<branch>/<path>
      注意分支: master = 源定义(streams/), gh-pages = 成品清单(countries/)
"""

import argparse
import collections
import concurrent.futures as futures
import os
import re
import socket
import ssl
import sys
import time
import urllib.request
from urllib.parse import urljoin

# ---------------------------------------------------------------- 配置

# 上游源池：iptv-org 公开清单
UPSTREAMS = [
    # 成品清单（已按国家过滤，149 条 CN）
    'https://gh-proxy.com/https://raw.githubusercontent.com/iptv-org/iptv/gh-pages/countries/cn.m3u',
    # 源定义（511 条，未经存活过滤 —— 挖新台的主力）
    'https://gh-proxy.com/https://raw.githubusercontent.com/iptv-org/iptv/master/streams/cn.m3u',
    # 备用代理（主代理挂了时兜底）
    'https://ghproxy.net/https://raw.githubusercontent.com/iptv-org/iptv/master/streams/cn.m3u',
    'https://ghfast.top/https://raw.githubusercontent.com/iptv-org/iptv/master/streams/cn.m3u',
    # 按【语言】聚合的清单：countries/cn.m3u 只按国家收（漏掉港台/海外中文台），
    # languages/zho.m3u 按语言收，能补上 CCTVR 港澳台/海外中文/部分境外中文频道。
    'https://gh-proxy.com/https://raw.githubusercontent.com/iptv-org/iptv/gh-pages/languages/zho.m3u',
    'https://ghproxy.net/https://raw.githubusercontent.com/iptv-org/iptv/gh-pages/languages/zho.m3u',
]

# 已知不稳定 / 海外 CDN / 版权存疑的域名，直接排除
BLACKLIST = [
    'amagi.tv', 'akamaized', 'antik.sk', 'skygo.mn', '0472.org', 'cgtn.com',
    'hlspackager', 'now.amagi', 'voa-ingest', 'mediaserver.abnvideos',
    'video.bread-tv', 'restream.pdtvhd.com',
]

# 频道名 -> (中文名, 分组)
CN_MAP = {
    'CCTV-1 (720p)': ('CCTV-1 综合', '央视'),
    'CCTV-2 (720p)': ('CCTV-2 财经', '央视'),
    'CCTV-3 (720p)': ('CCTV-3 综艺', '央视'),
    'CCTV-4 (720p)': ('CCTV-4 军事农业', '央视'),
    'CCTV-7 (720p)': ('CCTV-7 国防军事', '央视'),
    'CCTV-8 (720p)': ('CCTV-8 电视剧', '央视'),
    'CCTV-9 (576i)': ('CCTV-9 纪录', '央视'),
    'CCTV-10 (720p)': ('CCTV-10 科教', '央视'),
    'CCTV-11 (720p)': ('CCTV-11 戏曲', '央视'),
    'CCTV-12 (720p)': ('CCTV-12 社会与法', '央视'),
    'CCTV-13 (720p)': ('CCTV-13 新闻', '央视'),
    'CCTV-14 HD (1080p)': ('CCTV-14 少儿', '央视'),
    'CCTV-15 (720p)': ('CCTV-15 音乐', '央视'),
    'CCTV-16 HD (1080p)': ('CCTV-16 奥林匹克', '央视'),
    'CCTV-17 HD (1080p)': ('CCTV-17 农业农村', '央视'),
    'CCTV-4K HD (1080p)': ('CCTV-4K 超高清', '央视'),
    'CCTV-8K HD (1080p)': ('CCTV-8K 超高清', '央视'),
    'CCTV-Billiards (1080p)': ('CCTV-5+ 台球', '央视付费'),
    'CCTV-Culture of Quality SD (1080p)': ('CCTV-2 文化品质', '央视付费'),
    'CCTV-Golf & Tennis (1080p)': ('CCTV-5+ 高尔夫网球', '央视付费'),
    'CCTV-Health (1080p)': ('CCTV-5+ 健康', '央视付费'),
    'CCTV-Nostalgia Theater (1080p)': ('CCTV-6 怀旧剧场', '央视付费'),
    'CCTV-Storm Football (1080p)': ('CCTV-5+ 风暴足球', '央视付费'),
    'CCTV-Storm Music (1080p)': ('CCTV-5+ 风暴音乐', '央视付费'),
    'CCTV-Storm Theater (1080p)': ('CCTV-5+ 风暴剧场', '央视付费'),
    'CCTV-The First Theater (1080p)': ('CCTV-6 第一剧场', '央视付费'),
    'CCTV-Weapon & Technology (1080p)': ('CCTV-5+ 武器科技', '央视付费'),
    "CCTV-Women's Fashion SD (1080p)": ('CCTV-5+ 时尚', '央视付费'),
    'CCTV-World Geography SD (1080p)': ('CCTV-5+ 世界地理', '央视付费'),
    'CGTN': ('CGTN 中国国际电视台', '国际'),
    'CGTN Documentary': ('CGTN 纪录', '国际'),
    'CGTN Arabic': ('CGTN 阿拉伯语', '国际'),
    'CGTN French': ('CGTN 法语', '国际'),
    'CGTN Russian': ('CGTN 俄语', '国际'),
    'CGTN Spanish': ('CGTN 西语', '国际'),
    'CGTN Global Biz': ('CGTN 全球商业', '国际'),
    'China Travel': ('CGTN 旅行中国', '国际'),
    'Discovering China': ('CGTN 发现中国', '国际'),
    'Dragon TV International': ('东方卫视', '卫视'),
    'Hunan TV (2160p)': ('湖南卫视', '卫视'),
    'Hebei TV (2160p)': ('河北卫视', '卫视'),
    'Anhui TV': ('安徽卫视', '卫视'),
    'Shenzhen Satellite TV (2160p)': ('深圳卫视', '卫视'),
    'Qinghai TV': ('青海卫视', '卫视'),
    'Nei Monggol TV': ('内蒙古卫视', '卫视'),
    'Nei Monggol TV 2 Mongolian Culture Channel': ('内蒙古卫视蒙语', '卫视'),
    'Kangba TV': ('康巴卫视', '卫视'),
    'Jiangsu Public & News Channel': ('江苏新闻综合', '卫视'),
    'Zhejiang International Channel': ('浙江国际频道', '卫视'),
    'Jilin City Channel': ('吉林市台', '地方台'),
    'Jilin Lifestyle Channel': ('吉林生活频道', '地方台'),
    'Jilin Movie Channel': ('吉林影视频道', '地方台'),
    'Jilin Rural Channel': ('吉林农村频道', '地方台'),
    'Tonghua TV': ('通化电视台', '地方台'),
    'Harbin Comprehensive News Channel': ('哈尔滨新闻综合', '地方台'),
    'Harbin Movie Channel': ('哈尔滨电影频道', '地方台'),
    'Xinjiang TV 1': ('新疆电视 一套', '地方台'),
    'Xinjiang TV 2': ('新疆电视 二套', '地方台'),
    'Xinjiang TV 3': ('新疆电视 三套', '地方台'),
    'Xinjiang TV 5': ('新疆电视 五套', '地方台'),
    'Xinjiang TV 8': ('新疆电视 八套', '地方台'),
    'Xizang TV Chinese': ('西藏电视 中文', '地方台'),
    'Chifeng Comprehensive News Chanel': ('赤峰综合新闻', '地方台'),
    'Chuxiong News Channel [Not 24/7]': ('楚雄新闻频道', '地方台'),
    'Chaozhou Public Channel': ('潮州公共频道', '地方台'),
    'Dongguan Comprehensive News Channel': ('东莞综合频道', '地方台'),
    'Guangxi Variety & Travel Channel': ('广西综艺旅游', '地方台'),
    'Guangzhou TV': ('广州电视 综合频道', '地方台'),
    'Baicheng TV [Geo-blocked]': ('白城电视台', '地方台'),
    'Siping TV': ('四平电视台', '地方台'),
    'CND Film Middle School Channel': ('CND 中学生频道', '地方台'),
    'ABN China (720p)': ('ABN 中文台', '海外中文'),
    'Bread TV (720p)': ('面食 TV', '海外中文'),
    'Ando TV (1080p)': ('Ando TV 华人', '海外中文'),
    'CETV1 (576p)': ('CETV-1 中国教育', '央视'),
    'CETV2 (576p)': ('CETV-2 中国教育', '央视'),
    # ---- 港澳台 / 海外中文（来自 languages/zho.m3u）----
    'CCTV-4 America (1080p)': ('CCTV-4 America', '国际'),
    'CCTV-4 Asia (720p)': ('CCTV-4 亚洲', '国际'),
    'CCTV-4 Europe': ('CCTV-4 欧洲', '国际'),
    'CCTV+ 1 (600p) [Not 24/7]': ('CCTV+ 央视网1', '央视'),
    'CCTV+ 2 (600p) [Not 24/7]': ('CCTV+ 央视网2', '央视'),
    'CCTV-1 HD (1080p)': ('CCTV-1 综合 HD', '央视'),
    'CCTV-6 HD (1080p)': ('CCTV-6 电影 HD', '央视'),
    'CCTV-13 HD (1080p)': ('CCTV-13 新闻 HD', '央视'),
    'CCTV-4K HD (1080p)': ('CCTV-4K 超高清', '央视'),
    'CCTV-5+': ('CCTV-5+ 赛事', '央视付费'),
    'CCTV-6 [Geo-blocked]': ('CCTV-6 电影(备用)', '央视'),
    'Anhui TV': ('安徽卫视', '卫视'),
    'Beijing Satellite TV [Not 24/7]': ('北京卫视', '卫视'),
    'Beijing Satellite TV HD (1080p)': ('北京卫视 HD', '卫视'),
    'BSTV (Hong Kong) (576p)': ('BSTV 香港台', '港澳台'),
    '8TV': ('8TV 马来西亚', '海外中文'),
    'Angel TV Chinese (720p)': ('Angel TV 华人', '海外中文'),
    'BabyFirst (720p)': ('宝贝第一台', '海外中文'),
    'Beautiful Life TV': ('Beautiful Life 华人', '海外中文'),
    'Berita RTM [Geo-blocked]': ('RTM 马来新闻', '海外中文'),
}

# 浙江系（ali-m-l.cztv.com / l.cztvcloud.com）—— 频道名在源里五花八门，按 URL 归组
ZHEJIANG_HOSTS = ('cztv.com', 'cztvcloud.com', 'shaoxing.com.cn')

GROUP_ORDER = ['央视', '央视付费', '卫视', '港澳台', '地方台', '国际', '海外中文', '其他']

UA = 'Mozilla/5.0 (Linux; Android 4.2.2) AppleWebKit/537.36'

# 超时分级：
#   快筛 —— 短超时、零重试，用于淘汰绝大多数死源（死源占 70%+）
#   精验 —— 正常超时 + 重试，用于确认活源
FAST_TIMEOUT = 4
FULL_TIMEOUT = 10

# 并发数别贪高：24 线程同时打国内源会被服务端限流/耗尽连接，
# 实测 24 -> 全部假失败，降到 8 正常。GitHub Actions 是共享 IP，
# 8 更保险；本机网络好可以 12。
WORKERS = 8


def make_opener():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),          # 直连，不走本机代理
        urllib.request.HTTPSHandler(context=ctx),
    )


OPENER = make_opener()


def http_get(url, nbytes=2_000_000, timeout=FULL_TIMEOUT):
    """返回 (body_bytes, final_url)"""
    socket.setdefaulttimeout(timeout)
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    r = OPENER.open(req, timeout=timeout)
    try:
        return r.read(nbytes), r.geturl()
    finally:
        try:
            r.close()
        except Exception:
            pass


def is_ts(data):
    """MPEG-TS 同步字节校验：0x47 每 188 字节出现"""
    for off in (0, 188, 376, 564):
        if len(data) > off + 2 and data[off] == 0x47:
            return True
    return False


def parse_m3u(text):
    """解析 m3u -> [(name, url)]"""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    out = []
    for i, l in enumerate(lines):
        if not l.startswith('#EXTINF'):
            continue
        name = l.split(',', 1)[1].strip() if ',' in l else ''
        url = ''
        for j in range(i + 1, min(i + 4, len(lines))):
            if not lines[j].startswith('#'):
                url = lines[j]
                break
        if url:
            out.append((name, url))
    return out


def probe(url, depth=0, timeout=FULL_TIMEOUT, retries=1):
    """实测一条流：拉 m3u8 -> 下钻 -> 取分片验 TS。返回 True 表示确实能播。

    timeout/retries 可调：
      - 快筛阶段传 (4, 0)：死源 4 秒内出局，515 条里 ~70% 死源 30 秒搞定
      - 精验阶段传 (10, 1)：确认活源，允许重试
    """
    for attempt in range(retries + 1):
        try:
            body, final = http_get(url, 300_000, timeout=timeout)
        except Exception:
            if attempt < retries:
                time.sleep(0.5)
                continue
            return False
        try:
            text = body.decode('utf-8', 'replace')
        except Exception:
            return False
        if '#EXTM3U' not in text[:200]:
            return False
        uris = [urljoin(final, l.strip()) for l in text.splitlines()
                if l.strip() and not l.strip().startswith('#')]
        if not uris:
            return False
        for cand in uris[:2]:
            try:
                seg, _ = http_get(cand, 65_536, timeout=timeout)
            except Exception:
                continue
            if is_ts(seg):
                return True
            # 二层 variant playlist
            if depth < 1:
                t2 = seg.decode('utf-8', 'replace')
                if '#EXTM3U' in t2[:200]:
                    u2 = [urljoin(final, l.strip()) for l in t2.splitlines()
                          if l.strip() and not l.strip().startswith('#')]
                    for c3 in u2[:2]:
                        try:
                            b3, _ = http_get(c3, 65_536, timeout=timeout)
                            if is_ts(b3):
                                return True
                        except Exception:
                            continue
        if attempt < retries:
            time.sleep(0.4)
    return False


def round_test(items, timeout=FULL_TIMEOUT, retries=1):
    """一轮全量实测 -> {url: bool}"""
    res = {}

    def task(x):
        return x[1], probe(x[1], timeout=timeout, retries=retries)

    with futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for f in futures.as_completed([ex.submit(task, x) for x in items]):
            try:
                u, ok = f.result()
                res[u] = ok
            except Exception:
                pass
    return res


# 浙江系（ali-m-l.cztv.com / l.cztvcloud.com / shaoxing.com.cn）
# 注意：cztvcloud 域名下大部分是【市县台】（上虞/余姚/嵊州/萧山/新昌...），
# 不属于卫视组。只有 ali-m-l.cztv.com/channelXXX 这组是浙江本级频道。
ZHEJIANG_PROVINCIAL_HOST = 'ali-m-l.cztv.com'
# 浙江本级频道（该域下 channel001~channel0xx）
ZJ_PROVINCIAL = {
    '浙江国际频道': '卫视', '浙江新闻': '卫视', '浙江经济': '卫视',
    '浙江教科': '卫视', '浙江民生': '卫视', '浙江钱江': '卫视',
    '浙江影视': '卫视',
}
# 同一台的多个源（channel10 与 channel010 是同一个浙江国际频道）
ZJ_ALIAS = {
    '浙江国际频道': '浙江国际频道',
    '浙江国际': '浙江国际频道',
}


def classify(name, url):
    """返回 (中文台名, 分组)"""
    if name in CN_MAP:
        return CN_MAP[name]
    clean = re.sub(r'\s*\[(Geo-blocked|Not 24/7)\]', '', name)
    clean = re.sub(r'\s*\(\d{3,4}[pi]\)', '', clean)
    clean = clean.replace(' Ⅰ ', '').strip()
    # 浙江本级频道：按频道名判定（ali-m-l 域 + 已知本级台名）
    if ZHEJIANG_PROVINCIAL_HOST in url or 'cztv.com' in url:
        if clean in ZJ_PROVINCIAL:
            return (clean, ZJ_PROVINCIAL[clean])
        # 名字里带"浙江"但不在本级台名单 -> 多半是市县台
        if '国际' in clean and '浙江' in clean:
            return ('浙江国际频道', '卫视')
    # 同一台多源：统一台名（去重时按台名保留第一个可用的）
    if clean in ZJ_ALIAS:
        return (ZJ_ALIAS[clean], '卫视')
    if any(h in url for h in ZHEJIANG_HOSTS):
        return (clean, '地方台')
    if clean.startswith('CGTN'):
        return (clean, '国际')
    return (clean, '地方台')


def build_playlist(rows):
    """rows: [(group, cn_name, url)] -> (m3u_text, uniq_rows)

    去重规则：同一台名只保留第一个（第一个是实测轮次里最靠前、通常最稳的源）。
    """
    by_name = {}
    for g, cn, u in rows:
        if cn in by_name:
            continue          # 同台多源，只留首个
        by_name[cn] = (g, cn, u)
    uniq = list(by_name.values())
    uniq.sort(key=lambda x: (GROUP_ORDER.index(x[0]) if x[0] in GROUP_ORDER else 99, x[1]))
    lines = ['#EXTM3U']
    for g, cn, u in uniq:
        lines.append(f'#EXTINF:-1 tvg-name="{cn}" group-title="{g}",{cn}')
        lines.append(u)
    return '\n'.join(lines) + '\n', uniq


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--rounds', type=int, default=3)
    ap.add_argument('--full', action='store_true', help='同时输出全量清单')
    ap.add_argument('--out', default='car-tv.m3u')
    args = ap.parse_args()

    print('[1/4] 拉取上游源池...')
    pool = {}
    for u in UPSTREAMS:
        try:
            body, _ = http_get(u, 8_000_000, timeout=25)
            text = body.decode('utf-8', 'replace')
            if '#EXTM3U' not in text[:200]:
                print(f'    x {u[:60]} 非 m3u')
                continue
            got = parse_m3u(text)
            for nm, url in got:
                if any(b in url for b in BLACKLIST):
                    continue
                if 'udp/' in url or url.startswith('udp://'):
                    continue  # UDP 组播，车机不支持（需 udpxy 转 HTTP）
                pool.setdefault(url, nm)
            print(f'    + {u[:60]} -> {len(got)} 条')
        except Exception as e:
            print(f'    x {u[:60]} {type(e).__name__}')

    if not pool:
        print('!! 上游全挂，保留旧文件不覆盖')
        return 1
    print(f'    候选池: {len(pool)} 条')

    # pool 是 {url: name}，round_test 需要 [(name, url)] —— 注意顺序
    cands = [(name, url) for url, name in pool.items()]

    print(f'[2/4] 实测 {args.rounds} 轮...')
    # 快筛：短超时零重试，一次性淘汰绝大多数死源（死源占 70%+）
    print('    快筛 (4s 超时, 零重试)...')
    t0 = time.time()
    fast = round_test(cands, timeout=FAST_TIMEOUT, retries=0)
    survivors = [x for x in cands if fast.get(x[1])]
    print(f'    快筛: {len(survivors)}/{len(cands)} 通过  ({time.time()-t0:.0f}s)')

    # 精验：只对幸存者做多轮复测，耗时断崖式下降
    alive = []
    for r in range(args.rounds):
        res = round_test(survivors, timeout=FULL_TIMEOUT, retries=1)
        alive.append(res)
        ok = sum(1 for v in res.values() if v)
        print(f'    精验轮{r+1}: 通过 {ok}/{len(survivors)}')
        # 本轮失败的直接踢出，不再参与后续轮次
        survivors = [x for x in survivors if res.get(x[1])]
        if r < args.rounds - 1:
            time.sleep(3)

    stable = [u for _, u in survivors]
    print(f'    多轮均通过: {len(stable)}')

    print('[3/4] 生成清单...')
    rows = []
    for u in stable:
        cn, g = classify(pool[u], u)
        rows.append((g, cn, u))
    text, uniq = build_playlist(rows)
    with open(args.out, 'w', encoding='utf-8') as f:
        f.write(text)
    print(f'    已写 {args.out} ({len(uniq)} 条)')

    if args.full:
        # 全量：所有候选都写。
        # 注意：必须按 URL 保留全部，不能走 build_playlist（那个按台名去重，
        # 会把同台多源合并，数量对不上候选池）。同台多源用序号区分。
        allrows = []
        for u, nm in pool.items():
            cn, g = classify(nm, u)
            allrows.append((g, cn, u))
        flines = ['#EXTM3U']
        seen = collections.Counter()
        for g, cn, u in allrows:
            seen[cn] += 1
            name = cn if seen[cn] == 1 else '%s %d' % (cn, seen[cn])
            flines.append('#EXTINF:-1 tvg-name="%s" group-title="%s",%s' % (name, g, name))
            flines.append(u)
        with open('car-tv-full.m3u', 'w', encoding='utf-8') as f:
            f.write('\n'.join(flines) + '\n')
        print(f'    已写 car-tv-full.m3u ({len(allrows)} 条)')

    print('[4/4] 统计:')
    for g, c in collections.Counter(r[0] for r in uniq).most_common():
        print(f'    {g}: {c}')

    with open('refresh-report.txt', 'w', encoding='utf-8') as f:
        f.write(f'更新时间: {time.strftime("%Y-%m-%d %H:%M:%S")}\n')
        f.write(f'候选池: {len(pool)}  稳定: {len(stable)}\n\n')
        for g, cn, u in uniq:
            f.write(f'[{g}] {cn}\t{u}\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
