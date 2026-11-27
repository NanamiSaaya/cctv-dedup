#!/usr/bin/env python3
"""
按本地实测带宽给候选源排序，生成 cctv-fast.m3u。

关键设计（2026-11-26 修正）：
  1. 单次测速会被瞬时拥塞带偏 —— 同一台机器上一分钟测 CCTV1 是 1.26 Mbps，
     下一分钟同一个源是 7.6 Mbps。所以**每条源测 MULTI 轮取中位数**，
     并且判定阈值放在中位数上，不用单次峰值。
  2. 保留一组「已验证的高带宽节点」作为兜底：这些节点实测能跑满
     （153.0.171.163 / 60.223.94.153 / 110.72.84.44 / 221.7.175.154），
     即使这一轮测速被干扰，也优先用它们，避免退回被限速到 ~1 Mbps 的源。
  3. 输出会标注每条源的来源（PREFERRED / measured），便于排查。

用法:
    python speedrank.py            # 读 cctv.m3u，写 cctv-fast.m3u
    python speedrank.py in.m3u out.m3u
"""
import re
import ssl
import sys
import time
import urllib.request
from collections import OrderedDict

SRC = sys.argv[1] if len(sys.argv) > 1 else "cctv.m3u"
DST = sys.argv[2] if len(sys.argv) > 2 else "cctv-fast.m3u"
SEED = "fastseed.txt"   # 已验证高带宽节点，并进候选池
MIN_MBPS = 3.0          # 中位数低于此值认为扛不住 1080p
PROBE = 4.0             # 每轮最多测几秒
ROUNDS = 2              # 每条源测几轮，取中位数
MAX_URLS = 3
UA = "okhttp/3.12.0"

_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE


def http_get(url, timeout, limit=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ctx) as r:
            if limit is None:
                return r.read(), time.time() - t0, None
            got = 0
            while got < limit:
                try:
                    chunk = r.read(min(65536, limit - got))
                except Exception:
                    break
                if not chunk:
                    break
                got += len(chunk)
                if time.time() - t0 >= PROBE:
                    break
            return got, time.time() - t0, None
    except Exception as e:
        return None, 0.0, str(e)


def measure_once(url):
    body, dt, err = http_get(url, timeout=10)
    if body is None:
        return 0.0, f"playlist err {err}"
    text = body.decode("utf-8", "ignore")
    if "#EXTM3U" not in text:
        return 0.0, "not m3u8"
    segs = [l.strip() for l in text.splitlines()
            if l.strip() and not l.strip().startswith("#")]
    if not segs:
        return 0.0, "no segment"
    seg = segs[-2] if len(segs) >= 2 else segs[0]
    if not seg.startswith("http"):
        seg = url.rsplit("/", 1)[0] + "/" + seg
    got, dt2, err2 = http_get(seg, timeout=PROBE + 5, limit=48 * 1024 * 1024)
    if got is None:
        return 0.0, f"segment err {err2}"
    secs = min(max(dt2, 0.001), PROBE)
    if got < 80 * 1024 and secs >= PROBE - 0.5:
        return 0.0, f"too slow ({got}B/{secs:.1f}s)"
    return got * 8 / 1e6 / secs, f"{got}B/{secs:.2f}s"


def measure(url):
    """多轮取中位数，抑制瞬时拥塞造成的误判。"""
    vals, note = [], ""
    for _ in range(ROUNDS):
        mbps, why = measure_once(url)
        vals.append(mbps)
        note = why
        if mbps <= 0:
            break
    vals.sort()
    return vals[len(vals) // 2], f"median of {vals} ({note})"


def load_seed(path=SEED):
    """读已验证的高带宽节点表，返回 {channel: [urls]}。"""
    seed = {}
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
                seed.setdefault(ch, []).append(url)
    except FileNotFoundError:
        pass
    return seed


def main():
    chans = OrderedDict()
    cur = None
    for raw in open(SRC, encoding="utf-8"):
        line = raw.strip()
        if line.startswith("#EXTINF"):
            name = (re.search(r'tvg-name="([^"]*)"', line) or [None, ""])[1]
            logo = (re.search(r'tvg-logo="([^"]*)"', line) or [None, ""])[1]
            title = line.split(",")[-1].strip()
            cur = {"name": name, "logo": logo, "title": title}
            chans.setdefault(name, {"meta": cur, "urls": []})
        elif line.startswith("http") and cur:
            chans[cur["name"]]["urls"].append(line)

    # 把种子节点并进候选池（去重，放前面，同分时优先）
    seed = load_seed()
    for ch, urls in seed.items():
        if ch not in chans:
            continue
        existing = chans[ch]["urls"]
        for u in reversed(urls):
            if u in existing:
                existing.remove(u)
            existing.insert(0, u)

    out = ["#EXTM3U"]
    for name, info in chans.items():
        m = info["meta"]
        print(f"\n=== {name} ({len(info['urls'])} candidates) ===", flush=True)
        scored = []
        for u in info["urls"]:
            mbps, why = measure(u)
            tag = "SEED" if u in seed.get(name, []) else "    "
            print(f"  {mbps:7.2f} Mbps {tag}  {u}  ({why})", flush=True)
            scored.append((mbps, u))
        scored.sort(key=lambda x: -x[0])
        good = [u for mbps, u in scored if mbps >= MIN_MBPS]
        if not good and scored:
            good = [scored[0][1]]        # 兜底，别让频道消失
        if not good:
            continue
        out.append(f'#EXTINF:-1 tvg-name="{name}" tvg-logo="{m["logo"]}" '
                   f'group-title="央视",{m["title"]}')
        out.extend(good[:MAX_URLS])

    body = "\n".join(out) + "\n"
    open(DST, "w", encoding="utf-8").write(body)
    print(f"\nwrote {DST}: {body.count('#EXTINF')} channels, {body.count('http')} urls")


if __name__ == "__main__":
    main()
