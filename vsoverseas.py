#!/usr/bin/env python3
"""
海外源 vs 国内源：可达性 / 吞吐 / 稳定性 / 分辨率 四维实测。

为什么必须实测：
  「海外源更稳定、画质更好」是个常见误解。真实情况取决于
  ① 节点是否还活着 ② 国际出口带宽 ③ 分辨率是否真的更高。

判据：
  - TTFB：切台响应
  - 每分片是否能在 TARGETDURATION 内下完（决定会不会卡）
  - 真实分辨率：解析 EXT-X-STREAM-INF 的 RESOLUTION，并用 OpenCV 解首帧核实

用法: python vsoverseas.py [秒数]
"""
import concurrent.futures as cf
import os
import re
import ssl
import sys
import tempfile
import time
import urllib.request

os.environ.setdefault("OPENCV_FFMPEG_LOGLEVEL", "-8")
os.environ.setdefault("OPENCV_LOG_LEVEL", "SILENT")

import cv2
import numpy as np

cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_SILENT)

UA = "okhttp/3.12.0"
_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE
TMP = tempfile.gettempdir()
SECS = int(sys.argv[1]) if len(sys.argv) > 1 else 15

# 海外（美西/美东机房）
OVERSEAS = [
    ("US 74.91 CCTV-3",  "http://74.91.26.218:82/live/cctv3hd.m3u8"),
    ("US 74.91 CCTV-4",  "http://74.91.26.218:82/live/cctv4hd.m3u8"),
    ("US 74.91 CCTV-7",  "http://74.91.26.218:82/live/cctv7hd.m3u8"),
    ("US 74.91 CCTV-8",  "http://74.91.26.218:82/live/cctv8hd.m3u8"),
    ("US 74.91 CCTV-10", "http://74.91.26.218:82/live/cctv10hd.m3u8"),
    ("US 74.91 CCTV-11", "http://74.91.26.218:82/live/cctv11hd.m3u8"),
    ("US 74.91 CCTV-12", "http://74.91.26.218:82/live/cctv12hd.m3u8"),
    ("US 74.91 CCTV-14", "http://74.91.26.218:82/live/cctv14hd.m3u8"),
    ("US 74.91 CCTV-15", "http://74.91.26.218:82/live/cctv15hd.m3u8"),
    ("US 74.91 CCTV-17", "http://74.91.26.218:82/live/cctv17hd.m3u8"),
    ("US 198.204 CCTV-6", "http://198.204.240.250:82/live/cctv6.m3u8"),
    ("US 69.30 CCTV-6",  "http://69.30.245.50/live/cctv6.m3u8"),
]

DOMESTIC = [
    ("CN 119.39 CCTV-1",  "http://119.39.9.8:9901/tsfile/live/0001_1.m3u8?key=txiptv&playlive=0&authid=0"),
    ("CN 119.39 CCTV-2",  "http://119.39.9.8:9901/tsfile/live/0002_1.m3u8?key=txiptv&playlive=0&authid=0"),
    ("CN 119.39 CCTV-3",  "http://119.39.9.8:9901/tsfile/live/0003_1.m3u8?key=txiptv&playlive=0&authid=0"),
    ("CN 119.39 CCTV-4",  "http://119.39.9.8:9901/tsfile/live/0004_1.m3u8?key=txiptv&playlive=0&authid=0"),
    ("CN 119.39 CCTV-7",  "http://119.39.9.8:9901/tsfile/live/0007_1.m3u8?key=txiptv&playlive=0&authid=0"),
    ("CN 119.39 CCTV-8",  "http://119.39.9.8:9901/tsfile/live/0008_1.m3u8?key=txiptv&playlive=0&authid=0"),
    ("CN 119.39 CCTV-10", "http://119.39.9.8:9901/tsfile/live/0010_1.m3u8?key=txiptv&playlive=0&authid=0"),
    ("CN 119.39 CCTV-11", "http://119.39.9.8:9901/tsfile/live/0011_1.m3u8?key=txiptv&playlive=0&authid=0"),
    ("CN 119.39 CCTV-15", "http://119.39.9.8:9901/tsfile/live/0015_1.m3u8?key=txiptv&playlive=0&authid=0"),
]


def fetch(url, timeout=20):
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


def real_resolution(ts_bytes):
    """用 OpenCV 解码首帧，拿真实分辨率。"""
    if not ts_bytes:
        return None
    p = os.path.join(TMP, f"_rs_{os.getpid()}_{id(ts_bytes)}.ts")
    try:
        open(p, "wb").write(ts_bytes)
        cap = cv2.VideoCapture(p)
        for _ in range(150):
            ok, fr = cap.read()
            if ok and fr is not None:
                h, w = fr.shape[:2]
                cap.release()
                return f"{w}x{h}"
        cap.release()
    except Exception:
        pass
    finally:
        try:
            os.remove(p)
        except Exception:
            pass
    return None


