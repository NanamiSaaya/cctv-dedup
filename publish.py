#!/usr/bin/env python3
"""
选源 → 写入仓库 → 提交推送 → 配置电视自动拉取。一条龙。

为什么这样做（而不是 adb push 本地文件）：
  电视每次开机自启时 mytv0 会读 prefs 里的 config URL 拉最新列表，
  所以只要把新列表推到 GitHub，电视下次启动/手动刷新就会自动用上。
  不用每次 adb push，也不怕 adb 掉线。

关键点（都是踩过的坑）：
  1. 电视**直连 raw.githubusercontent.com 会被重置**，必须用 ghproxy.net 前缀。
     实测：直连 curl: (35) Connection was reset；ghproxy 正常 200。
  2. 仓库的 cctv.m3u 会被 GitHub Actions 每 6 小时覆盖。
     所以本地验证过的版本要 push，并且 Actions 只负责产出候选池时不能覆盖成品。
     → 本脚本把成品写到 cctv.m3u，同时保留 picked.m3u 作为带质量标注的版本。
  3. 电视端 prefs 里必须：
       config_auto_load = true
       config = https://ghproxy.net/https://raw.githubusercontent.com/.../cctv.m3u
  4. 本机没有 sed/stat，改 prefs 要 PC 侧改完再 push，并 chown 10041:10041。

用法:
  python publish.py              # 选源 + 推送 GitHub（不动电视）
  python publish.py --tv         # 另外配置电视立即拉取
"""
import os
import re
import shutil
import subprocess
import sys
import ssl
import time
import urllib.request

REPO = os.path.dirname(os.path.abspath(__file__))
TOP = os.path.dirname(REPO)
ADB = os.environ.get("ADB", "C:/Game/MuMuPlayer-12.0/shell/adb.exe")
TV = os.environ.get("TV_HOST", "192.168.124.5") + ":5555"
GH_USER = "NanamiSaaya"
GH_REPO = "cctv-dedup"
GH_BRANCH = "master"
RAW = f"https://raw.githubusercontent.com/{GH_USER}/{GH_REPO}/{GH_BRANCH}/cctv.m3u"
PROXY_URL = f"https://ghproxy.net/{RAW}"

UA = "okhttp/3.12.0"
_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", **kw)


def adb(*args, timeout=40):
    return run([ADB, "-s", TV] + list(args), timeout=timeout)


def adb_connect():
    run([ADB, "connect", TV], timeout=20)
    time.sleep(4)
    st = adb("get-state")
    return "device" in (st.stdout or "")


def verify_url(url, tries=3):
    """确认 URL 真的能拉到 m3u8。"""
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20, context=_ctx) as r:
                body = r.read().decode("utf-8", "ignore")
            n = body.count("#EXTINF")
            if n >= 10:
                return n
        except Exception as e:
            print(f"  try{i+1}: {e}")
        time.sleep(3)
    return 0


def pick_sources():
    """跑 pick.py 生成 picked.m3u。"""
    print("=== 1. 选源（画质优先 + 余量判据）===", flush=True)
    r = run([sys.executable, "pick.py", "", "48"], cwd=REPO,
            timeout=1200)
    out = (r.stdout or "") + (r.stderr or "")
    for line in out.splitlines():
        if line.startswith("★") or line.startswith("!") or "耗时" in line:
            print("  " + line)
    p = os.path.join(REPO, "picked.m3u")
    if not os.path.exists(p):
        sys.exit("pick.py 没生成 picked.m3u")
    return p


