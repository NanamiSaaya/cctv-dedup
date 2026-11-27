#!/usr/bin/env python3
"""
抓取 best-fan/iptv-sources 的 CCTV 源，去重后输出 candidates.m3u。

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
import os
import re
import ssl
import urllib.request
from collections import defaultdict

# 抓上游要走代理（GitHub 直连被 reset），但这里只是抓文本，不影响测源精度。
# 代理从环境变量 GIT_PROXY 或默认 mihomo 取；没有则直连。
_PROXY = os.environ.get("GIT_PROXY") or os.environ.get("https_proxy") or ""
_opener = None
if _PROXY:
    _opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": _PROXY, "https": _PROXY}))
else:
    _opener = urllib.request.build_opener()

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
        r"204\.12\.221\.218",
        # 2026-11-27 实测：播放列表 34ms 秒回，分片却限速到 0.5~1.3 Mbps。
        # 上游总塞 15 条（response-time 好看），把优质节点挤出候选池，
        # 导致 18 频道只有 2~3 个达标。拉黑。
        r"123\.160\.235\.204"]
MAX_URLS = 8


def fetch():
    last = None
    for url in UPSTREAMS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with _opener.open(req, timeout=30) as r:
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


def load_seed(path="fastseed.txt"):
    """读取已验证节点种子表：每行 `CCTV1<TAB或空格>URL`。

    这些是人工核对过台标、实测过带宽和分片稳定性的节点，
    优先级高于上游任意排序。
    """
    seed = defaultdict(list)
    try:
        for raw in open(path, encoding="utf-8"):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            ch, url = parts[0].strip(), parts[1].strip()
            if url.startswith("http"):
                seed[ch].append(url)
    except FileNotFoundError:
        pass
    return seed


def dedupe(text):
    g = parse(text)
    seed = load_seed()
    out = ["#EXTM3U"]
    for ch in ORDER:
        cand = g.get(ch, [])
        if not cand:
            continue
        # ⚠️ 关键：把 fastseed.txt 里**人工核对过台标 + 实测稳定**的节点
        # 优先放进候选池。否则上游按 time 排序会把这些好节点挤掉 ——
        # 实测上游一次给 123.160.235.204（限速 1 Mbps）放了 15 条，
        # 把 60.223.94.153 / 112.123.243.37 这些优质节点全挤出去了，
        # 结果 18 个频道只有 2~3 个达标。
        urls, seen_ip = [], set()
        for u in seed.get(ch, []):
            ip = (re.match(r"https?://([^/:]+)", u) or [None, ""])[1]
            if ip and ip not in seen_ip:
                seen_ip.add(ip)
                urls.append(u)
        # 再用上游候选补足
        for c in cand:
            ip = (re.match(r"https?://([^/:]+)", c["url"]) or [None, ""])[1]
            if ip in seen_ip:
                continue
            seen_ip.add(ip)
            urls.append(c["url"])
            if len(urls) >= MAX_URLS + 4:
                break
        disp = "CCTV-5+" if ch == "CCTV5+" else ch.replace("CCTV", "CCTV-", 1)
        out.append(f'#EXTINF:-1 tvg-name="{ch}" tvg-logo="{cand[0]["logo"]}" group-title="央视",{disp}')
        out.extend(urls)
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    body = dedupe(fetch())
    with open("candidates.m3u", "w", encoding="utf-8") as f:
        f.write(body)
    n = body.count("#EXTINF")
    print(f"wrote candidates.m3u: {n} channels, {body.count('http')} urls")