def test(grp, label, url, secs=SECS):
    r = {"grp": grp, "label": label, "url": url, "ok": False, "why": "",
         "ttfb": None, "mbps": 0.0, "stall": 0, "err": 0, "worst": 0.0,
         "seglen": None, "res": None, "res_src": "", "n": 0}
    try:
        t0 = time.time()
        body, _ = fetch(url, timeout=12)
        r["ttfb"] = time.time() - t0
    except Exception as e:
        r["why"] = f"playlist {str(e)[:40]}"
        return r
    text = body.decode("utf-8", "ignore")
    if "#EXTM3U" not in text:
        r["why"] = f"not m3u8 ({len(body)}B)"
        return r

    m = re.search(r"RESOLUTION=(\d+x\d+)", text)
    if m:
        r["res"] = m.group(1)
        r["res_src"] = "master"

    segs, tgt = parse(text)
    if not segs:
        r["why"] = "no segment"
        return r
    r["seglen"] = tgt
    base = url.rsplit("/", 1)[0]
    segdur = tgt or 6.0

    t_end = time.time() + secs
    seen, tb, tt, ok, stall, err = set(), 0, 0.0, 0, 0, 0
    worst, first_data = 0.0, None
    while time.time() < t_end:
        try:
            body, _ = fetch(url, timeout=8)
        except Exception:
            err += 1
            time.sleep(0.4)
            continue
        segs, t2 = parse(body.decode("utf-8", "ignore"))
        if t2:
            segdur = t2
        for dur, seg in segs[-2:]:
            s = seg if seg.startswith("http") else f"{base}/{seg}"
            if s in seen:
                continue
            seen.add(s)
            expect = dur or segdur
            ts = time.time()
            try:
                d, _ = fetch(s, timeout=int(expect * 2.5) + 4)
                el = time.time() - ts
                tb += len(d)
                tt += el
                worst = max(worst, el)
                if first_data is None and len(d) > 20000:
                    first_data = d
                if el <= expect:
                    ok += 1
                else:
                    stall += 1
            except Exception:
                err += 1
        time.sleep(0.3)

    if first_data and not r["res"]:
        r["res"] = real_resolution(first_data)
        r["res_src"] = "decoded"

    r.update(ok=True, mbps=tb * 8 / 1e6 / tt if tt else 0, stall=stall,
             err=err, worst=worst, n=ok + stall + err)
    return r


def main():
    jobs = [(g, l, u) for g, lst in (("OVERSEAS", OVERSEAS), ("DOMESTIC", DOMESTIC))
            for l, u in lst]
    results = []
    print(f"每源测 {SECS} 秒，{len(jobs)} 条并发\n", flush=True)
    with cf.ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        futs = {ex.submit(test, g, l, u): (g, l) for g, l, u in jobs}
        for f in cf.as_completed(futs):
            try:
                results.append(f.result())
            except Exception:
                pass

    for grp, name in (("OVERSEAS", "海外源"), ("DOMESTIC", "国内源")):
        rows = [r for r in results if r["grp"] == grp]
        rows.sort(key=lambda r: (not r["ok"], r["stall"], r["err"], -(r["mbps"] or 0)))
        print("\n" + "=" * 104)
        print(f"{name}（{len(rows)} 条）")
        print("=" * 104)
        print(f"{'标签':22s} {'TTFB':>7} {'Mbps':>7} {'分片长':>7} {'stall':>6} {'err':>4} "
              f"{'worst':>7} {'分辨率':>11}  备注")
        print("-" * 104)
        for r in rows:
            if not r["ok"]:
                print(f"{r['label']:22s} {'--':>7} {'--':>7} {'--':>7} {'--':>6} {'--':>4} "
                      f"{'--':>7} {'--':>11}  {r['why']}")
                continue
            print(f"{r['label']:22s} {r['ttfb']:7.2f} {r['mbps']:7.2f} "
                  f"{r['seglen'] or 0:7.1f} {r['stall']:6d} {r['err']:4d} "
                  f"{r['worst']:7.1f} {(r['res'] or '-'):>11}"
                  f"  {r['res_src']}")

    print("\n" + "=" * 104)
    print("汇总")
    print("=" * 104)
    for grp, name in (("OVERSEAS", "海外源"), ("DOMESTIC", "国内源")):
        allr = [r for r in results if r["grp"] == grp]
        reach = [r for r in allr if r["ok"]]
        clean = [r for r in reach if r["stall"] == 0 and r["err"] == 0]
        if not reach:
            print(f"{name:8s} 可达 0/{len(allr)} —— 全部不可用")
            continue
        avg_ttfb = sum(r["ttfb"] for r in reach) / len(reach)
        avg_mbps = sum(r["mbps"] for r in reach) / len(reach)
        avg_worst = sum(r["worst"] for r in reach) / len(reach)
        print(f"{name:8s} 播放列表可达 {len(reach)}/{len(allr)}  "
              f"真正能连续播（零停顿零错误）{len(clean)}/{len(allr)}")
        print(f"{'':8s} 平均 TTFB {avg_ttfb:.2f}s  平均 {avg_mbps:.1f} Mbps  "
              f"最差分片均值 {avg_worst:.1f}s")


if __name__ == "__main__":
    main()