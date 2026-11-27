#!/usr/bin/env python3
"""
稳定性筛选：找出「每个分片都能在分片时长内下完」的源。

⚠️ 这是唯一正确的判据。历史教训：
  旧的 speedrank.py 测「4 秒能下多少字节」，测出 8~11 Mbps 就判合格。
  但实测有源「平均 10 Mbps、单个分片却要 113 秒」——播放必卡。
  分片停顿发生在「分片刚生成、服务器还没写完」时，
  与链路带宽无关，所以挂代理也解决不了（已验证：直连 vs 代理 速度无差异）。

快速失败（关键，否则极慢）：
  - 播放列表 3 秒超时
  - 最新分片 2 秒内拿不到 300KB（≈1.2 Mbps）→ 直接判死
  - 任何一次 stall 立刻放弃该源
  - 命中即停：某频道一旦有干净源，不再测它的其他候选

用法:
  python stability.py                    # 全部频道
  python stability.py CCTV1,CCTV7 12 40  # 指定频道 / 每源秒数 / 并发
"""
import concurrent.futures as cf
import re
import ssl
import sys
import threading
import time
import urllib.request
from collections import OrderedDict

UA = "okhttp/3.12.0"
_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE

WANT = [c.strip() for c in (sys.argv[1] if len(sys.argv) > 1 else "").split(",") if c.strip()]
MAXSEC = int(sys.argv[2]) if len(sys.argv) > 2 else 12
WORKERS = int(sys.argv[3]) if len(sys.argv) > 3 else 40

MIN_BYTES_IN_2S = 300 * 1024
MIN_MBPS = 3.0
MIN_CLEAN_SEGS = 2
lock = threading.Lock()


def read_for(url, seconds, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    t0 = time.time()
    got = 0
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ctx) as r:
            while time.time() - t0 < seconds:
                c = r.read(65536)
                if not c:
                    break
                got += len(c)
    except Exception as e:
        return got, time.time() - t0, str(e)[:40]
    return got, time.time() - t0, None


def fetch_all(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout, context=_ctx) as r:
        d = r.read()
    return d, time.time() - t0


def parse(body):
    segs, tgt, pend = [], None, None
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-TARGETDURATION:"):
            try:
                tgt = float(line.split(":")[1])
            except Exception:
                pass
        elif line.startswith("#EXTINF:"):
            try:
                pend = float(line.split(":")[1].split(",")[0])
            except Exception:
                pend = None
        elif line and not line.startswith("#"):
            segs.append((pend, line))
    return segs, tgt


def probe(url, maxsec=MAXSEC):
    dead = {"ok": 0, "stall": 0, "err": 1, "mbps": 0.0, "worst": 0.0, "n": 1, "dead": True}
    try:
        body, _ = fetch_all(url, timeout=3)
    except Exception:
        return dead
    text = body.decode("utf-8", "ignore")
    if "#EXTM3U" not in text:
        return dead
    segs, tgt = parse(text)
    if not segs:
        return dead
    base = url.rsplit("/", 1)[0]
    segdur = tgt or 6.0

    # 快速失败：最新分片 2 秒能拿多少
    last = segs[-1][1]
    su = last if last.startswith("http") else f"{base}/{last}"
    got, dt, err = read_for(su, 2.0, timeout=6)
    if (err and got == 0) or got < MIN_BYTES_IN_2S:
        return {"ok": 0, "stall": 0, "err": 1,
                "mbps": got * 8 / 1e6 / dt if dt else 0,
                "worst": dt, "n": 1, "dead": True}

    t_end = time.time() + maxsec
    seen, oks, stalls, errs = set(), 0, 0, 0
    tb, tt, worst = 0, 0.0, 0.0
    while time.time() < t_end:
        try:
            body, _ = fetch_all(url, timeout=3)
        except Exception:
            errs += 1
            time.sleep(0.3)
            continue
        segs, tgt2 = parse(body.decode("utf-8", "ignore"))
        if tgt2:
            segdur = tgt2
        for dur, seg in segs[-2:]:
            s = seg if seg.startswith("http") else f"{base}/{seg}"
            if s in seen:
                continue
            seen.add(s)
            expect = dur or segdur
            try:
                d, dt2 = fetch_all(s, timeout=int(expect * 1.5) + 2)
                tb += len(d)
                tt += dt2
                worst = max(worst, dt2)
                if dt2 <= expect:
                    oks += 1
                else:
                    stalls += 1
                    break
            except Exception:
                errs += 1
                break
        if stalls or errs:
            break
        if oks >= MIN_CLEAN_SEGS and tb * 8 / 1e6 / max(tt, 0.001) >= MIN_MBPS:
            break
        time.sleep(0.3)

    return {"ok": oks, "stall": stalls, "err": errs,
            "mbps": tb * 8 / 1e6 / tt if tt else 0.0,
            "worst": worst, "n": oks + stalls + errs, "dead": False}


