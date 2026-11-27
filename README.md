# CCTV 直播列表（云端发布 + 电视自动拉取）

给家里飞利浦 43PUF6061（Android 5.1、hi3751 双核 A7、1GB 内存）上的 **my-tv-0** 用。

## 架构

```
家庭宽带（本机）
  ├─ dedupe.py     抓 best-fan 上游 → candidates.m3u（候选池，不排序）
  ├─ pick.py       逐条实测 → picked.m3u（画质优先 + 余量判据）
  └─ publish.py    推 GitHub + 配置电视
          ↓ push
   GitHub  NanamiSaaya/cctv-dedup  →  cctv.m3u
          ↓ ghproxy.net（电视直连 GitHub 会被重置）
  电视 mytv0  开机/启动时按 config URL 自动拉取
```

电视端配置（`shared_prefs/我的電視·〇.xml`）：

```xml
<boolean name="config_auto_load" value="true" />
<string name="config">https://ghproxy.net/https://raw.githubusercontent.com/NanamiSaaya/cctv-dedup/master/cctv.m3u</string>
```

开关在 app 里叫「应用启动后更新视频源」。

## 一条命令搞定

```bash
cd tv-fix/gh-dedup
python publish.py            # 只选源 + 推 GitHub
python publish.py --tv       # 另外配置电视并让它立刻拉取
```

`publish.py` 会依次做：
1. `pick.py` 选源（约 20~60 秒，48 并发）
2. 写入 `cctv.m3u` 并 push（自动处理 Actions 造成的冲突，保留本地版本）
3. 从 ghproxy 校验线上确实是新版
4. （`--tv`）改电视 prefs + 重启 app + 确认 `导入频道 N`

## 三条血泪教训

### 1. 别按 `response-time` 排序

上游 m3u8 的 `response-time` 只测了**播放列表本身（几百字节）** 的响应，
和分片码流吞吐毫无关系。曾经因此把 18 个频道里 17 个都排到
`123.160.235.204`——它播放列表 34 ms 秒回，但分片被限速到 0.5~1.3 Mbps，
1080p 需要 3~5 Mbps，必然卡。

### 2. 带宽够 ≠ 能播：要看分片级停顿

实测反例：平均 10 Mbps 的源，单个分片却要 **113 秒**才下完。

停顿只发生在**分片刚生成、服务器还没写完**时。对同一分片反复请求，
完整时稳定 14~35 Mbps / 1~3 秒完成。这是源服务器端行为，与链路无关。

**所以判据必须是「每个分片能否在 TARGETDURATION 内下完」，而且要留余量。**

### 3. 带宽够 ≠ 台对：必须核台标

上游 `tvg-name` 里的 `[1080]` 标注**不可信**，实测 CCTV-3 标 1080 实际只有 576p。
更严重的是内容错配：曾经 CCTV-2 播的是 CCTV-1、CCTV-5+ 播的是 CCTV-5、
CCTV-13/14 是 TVU 测试卡、CCTV-16 是 CCTV-17。

节点编号也完全不可信——同一套 `tsfile/live/xxxx` 在不同 IP 上指向不同频道
（`60.223:9003/hls/16` = 山西卫视，`110.72.84.44/hls/16` = CCTV-5+）。

→ `pick.py` 会**解码首帧核实真实分辨率**。改源后务必人工看一眼台标。

## pick.py 的判据

| 档位 | 最低带宽 | 最慢分片允许耗时 |
| --- | --- | --- |
| 1080p | ≥ 8 Mbps | ≤ 分片时长 × 0.60 |
| 720p | ≥ 5 Mbps | ≤ 分片时长 × 0.65 |
| 576p | ≥ 3 Mbps | ≤ 分片时长 × 0.70 |

速度靠快速失败保证：播放列表 3 秒超时、**2 秒内拿不到 400KB 直接判死**、
任何 stall 立刻放弃、某频道达标后不再测它的低画质候选。

## 关于代理

**对国内源没有收益。** 实测同一分片：直连 25.9 Mbps vs 本地代理 34.9 Mbps，
属噪声范围。代理不改变「终点服务器还没写完分片」这个事实。

代理只对海外源有意义。实测海外节点（74.91.x / 198.204.x / 69.30.x）：
播放列表可达 7/9，但分片大量 404，最好的一条也只有 1.5~2.3 Mbps、耗时 8~13 秒。
所以现在全部用国内源。

`ghproxy.net` 是另一回事——它不是加速代理，而是让**电视能访问 GitHub raw**。
电视直连 `raw.githubusercontent.com` 会被 `Connection reset`，走 ghproxy 正常。

## 关于没声音

本机 `/system/lib` 只有这些音频解码器：

```
libstagefright_soft_aacdec / mp3dec / amrdec / g711dec / gsmdec / opusdec / vorbisdec / rawdec
```

**没有 MP2（MPEG-1/2 Audio），也没有 AC-3。** 而不少 CCTV 源的音频正是 MP2，
所以画面正常但完全静音。

解决方案：app 里打开「软解」（prefs 的 `soft_decode=true`），不用换源。

## 文件

| 文件 | 作用 |
| --- | --- |
| `publish.py` | **主入口**：选源 → 推送 → 校验 → 配置电视 |
| `pick.py` | 选源核心：画质优先 + 分片级稳定性 + 余量判据 |
| `dedupe.py` | 抓上游生成候选池 `candidates.m3u`（Actions 跑这个） |
| `cctv.m3u` | **线上成品**，电视拉这个（不要手改） |
| `fastseed.txt` | 已人工核对过台标的高质量节点种子表 |
| `.github/workflows/update.yml` | 每 6 小时更新候选池，**不覆盖 cctv.m3u** |

## 排查

某个频道卡或不对：

```bash
python pick.py CCTV7 48        # 单频道重测，看全部候选的实测指标
```

输出里每条都有 `标1080 实1080p ok=2 stall=0 err=0 24.2M worst=2.2s`，
直接能看出是带宽不够、停顿、还是画质不够。

改完后 `python publish.py --tv` 一键生效。