def publish(path):
    print("\n=== 2. 写入仓库并推送 ===", flush=True)
    dst = os.path.join(REPO, "cctv.m3u")
    shutil.copyfile(path, dst)
    n = open(dst, encoding="utf-8").read().count("#EXTINF")
    print(f"  cctv.m3u: {n} 频道")

    git = ["git", "-c", f"user.name={GH_USER}",
           "-c", f"user.email={GH_USER}@users.noreply.github.com"]
    run(git[:1] + git[1:] + ["add", "-A"], cwd=REPO)
    subprocess.run(git + ["add", "-A"], cwd=REPO,
                   capture_output=True, text=True)
    subprocess.run(git + ["commit", "-m",
                          f"chore: 更新频道表 {time.strftime('%F %H:%M')} "
                          f"({n} 频道，画质优先)"],
                   cwd=REPO, capture_output=True, text=True)
    # 远程可能被 Actions 推送过，先 rebase 再推
    for attempt in range(2):
        p = subprocess.run(git + ["push", "origin", GH_BRANCH], cwd=REPO,
                           capture_output=True, text=True)
        if p.returncode == 0:
            print("  push OK")
            return True
        print("  push 失败，尝试 rebase …")
        subprocess.run(git + ["pull", "--rebase", "--no-edit",
                              "origin", GH_BRANCH],
                       cwd=REPO, capture_output=True, text=True)
        # 冲突时保留本地版本
        subprocess.run(["git", "checkout", "--ours", "cctv.m3u"], cwd=REPO,
                       capture_output=True, text=True)
        subprocess.run(["git", "add", "cctv.m3u"], cwd=REPO,
                       capture_output=True, text=True)
        subprocess.run(["git", "-c", "core.editor=true", "rebase",
                        "--continue"], cwd=REPO, capture_output=True, text=True)
    return False


def configure_tv():
    print("\n=== 3. 配置电视自动拉取 ===", flush=True)
    if not adb_connect():
        print(f"  adb 未连接（{TV}）。电视可能待机了，先唤醒再重跑")
        return False

    prefs = "/data/data/com.lizongying.mytv0/shared_prefs/我的電視·〇.xml"

    print("  停 app（否则退出时会覆盖 prefs）")
    adb("shell", "am force-stop com.lizongying.mytv0")
    time.sleep(2)

    local = os.path.join(TOP, "out", "prefs_tv.xml")
    r = adb("pull", prefs, local)
    if not os.path.exists(local):
        print(f"  拉取 prefs 失败: {(r.stderr or '')[:120]}")
        return False

    s = open(local, encoding="utf-8").read()
    s = re.sub(r'<boolean name="config_auto_load" value="[^"]*" />',
               '<boolean name="config_auto_load" value="true" />', s)
    if "config_auto_load" not in s:
        s = s.replace("</map>",
                      '    <boolean name="config_auto_load" value="true" />\n</map>')
    s = re.sub(r'(<string name="config">)[^<]*(</string>)',
               lambda m: m.group(1) + PROXY_URL + m.group(2), s)
    if 'name="config"' not in s:
        s = s.replace("</map>",
                      f'    <string name="config">{PROXY_URL}</string>\n</map>')
    open(local, "w", encoding="utf-8").write(s)
    print(f"  config = {PROXY_URL}")
    print("  config_auto_load = true")

    adb("push", local, prefs)
    adb("shell", f"chown 10041:10041 '{prefs}'; chmod 660 '{prefs}'")
    v = adb("shell", f"busybox grep -oE '<(string|boolean) name=\"(config|config_auto_load)\"[^/]*' '{prefs}'")
    print("  远端确认:")
    for line in (v.stdout or "").splitlines():
        print("    " + line.strip())

    print("  重启 app，让它拉取")
    adb("shell", "logcat -c")
    adb("shell", "am start -n com.lizongying.mytv0/.MainActivity")
    time.sleep(25)

    imp = adb("shell",
              "logcat -d -v brief 2>/dev/null | busybox grep -oE '导入频道 [0-9]+' | busybox tail -1")
    print(f"  {(imp.stdout or '').strip() or '（没看到导入日志）'}")
    err = adb("shell",
              "echo -n pb_err=; logcat -d -v time 2>/dev/null | busybox grep -c 'Playback error'; "
              "echo -n ' qfail='; logcat -d -v time 2>/dev/null | busybox grep -c 'queue frame failed'")
    print(f"  {(err.stdout or '').strip()}")
    return True


def main():
    n = verify_url(PROXY_URL)
    if n >= 10:
        print(f"=== 0. 仓库当前已有 {n} 频道（将覆盖）===")

    p = pick_sources()
    ok = publish(p)
    n2 = verify_url(PROXY_URL)
    print(f"\n=== 4. 校验线上 ===\n  ghproxy 拉到 {n2} 频道")
    if n2 < 10:
        print("  ⚠️ 线上校验失败，电视可能拉不到。检查 ghproxy 或仓库分支名")

    if "--tv" in sys.argv:
        configure_tv()

    print("\n完成。电视端 URL：")
    print(f"  {PROXY_URL}")


if __name__ == "__main__":
    main()