def good(st):
    return (not st.get("dead") and st["ok"] >= MIN_CLEAN_SEGS
            and st["stall"] == 0 and st["err"] == 0
            and st["worst"] < 15 and st["mbps"] >= MIN_MBPS)


def main():
    chans, meta, cur = OrderedDict(), {}, None
    for raw in open("cctv.m3u", encoding="utf-8"):
        line = raw.strip()
        if line.startswith("#EXTINF"):
            name = (re.search(r'tvg-name="([^"]*)"', line) or [None, ""])[1]
            logo = (re.search(r'tvg-logo="([^"]*)"', line) or [None, ""])[1]
            title = line.split(",")[-1].strip()
            cur = name or title
            meta[cur] = (logo, title)
            chans.setdefault(cur, [])
        elif line.startswith("http") and cur:
            chans[cur].append(line)

    targets = [c for c in WANT if c in chans] or list(chans)
    solved, fallback = {}, {}
    stop = threading.Event()
    t0 = time.time()

    def work(ch, url):
        if stop.is_set():
            return
        with lock:
            if ch in solved:
                return
        try:
            st = probe(url)
        except Exception:
            return
        with lock:
            if ch in solved:
                return
            if good(st):
                solved[ch] = (url, st)
                tag = "★"
            else:
                old = fallback.get(ch)
                sc = st["err"] * 10 + st["stall"] * 3
                if old is None or sc < old[1]["err"] * 10 + old[1]["stall"] * 3:
                    fallback[ch] = (url, st)
                tag = "." if st.get("dead") else " "
            print(f"{tag} {ch:8s} ok={st['ok']:<2d} stall={st['stall']:<2d} "
                  f"err={st['err']:<2d} {st['mbps']:6.1f}M worst={st['worst']:5.1f}s "
                  f"[{len(solved)}/{len(targets)} {time.time()-t0:.0f}s]", flush=True)
            if len(solved) >= len(targets):
                stop.set()

    order = []
    for i in range(max((len(chans[t]) for t in targets), default=0)):
        for t in targets:
            if i < len(chans[t]):
                order.append((t, chans[t][i]))
    order = list(dict.fromkeys(order))

    print(f"{len(targets)} 频道 / {len(order)} 候选，{WORKERS} 并发，慢源 2 秒即弃\n", flush=True)
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(work, ch, u) for ch, u in order]
        for f in cf.as_completed(futs):
            if stop.is_set():
                for x in futs:
                    x.cancel()
                break

    print("\n" + "=" * 74)
    out = ["#EXTM3U"]
    for ch in targets:
        if ch in solved:
            u, st = solved[ch]
            print(f"  ★ {ch:8s} 干净 {st['mbps']:6.1f}M worst={st['worst']:.1f}s  {u}")
        elif ch in fallback:
            u, st = fallback[ch]
            print(f"  ! {ch:8s} stall={st['stall']} err={st['err']} worst={st['worst']:.1f}s  {u}")
        else:
            print(f"  X {ch:8s} 无结果")
            continue
        logo, title = meta.get(ch, ("", ch))
        out.append(f'#EXTINF:-1 tvg-name="{ch}" tvg-logo="{logo}" group-title="央视",{title}')
        out.append(u)

    open("stable.m3u", "w", encoding="utf-8").write("\n".join(out) + "\n")
    print(f"\n耗时 {time.time()-t0:.0f}s，干净 {len(solved)}/{len(targets)}，wrote stable.m3u")


if __name__ == "__main__":
    main()