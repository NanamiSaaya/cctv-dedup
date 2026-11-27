#!/usr/bin/env python3
"""
抓取 best-fan/iptv-sources 的 CCTV 源，去重后输出 cctv.m3u。

⚠️ 重要设计变更（2026-11-26）：
  旧版本按上游的 response-time 排序，这个字段只测量了 m3u8 播放列表本身
  （几百字节）的响应速度，**完全不反映分片码流的吞吐**。
  结果每个频道都被排到 123.160.235.204 上——它播放列表秒回，
  但分片被限速到 ~1 Mbps，而 1080p 需要 3-5 Mbps，于是必然卡顿/反复重连。

  现在本脚本只负责「去重 + 保留全部候选」，**不做质量排序**。
  真正的排序交给家里跑的 speedrank.py 按实测带宽决定
  （云端 runner 在美国，测出来的带宽和家里宽带毫无关系，不能在这里排序）。

规则：
  - 频道：CCTV-1~17 + CCTV5+，每个频道只保留一个条目
  - 每频道保留最多 8 条不同 IP 的候选源（给本地测速留挑选余地）
  - 过滤已知失效的源
输出供 my-t-v0 / 任意 m3u 播放器直接加载。
"""
import re
import urllib.request
from collections import defaultdict

UPSTREAMS = [
    "https://raw.githubusercontent.com/best-fan/iptv-sources/main/cn_cctv_status.m3u8",
    "https://cdn.jsdelivr.net/gh/best-fan/iptv-sources@main/cn_cctv_status.m3u8",
    "https://ghproxy.net/https://raw.githubusercontent.com/best-fan/iptv-sources/main/cn_cctv_status.m3u8",
]
ORDER = ["CCTV1", "CCTV2", "CCTV3", "CCTV4", "CCTV5", "CCTV5+", "CCTV6", "CCTV7",
         "CCTV8", "CCTV9", "CCTV10", "CCTV11", "CCTV12", "CCTV13", "CCTV14",
         "CCTV15", "CCTV16", "CCTV17"]
DEAD = [r"74\.91\.26\.218", r"173\.208\.", r"gslbserv\.itv\.cmvideo\.cn",
        r"38\.75\.136\.137", r"104\.152\.209\.49", r"69\.30\.245\.50",
        r"204\.12\.221\.218"]
MAX_URLS = 8


def fetch():
    last = None
    for url in UPSTREAMS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                text = r.read().decode("utf-8", "ignore")
            if "#EXTINF" in text:
                return text
        except Exception as e:
            last = e
            print(f"fetch {url} failed: {e}")
    raise SystemExit(f"all upstreams failed: {last}")


def base_name(n):
    if not n:
        return None
    m = re.search(r"CCTV-?(\d+)(\+?)", n)
    if not m:
        return None
    return f"CCTV{m.group(1)}{'+' if m.group(2) else ''}"


def parse(text):
    groups = defaultdict(list)
    cur = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXTINF"):
            tn = (re.search(r'tvg-name="([^"]*)"', line) or [None, ""])[1]
            logo = (re.search(r'tvg-logo="([^"]*)"', line) or [None, ""])[1]
            title = line.split(",")[-1].strip()
            cur = {"tn": tn, "logo": logo, "title": title}
        elif line.startswith("http") and cur:
            b = base_name(cur["tn"]) or base_name(cur["title"])
            if b in ORDER and not any(re.search(p, line) for p in DEAD):
                groups[b].append({**cur, "url": line})
            cur = None
    return groups


def dedupe(text):
    g = parse(text)
    out = ["#EXTM3U"]
    for ch in ORDER:
        cand = g.get(ch, [])
        if not cand:
            continue
        # 只做 IP 去重，保持上游给的相对顺序；质量排序留给本地 speedrank.py
        seen_ip, urls = set(), []
        for c in cand:
            ip = (re.match(r"https?://([^/:]+)", c["url"]) or [None, ""])[1]
            if ip in seen_ip:
                continue
            seen_ip.add(ip)
            urls.append(c["url"])
            if len(urls) >= MAX_URLS:
                break
        disp = "CCTV-5+" if ch == "CCTV5+" else ch.replace("CCTV", "CCTV-", 1)
        out.append(f'#EXTINF:-1 tvg-name="{ch}" tvg-logo="{cand[0]["logo"]}" group-title="央视",{disp}')
        out.extend(urls)
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    body = dedupe(fetch())
    with open("cctv.m3u", "w", encoding="utf-8") as f:
        f.write(body)
    n = body.count("#EXTINF")
    print(f"wrote cctv.m3u: {n} channels, {body.count('http')} candidate urls")
