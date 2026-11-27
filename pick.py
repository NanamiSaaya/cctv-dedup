#!/usr/bin/env python3
"""
画质优先 + 余量充足的选源（最终版）。

设计目标（按用户要求）：
  1. **画质尽量高**：1080 > 720 > 576 > 未知。同档位里挑带宽最高的。
  2. **超过临界点留余量**：不能只看「不 stall」，还要
       - 速度 ≥ 目标码率 × 2（跑得动峰值）
       - 最慢分片耗时 ≤ 分片时长 × 0.6（留 40% 缓冲余量）
  3. **扫描要快**：
       - 并发 48
       - 播放列表 3 秒超时
       - 2 秒内拿不到 400KB 直接判死
       - 任何 stall 立刻放弃
       - 某频道一旦拿到「1080 + 余量充足」，立刻停止测它的低画质候选

判据细节（为什么这样定）：
  - 1080p H.264 直播实际码率约 3~6 Mbps，要求 ≥8 Mbps 才有余量
  - 720p 约 2~3 Mbps，要求 ≥5 Mbps
  - 576p 约 1~2 Mbps，要求 ≥3 Mbps
  - 「最慢分片 ≤ 分片时长×0.6」是为了应对服务器端分片写入延迟
    （实测有源平均 10 Mbps 但单分片 113 秒，那是分片未写完）

用法:
  python pick.py                  # 全频道
  python pick.py CCTV1,CCTV5      # 指定频道
  python pick.py "" 48            # 全频道 + 并发数
"""
import concurrent.futures as cf
import os
import re
import ssl
import sys
import tempfile
import threading
import time
import urllib.request
from collections import OrderedDict

# OpenCV 只在需要核实分辨率时用（解码开销大，缓存结果）
os.environ.setdefault("OPENCV_FFMPEG_LOGLEVEL", "-8")
os.environ.setdefault("OPENCV_LOG_LEVEL", "SILENT")
# OpenCV 的 ffmpeg 后端会直接写 stderr，用 dup2 临时静音
import contextlib
import sys as _sys


@contextlib.contextmanager
def _quiet_stderr():
    """解码时把 fd 2 重定向到 /dev/null，避免 ffmpeg 噪声刷屏。"""
    try:
        _sys.stderr.flush()
    except Exception:
        pass
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        old = os.dup(2)
        os.dup2(devnull, 2)
        try:
            yield
        finally:
            os.dup2(old, 2)
            os.close(old)
            os.close(devnull)
    except Exception:
        yield


try:
    import cv2
    cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_SILENT)
except Exception:
    cv2 = None

TMP = tempfile.gettempdir()
_res_cache = {}

UA = "okhttp/3.12.0"
_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE

# 强制 IPv4：Debian 那台机器 DNS 优先返回 IPv6，而 IPv6 出口不通，
# 会导致所有 HTTPS 源测出 000。测源必须直连且走 v4。
try:
    import socket as _socket
    _orig_getaddrinfo = _socket.getaddrinfo

    def _v4_only(host, port, family=0, type=0, proto=0, flags=0):
        return _orig_getaddrinfo(host, port, _socket.AF_INET, type, proto, flags)

    if os.environ.get("FORCE_V4", "1") == "1":
        _socket.getaddrinfo = _v4_only
except Exception:
    pass

# 测源绝不能走代理：代理会绕一圈，"分片停顿"就测不准了
for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
           "all_proxy", "ALL_PROXY"):
    os.environ.pop(_k, None)
os.environ["no_proxy"] = "*"
os.environ["NO_PROXY"] = "*"

WANT = [c.strip() for c in (sys.argv[1] if len(sys.argv) > 1 else "").split(",") if c.strip()]
WORKERS = int(sys.argv[2]) if len(sys.argv) > 2 else 48

# ---- 画质档位 → 要求 ----
# (像素档, 最低 Mbps, 余量系数: 最慢分片 <= 分片时长 * 余量)
TIERS = {
    1080: (8.0, 0.60),
    720:  (5.0, 0.65),
    576:  (3.0, 0.70),
    0:    (4.0, 0.65),   # 未知档位，按 720p 要求
}

