#!/usr/bin/env python3
"""
抓取 best-fan/iptv-sources 的 CCTV 源，去重后输出 cctv.m3u。

规则：
  - 频道：CCTV-1~17 + CCTV5+，每个频道只保留一个条目
  - 每频道最多 3 条不同 IP 的源做 fallback（response-time 短的优先，[S] 已验证优先）
  - 过滤已知失效的海外源（74.91.x、173.208.x、cmvideo 403 源）
输出供 my-t-v0 / 任意 m3u 播放器直接加载。
"""
import re
import urllib.request
from collections import defaultdict

UPSTREAMS = [
    "https://raw.githubusercontent.com/best-fan/iptv-sources/main/cn_cctv_status.m3u8",
    "https://cdn.jsdelivr.net/gh/best-fan/iptv-sources@main/cn_cctv_status.m3u8",
]
ORDER = ["CCTV1", "CCTV2", "CCTV3", "CCTV4", "CCTV5", "CCTV5+", "CCTV6", "CCTV7",
         "CCTV8", "CCTV9", "CCTV10", "CCTV11", "CCTV12", "CCTV13", "CCTV14",
         "CCTV15", "CCTV16", "CCTV17"]
DEAD = [r"74\.91\.26\.218", r"173\.208\.", r"gslbserv\.itv\.cmvideo\.cn"]


def fetch():
    for url in UPSTREAMS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                text = r.read().decode("utf-8", "ignore")
            if "#EXTINF" in text:
                return text
        except Exception as e:
            print(f"fetch {url} failed: {e}")
    raise SystemExit("all upstreams failed")


def base_name(n):
    m = re.match(r"CCTV-?(\d+)", n or "")
    if not m:
        return None
    return "CCTV" + m.group(1) + ("+" if "+" in (n or "")[:12] else "")


def parse(text):
    groups = defaultdict(list)
    cur = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXTINF"):
            tn = (re.search(r'tvg-name="([^"]*)"', line) or [None, ""])[1]
            rt = (re.search(r'response-time="([^"]*)"', line) or [None, ""])[1]
            logo = (re.search(r'tvg-logo="([^"]*)"', line) or [None, ""])[1]
            title = line.split(",")[-1].strip()
            cur = {"tn": tn, "rt": rt, "logo": logo, "title": title}
        elif line.startswith("http") and cur:
            b = base_name(cur["tn"])
            if b in ORDER and not any(re.search(p, line) for p in DEAD):
                groups[b].append({**cur, "url": line})
            cur = None
    return groups


def ms(s):
    n = re.sub(r"\D", "", s or "")
    return int(n) if n else 9999


def score(c):
    s = ms(c["rt"])
    if "[S]" in c["tn"]:
        s -= 1000
    if "1080" in c["tn"]:
        s -= 200
    elif "720" in c["tn"]:
        s -= 100
    return s


def dedupe(text):
    g = parse(text)
    out = ["#EXTM3U"]
    for ch in ORDER:
        cand = sorted(g.get(ch, []), key=score)
        if not cand:
            continue
        seen_ip, urls = set(), []
        for c in cand:
            ip = (re.match(r"https?://([^/:]+)", c["url"]) or [None, ""])[1]
            if urls and ip in seen_ip:
                continue
            seen_ip.add(ip)
            urls.append(c["url"])
            if len(urls) >= 3:
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
    print(f"wrote cctv.m3u: {n} channels")
