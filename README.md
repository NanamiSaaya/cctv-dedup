# CCTV 去重直播列表（云端自动更新）

给我家电视上的 my-t-v0 用的。上游是 [best-fan/iptv-sources](https://github.com/best-fan/iptv-sources)
（每 6 小时自动检测更新的 CCTV 源）。

## ⚠️ 两条血泪教训（2026-11-26）

**1. 按上游 `response-time` 排序 = 必然卡顿**

那个字段只测量了 m3u8 播放列表本身（几百字节）的响应速度，和分片码流吞吐毫无关系。
`123.160.235.204` 播放列表 34 ms 秒回，但分片被限速到 **0.5~1.3 Mbps**，
而 1080p 需要 3~5 Mbps → 缓冲欠载 → 反复重连。

**2. 带宽够 ≠ 台对**

只看带宽修完卡顿后，仍有 5 个频道看不了：

| 频道 | 实际播的是 |
| --- | --- |
| CCTV-2 | CCTV-1 综合 |
| CCTV-5+ | CCTV-5 体育 |
| CCTV-13 | TVU 测试卡（带宽 54 Mbps！） |
| CCTV-14 | TVU 测试卡 |
| CCTV-16 | CCTV-17 农业农村 |

**节点编号也完全不可信**：同一套 `tsfile/live/xxxx` 在不同 IP 上指向不同频道
（`60.223.94.153:9003/hls/16` = 山西卫视，`110.72.84.44:808/hls/16` = CCTV-5+，
`221.7.175.154:8445/.../1016` = CCTV-5）。**只能看台标，不能靠编号推断。**

所以流程改成：**云端只供候选池，本地测带宽 + 人工核台标**。

**3. 断流卡顿：分片级停顿，跟带宽无关**

实测过很多「平均 10 Mbps 但单个分片 113 秒」的源。平均带宽看着够，
播放器却每几秒卡一次——因为**真实判据是「每个分片能否在 TARGETDURATION 内下完」**。

停顿只发生在「分片刚生成、服务器还没写完」的时刻，是 IPTV 源服务器端行为。
对同一分片反复请求，完整时稳定 14~35 Mbps / 1~3 秒完成。

**所以 `speedrank.py`（只测 4 秒吞吐）不够用，必须用 `stability.py`。**

关于代理：对国内源**没有收益**（实测直连 25.9 Mbps vs 代理 34.9 Mbps，属噪声范围）。
代理只对海外源有意义。

## 文件

| 文件 | 作用 |
| --- | --- |
| `dedupe.py` | 抓取 + 去重，生成 `cctv.m3u` 候选池（每频道最多 8 条）。**Actions 跑这个** |
| `cctv.m3u` | 候选池（Actions 自动提交，不要手改） |
| `speedrank.py` | **本地跑**，按 4 秒吞吐排序（仅初筛，**不足以判断卡顿**） |
| `stability.py` | **本地跑，最重要的工具**：按分片级停顿筛源，2 秒快速失败、命中即停 |
| `fastseed.txt` | **人工核对过台标**的种子表，并进候选池防退回慢源 |
| `finalcheck.py` | **交付前终审**：带宽 + 台标全量出图（必跑） |
| `audiotrack.py` | 解析 PMT，看每个频道的音频编码（有声音/没声音的判据） |
| `audiopick.py` | 按音频是否本机可解（AAC）挑源，MP2/AC3 的源必然静音 |
| `logosheet.py` | 每条源台标区域放大拼图 |
| `pickbychan.py` | 指定频道的全部候选台标拼图 |
| `combo.py` | 带宽 + 台标同时出，一次定案 |
| `hunt.py` | 跨多个源列表撒网收集某频道候选 |
| `checkcontent.py` | 自动判据（辅助参考，**不能单独作准**） |
| `cctv-fast.m3u` | 最终结果，推给电视用 |
| `.github/workflows/update.yml` | 每 6 小时更新候选池 |

## 电视端配置

电视上**不用在线 config**（在线列表是云端按响应时间排的，拉下来还是错源/慢源）：

```
/sdcard/cctv-fast.m3u                                ← refresh.sh 推送
/data/data/com.lizongying.mytv0/files/channels.txt   ← 同上（uid 10041, chmod 600）
```

mytv0 prefs 里 `config_auto_load` 设为 `false`。

## 家里刷新流程

```bash
cd tv-fix && bash refresh.sh                 # 默认 192.168.124.5
cd tv-fix && bash refresh.sh 192.168.124.9   # IP 变了就传参
```

**改了源一定要核台标**（带宽高 ≠ 内容对）：

```bash
cd tv-fix/gh-dedup
python finalcheck.py cctv-fast.m3u ../out/final
# 打开 out/final/sheet.png 逐张看台号，对不上的从 fastseed.txt 删掉
```

## 当前状态

17 个频道全部台标正确、带宽 10~88 Mbps：CCTV-1/2/3/4/5/5+/6/7/8/9/10/11/12/13/14/15/17。

**CCTV-16 已移除** —— 所有已查节点都找不到真 CCTV-16，与其显示错误画面不如不放。
日后若找到，加进 `fastseed.txt` 并跑 `finalcheck.py` 核对。

## 关于「能看但没声音」

本机 `/system/lib` 只有这些音频解码器：

```
libstagefright_soft_aacdec / mp3dec / amrdec / g711dec / gsmdec / opusdec / vorbisdec / rawdec
```

**没有 MP2（MPEG-1/2 Audio），也没有 AC-3。** 而不少 CCTV 源的音频正是 MP2。

用 `audiotrack.py` 可以查每个频道到底是什么音频：

```bash
python audiotrack.py cctv-fast.m3u
# CCTV-3   [有] 有音频 AAC (ADTS)(pid=257)   ← 有声
# CCTV-13  [有] 有音频 MPEG-1 Audio(pid=46)  ← 静音
```

**解决方案**：在 mytv0 里打开「软解」（prefs 的 `soft_decode=true`），
软解路径能处理 MP2，就有声音了。这个开关不需要改源。

如果想让源本身都是 AAC，用 `audiopick.py`：

```bash
python audiopick.py cctv.m3u cctv-aac.m3u
```

## 排查

某频道看不了：

1. `out/refresh.log` 找该频道测速段，看有没有 ≥3 Mbps 的源
2. 有快源但仍不对 → `python pickbychan.py CCTVn` 看全部候选台标，挑对的
3. 全都不对 → 该频道当前确实没有可用源，先移除