MIN_BYTES_IN_2S = 200 * 1024     # 快速失败阈值（放宽，真正淘汰交给分片超时判据）
MAXSEC = 10                       # 单源最长观测
MIN_SEGS = 2

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
    except Exception:
        return got, time.time() - t0, True
    return got, time.time() - t0, False


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


def tier_of(tvg_name):
    """从 tvg-name 里读画质档位：CCTV1[1080][S] → 1080"""
    m = re.search(r"\[(\d{3,4})\]", tvg_name or "")
    if m:
        v = int(m.group(1))
        for t in (1080, 720, 576):
            if v >= t:
                return t
    return 0


def real_res(ts_bytes):
    """解码首帧拿真实分辨率。上游 [1080] 标注不可信，必须核实。"""
    if not cv2 or not ts_bytes:
        return None
    with _quiet_stderr():
        try:
            p = os.path.join(TMP, f"_pk_{os.getpid()}_{threading.get_ident()}.ts")
            open(p, "wb").write(ts_bytes)
            cap = cv2.VideoCapture(p)
            res = None
            for _ in range(150):
                ok, fr = cap.read()
                if ok and fr is not None:
                    h, w = fr.shape[:2]
                    res = (w, h)
                    break
            cap.release()
            try:
                os.remove(p)
            except Exception:
                pass
            return res
        except Exception:
            return None


def probe(url, tier):
    """返回质量指标。tier 只作为「预期档位」参考，真实画质靠解码核实。

    ⚠️ 单次测量不可靠：同一个源连测 5 次，最快 23 Mbps、最慢 3.5 Mbps 还带 stall。
    IPTV 源的分片停顿是概率性的，测一次就下结论会误杀好源、也会放过坏源。
    → 对「被判失败」的源再补测 1 次，两次取好的那次；只有连续两次都不行才淘汰。
    """
    first = _probe_once(url, tier)
    if first["margin_ok"] or first.get("dead"):
        return first
    # 没达标又没判死（说明是 stall 或速度不足）→ 补测一次
    second = _probe_once(url, tier)
    # 取更好的一次（按 margin_ok 优先，其次 stall/err 少，再其次 mbps 高）
    def rank(x):
        return (0 if x["margin_ok"] else 1, x["err"], x["stall"], -x["mbps"])
    return first if rank(first) <= rank(second) else second


