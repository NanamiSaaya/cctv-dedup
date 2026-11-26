# CCTV 去重直播列表（云端自动更新）

给我家电视上的 my-t-v0 用的。上游是 [best-fan/iptv-sources](https://github.com/best-fan/iptv-sources)
（每 6 小时自动检测更新的 CCTV 源），本仓库用 GitHub Actions 每 6 小时抓取一次，
去重后生成 `cctv.m3u`：每个 CCTV 频道只保留一个条目，附最多 2 个备用线路。

## 电视端配置

mytv0 的「远程配置」URL 设为：

```
https://raw.githubusercontent.com/NanamiSaaya/cctv-dedup/main/cctv.m3u
```

并打开「应用启动后更新视频源」。app 会对 raw.githubusercontent.com 自动尝试多个加速代理，
所以电视直连 GitHub 不通也能拉到。

## 文件

- `dedupe.py` — 抓取 + 去重脚本
- `cctv.m3u` — 生成结果（Actions 自动提交，不要手改）
- `.github/workflows/update.yml` — 每 6 小时定时更新
