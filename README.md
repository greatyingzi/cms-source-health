# cms-source-health

定时检查一批 **苹果CMS(macCMS) 采集站接口**是否还活着，产出机器可读的清单，供播放器 / 聚合站取用。

> 很多影视聚合项目的片源都来自这类"采集站"接口（`?ac=detail&pg=` 取目录、`?ac=videolist&wd=` 搜片）。
> 这类站点会换域名、限流、整族失效，**没有"永久可靠"的源**，因此需要持续体检。

## 产出

| 文件 | 内容 |
|---|---|
| `sources.json` | 每个候选源的检查结果 + 历史曲线（机器可读，字段见下） |
| `dead.txt` | 本次在海外出口**完全失败**的接口列表（一行一个） |
| `candidates.txt` | 候选池（**加源就 PR 这个文件**） |

`sources.json` 单条结构：

```json
{
  "key": "example-com", "name": "示例源", "api": "https://example.com/api.php/provide/vod",
  "first_seen": 1759900000, "last_ok": 1759900000, "last_fail": null,
  "overseas": { "api_ok": true, "sampled": 3, "playable": 3, "rate": 1.0,
                "reason": "", "ms": 812, "checked_at": 1759900000 },
  "history": [{ "at": 1759900000, "rate": 1.0, "api_ok": true }]
}
```

## 检查了什么（三步递进）

1. **API 可达** — `GET {api}?ac=detail&pg=1` 必须是 HTTP 200、可 JSON 解析、`list` 非空
2. **播放列表可读** — 抽样若干条，解析 `vod_play_url`（`集名$地址#…`）取首条地址，必须拿到 `#EXTM3U`（若是 master 播放列表会再跟一层变体）
3. **分片可取** — 在 media 列表里抽 1 个分片，必须 HTTP 200 且体积 > 10KB

## ⚠️ 结论怎么用（很重要）

本仓库的检查跑在 **GitHub Actions 的海外出口**：

- **海外不通 → 该源基本不可用**（必要条件下不满足，可以据此剔除/降权）
- **海外通 → 不代表国内可用**：不少国内 CDN 会拦截海外 IP，也有反过来对国内某些运营商差的

所以：**本仓库只做筛选，不做最终裁决。** 使用方必须在自己的网络里复测（同样的三步）后再决定是否采纳——
`sources.json` 里的字段名刻意写成 `overseas`，就是为了提醒这一点。

## 运行

GitHub Actions 自动跑（见 `.github/workflows/check.yml`）：

| 任务 | 频率 | 抽样 |
|---|---|---|
| 快速筛 | 每 6 小时 | 每源 3 条 |
| 深度筛 | 每天 03:00 UTC | 每源 8 条 |

结果自动 commit 回仓库；**所有候选源全灭**时会开一个 issue 提醒补源。
也可以在 Actions 页面 `Run workflow` 手动触发，或本地跑：

```bash
python3 scripts/check_sources.py          # 快速筛
python3 scripts/check_sources.py --deep   # 深度筛
```

只依赖 Python 标准库，无需第三方包。

## 参与

- **加源**：PR `candidates.txt`，一行一条：`名称|接口地址`
- **提问题/建议**：开 issue
- **本仓库不接受**：任何密钥、私有域名、账号、个人信息 —— 这里只放**公开**的接口地址

## 许可

MIT
