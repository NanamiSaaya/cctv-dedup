#!/usr/bin/env python3
"""
最终校验：对 cctv-fast.m3u 里每条源渲染台标拼图 + 测带宽，
确认「内容正确」且「带宽够」，一次出图人工终审。

用法: python finalcheck.py [m3u] [outdir]
"""
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
CROP_W, CROP_H, SCALE, COLS = 0.28, 0.24, 2, 3
PROBE = 4.0


def fetch(url, limit, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout, context=_ctx) as r:
        return r.read(limit)


def meter(url, limit, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    t0, got = time.time(), 0
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ctx) as r:
            while got < limit:
                chunk = r.read(min(65536, limit - got))
                if not chunk:
                    break
                got += len(chunk)
                if time.time() - t0 >= PROBE:
                    break
    except Exception:
        pass
    return got, time.time() - t0


def probe(url):
    try:
        body = fetch(url, 256 * 1024).decode("utf-8", "ignore")
    except Exception as e:
        return 0.0, None, str(e)[:40]
    if "#EXTM3U" not in body:
        return 0.0, None, "not m3u8"
    base = url.rsplit("/", 1)[0]
    segs = [l.strip() for l in body.splitlines()
            if l.strip() and not l.strip().startswith("#")]
    if not segs:
        return 0.0, None, "no seg"
    seg = segs[-2] if len(segs) >= 2 else segs[0]
    su = seg if seg.startswith("http") else f"{base}/{seg}"
    got, dt = meter(su, 48 * 1024 * 1024, PROBE + 5)
    mbps = got * 8 / 1e6 / min(max(dt, 0.001), PROBE) if got else 0.0
    data = b""
    for s in segs[-3:]:
        u = s if s.startswith("http") else f"{base}/{s}"
        try:
            data += fetch(u, 6 * 1024 * 1024)
        except Exception:
            continue
    frame = None
    if data:
        p = os.path.join(TMP, f"_fc_{os.getpid()}.ts")
        open(p, "wb").write(data)
        cap = cv2.VideoCapture(p)
        for i in range(300):
            ok, fr = cap.read()
            if not ok:
                break
            if i > 25:
                frame = fr
                break
        cap.release()
        os.remove(p)
    return mbps, frame, "ok"


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "cctv-fast.m3u"
    outdir = sys.argv[2] if len(sys.argv) > 2 else os.path.join("..", "out", "final")
    os.makedirs(outdir, exist_ok=True)

    tiles, rows, cur, idx = [], [], None, 0
    for raw in open(src, encoding="utf-8"):
        line = raw.strip()
        if line.startswith("#EXTINF"):
            cur = line.split(",")[-1].strip()
        elif line.startswith("http") and cur:
            idx += 1
            mbps, fr, why = probe(line)
            rows.append(f"{idx:02d} {cur:10s} {mbps:7.2f} Mbps  {line}")
            print(f"  {idx:02d} {cur:10s} {mbps:7.2f} Mbps  {'frame' if fr is not None else 'NONE'}")
            label = f"{idx:02d} {cur} {mbps:.1f}M"
            if fr is not None:
                h, w = fr.shape[:2]
                crop = fr[0:int(h * CROP_H), 0:int(w * CROP_W)]
                crop = cv2.resize(crop, (crop.shape[1] * SCALE, crop.shape[0] * SCALE),
                                  interpolation=cv2.INTER_CUBIC)
                tiles.append((label, crop))
            else:
                tiles.append((label, None))
            cur = None

    open(os.path.join(outdir, "report.txt"), "w", encoding="utf-8").write("\n".join(rows) + "\n")
    good = [t for t in tiles if t[1] is not None]
    if not good:
        sys.exit("nothing rendered")
    tw = max(t[1].shape[1] for t in good)
    th = max(t[1].shape[0] for t in good)
    rn = (len(tiles) + COLS - 1) // COLS
    pad = 24
    img = np.zeros((rn * (th + pad + 6), COLS * (tw + 8), 3), np.uint8)
    for i, (label, t) in enumerate(tiles):
        r, c = i // COLS, i % COLS
        y, x = r * (th + pad + 6), c * (tw + 8)
        if t is not None:
            img[y:y + t.shape[0], x:x + t.shape[1]] = t
        cv2.putText(img, label, (x + 2, y + th + 18),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 255), 1, cv2.LINE_AA)
    out = os.path.join(outdir, "sheet.png")
    cv2.imwrite(out, img)
    print("wrote", out)


if __name__ == "__main__":
    main()