def _probe_once(url, tier):
    dead_mbps, margin = TIERS.get(tier, TIERS[0])
    dead = {"ok": 0, "stall": 0, "err": 1, "mbps": 0.0, "worst": 0.0,
            "n": 1, "seglen": None, "dead": True, "margin_ok": False,
            "res": None, "res_px": 0}

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

    # --- 快速失败：用「倒数第 2 个」分片测速 ---
    # 不能用最后一个：那是最新分片，服务器常常还没写完，
    # 实测会把 7 Mbps 的好源误判成 dead（这个 bug 让达标率从 14/18 掉到 2/18）。
    probe_seg = segs[-2][1] if len(segs) >= 2 else segs[-1][1]
    su = probe_seg if probe_seg.startswith("http") else f"{base}/{probe_seg}"
    got, dt, failed = read_for(su, 2.0, timeout=8)
    quick_mbps = got * 8 / 1e6 / dt if dt else 0
    if failed and got == 0:
        return dead
    # 阈值放宽到 200KB/2s：只要不是烂到没法看，就给它进入逐分片验证的机会，
    # 真正的淘汰交给后面的「分片是否超时」判据。
    if got < 200 * 1024:
        return {"ok": 0, "stall": 0, "err": 1, "mbps": quick_mbps,
                "worst": dt, "n": 1, "seglen": segdur, "dead": True,
                "margin_ok": False, "res": None, "res_px": 0}

    # --- 逐分片验证 ---
    t_end = time.time() + MAXSEC
    seen, oks, stalls, errs = set(), 0, 0, 0
    tb, tt, worst = 0, 0.0, 0.0
    sample = None
    while time.time() < t_end:
        try:
            body, _ = fetch_all(url, timeout=3)
        except Exception:
            errs += 1
            time.sleep(0.3)
            continue
        segs, t2 = parse(body.decode("utf-8", "ignore"))
        if t2:
            segdur = t2
        # 只取倒数第 2、3 个：最后一个常是「刚生成但服务器还没写完」的分片，
        # 拉它会长时间挂住。真实播放器也会自动落后一两个分片，所以这样更贴近实际。
        cands = segs[-3:-1] if len(segs) >= 3 else segs[:-1] if len(segs) > 1 else segs
        for dur, seg in cands:
            s = seg if seg.startswith("http") else f"{base}/{seg}"
            if s in seen:
                continue
            seen.add(s)
            expect = dur or segdur
            ts = time.time()
            try:
                d, _ = fetch_all(s, timeout=int(expect * 1.5) + 2)
                el = time.time() - ts
                tb += len(d)
                tt += el
                worst = max(worst, el)
                if sample is None and len(d) > 30000:
                    sample = d
                if el <= expect:
                    oks += 1
                else:
                    stalls += 1
                    break
            except Exception:
                errs += 1
                break
        if stalls or errs:
            break
        if oks >= MIN_SEGS:
            mb = tb * 8 / 1e6 / max(tt, 0.001)
            if mb >= dead_mbps and worst <= segdur * margin:
                break
        time.sleep(0.3)

    mbps = tb * 8 / 1e6 / tt if tt else 0.0

    # 核实真实分辨率（只对已达标或接近达标的源做，省时间）
    res = None
    res_px = 0
    if sample and stalls == 0 and errs == 0 and oks >= MIN_SEGS:
        key = url
        if key in _res_cache:
            res = _res_cache[key]
        else:
            res = real_res(sample)
            _res_cache[key] = res
        if res:
            res_px = res[1]      # 用垂直分辨率做档位

    margin_ok = (stalls == 0 and errs == 0 and oks >= MIN_SEGS
                 and mbps >= dead_mbps and worst <= segdur * margin)
    return {"ok": oks, "stall": stalls, "err": errs, "mbps": mbps,
            "worst": worst, "n": oks + stalls + errs, "seglen": segdur,
            "dead": False, "margin_ok": margin_ok,
            "res": res, "res_px": res_px}


def main():
    chans, meta, cur = OrderedDict(), {}, None
    # 用上游原始列表，才有 [1080]/[720] 标注
    path = None
    for cand in ("../out/upstream.m3u8", "candidates.m3u", "upstream.m3u8", "cctv.m3u"):
        import os
        if os.path.exists(cand):
            path = cand
            break
    if not path:
        sys.exit("找不到源列表")
    print(f"源列表: {path}\n", flush=True)

    for raw in open(path, encoding="utf-8"):
        line = raw.strip()
        if line.startswith("#EXTINF"):
            tn = (re.search(r'tvg-name="([^"]*)"', line) or [None, ""])[1]
            logo = (re.search(r'tvg-logo="([^"]*)"', line) or [None, ""])[1]
            base = re.match(r"CCTV-?(\d+)(\+?)", tn)
            if not base:
                cur = None
                continue
            key = f"CCTV{base.group(1)}{'+' if base.group(2) else ''}"
            cur = key
            tier = tier_of(tn)
            chans.setdefault(key, {"meta": (logo, line.split(",")[-1].strip()),
                                   "urls": []})
            cur_meta = chans[key]
            cur = (key, tier)
        elif line.startswith("http") and cur:
            key, tier = cur
            chans[key]["urls"].append((tier, line))
            cur = None

    targets = [c for c in WANT if c in chans] or list(chans)
    # 每个频道内按画质降序排，先测高画质
    for k in chans:
        chans[k]["urls"].sort(key=lambda x: -x[0])
        chans[k]["urls"] = chans[k]["urls"][:14]   # 每频道最多测 14 条

    solved = {}        # ch -> (url, tier, st)
    best_fb = {}       # ch -> (url, tier, st)  没达标的兜底
    stop = threading.Event()
    t0 = time.time()

    def work(ch, tier, url):
        if stop.is_set():
            return
        with lock:
            if ch in solved and solved[ch][2]["res_px"] >= 1080:
                return          # 已经拿到 1080 达标源，不必再测
        try:
            st = probe(url, tier)
        except Exception:
            return
        with lock:
            if ch in solved:
                cur = solved[ch][2]
                # 只有在「画质更高且仍达标」时才替换
                if st["margin_ok"] and st["res_px"] > cur["res_px"]:
                    solved[ch] = (url, tier, st)
                    print(f"↑ {ch:8s} 升级到 {st['res_px']}p  "
                          f"{st['mbps']:6.1f}M  {url}", flush=True)
                return
            if st["margin_ok"]:
                solved[ch] = (url, tier, st)
                tag = "★"
            else:
                old = best_fb.get(ch)
                def sc(x):
                    return (x["err"] * 20 + x["stall"] * 5
                            + (0 if x["margin_ok"] else 10)
                            - x["res_px"] / 1000.0)
                if old is None or sc(st) < sc(old[2]):
                    best_fb[ch] = (url, tier, st)
                tag = "." if st.get("dead") else " "
            res_s = f"{st['res_px']}p" if st["res_px"] else "?"
            print(f"{tag} {ch:8s} 标{tier or '?':>4} 实{res_s:>5} "
                  f"ok={st['ok']:<2d} stall={st['stall']:<2d} err={st['err']:<2d} "
                  f"{st['mbps']:6.1f}M worst={st['worst']:5.1f}s "
                  f"[{len(solved)}/{len(targets)} {time.time()-t0:.0f}s]", flush=True)
            if len(solved) >= len(targets):
                stop.set()

    order = []
    for i in range(max((len(chans[t]["urls"]) for t in targets), default=0)):
        for t in targets:
            if i < len(chans[t]["urls"]):
                tier, u = chans[t]["urls"][i]
                order.append((t, tier, u))
    order = list(dict.fromkeys(order))

    print(f"{len(targets)} 频道 / {len(order)} 候选，{WORKERS} 并发，"
          f"画质优先、慢源 2 秒即弃\n", flush=True)
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(work, ch, ti, u) for ch, ti, u in order]
        for f in cf.as_completed(futs):
            if stop.is_set():
                for x in futs:
                    x.cancel()
                break

    print("\n" + "=" * 92)
    out = ["#EXTM3U"]
    n_clean = 0
    for ch in targets:
        logo, title = chans[ch]["meta"]
        if ch in solved:
            u, tier, st = solved[ch]
            n_clean += 1
            res_s = f"{st['res_px']}p" if st["res_px"] else f"{tier}p?"
            seglen = st["seglen"] or 0
            print(f"  ★ {ch:8s} {res_s:>6}  {st['mbps']:6.1f}M  "
                  f"最慢分片 {st['worst']:.1f}s / 分片长 {seglen:.1f}s "
                  f"(余量 {(1-st['worst']/seglen)*100:.0f}%)")
            print(f"      {u}")
        elif ch in best_fb:
            u, tier, st = best_fb[ch]
            res_s = f"{st['res_px']}p" if st["res_px"] else f"{tier}p?"
            print(f"  ! {ch:8s} {res_s:>6}  {st['mbps']:6.1f}M  "
                  f"stall={st['stall']} err={st['err']}（未达标，兜底）")
            print(f"      {u}")
        else:
            print(f"  X {ch:8s} 无结果")
            continue
        out.append(f'#EXTINF:-1 tvg-name="{ch}" tvg-logo="{logo}" group-title="央视",{title}')
        out.append(u)

    open("picked.m3u", "w", encoding="utf-8").write("\n".join(out) + "\n")
    print(f"\n耗时 {time.time()-t0:.0f}s，达标 {n_clean}/{len(targets)}，wrote picked.m3u")


if __name__ == "__main__":
    main()