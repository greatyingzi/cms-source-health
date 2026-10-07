# cms-source-health

定时**自动发现**并**体检**一批苹果CMS(macCMS) 采集站接口，产出机器可读的清单，供播放器 / 聚合站取用。

> 很多影视聚合项目的片源都来自这类"采集站"接口（`?ac=detail&pg=` 取目录、`?ac=videolist&wd=` 搜片）。
> 这类站点会换域名、限流、整族失效，**没有"永久可靠"的源** —— 所以候选池要能自己长大，健康度要持续体检。
> 本仓库两件事都做：**进料（发现新接口）+ 体检（验证可用性）**。

<details>
<summary><b>English TL;DR</b></summary>

A scheduled harvester + health-checker for macCMS ("采集站") VOD APIs.

- **Discovery**: pulls candidate API endpoints from public TVBox configs / aggregator posts, dedupes by canonical URL, appends them to `candidates.txt`.
- **Health check** (every 6h, deep run daily): API reachable → playlist is `#EXTM3U` → first segment fetchable. Outputs `sources.json`, `dead.txt`, `streams-blocked.txt`.
- **Vantage caveat**: checks run from GitHub Actions (datacenter IP, overseas). *Unreachable from here ⇒ probably unusable*, but **reachable here does NOT mean usable in China** — some CN CDNs block datacenter/overseas IPs entirely. This repo **filters, it does not judge**; consumers must re-check on their own network (same three steps).
- No dependencies (Python stdlib only), no credentials, no private data.

</details>

## 仓库结构

| 路径 | 作用 |
|---|---|
| `scripts/discover.py` | **进料口**：从公开渠道挖新接口 → 追加 `candidates.txt` |
| `scripts/check_sources.py` | **体检**：三步检查 → 写 `sources.json` / `dead.txt` / `streams-blocked.txt` |
| `candidates.txt` | 候选池（人工 PR + 自动发现，**加源就改这个文件**） |
| `.github/workflows/check.yml` | 定时编排（发现 → 体检 → 提交结果 → 异常开 issue） |
| `sources.json` | 机器可读结果 + 每源历史曲线 |
| `dead.txt` / `streams-blocked.txt` | 两级信号清单（见下） |

## 产出

| 文件 | 内容 |
|---|---|
| `sources.json` | 每个候选源的检查结果 + 历史曲线（机器可读，字段见下） |
| `dead.txt` | **接口本身不可用**（强信号：接口没了/换域名了/需 IP 白名单） |
| `streams-blocked.txt` | **接口通但流在海外云出口全失败**（弱信号：很可能是 CDN 只拦机房 IP，**别据此判死**） |
| `candidates.txt` | 候选池 |

> ⚠️ 这两个清单的区别是实测换来的：本项目自己的四个在用源里，有两个的 CDN **只拦机房 IP** ——
> 从 GitHub Actions(Azure 美国) 拉流全部 404，但从普通海外出口(如东京家宽/代理)与国内出口都 100% 可播。
> 若把它们按"海外不通"剔除，等于自断主源。所以清单必须分两级。

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

`reason` 取值：`api http <code>` / `api not json` / `api empty list` / `no play url in feeds` /
`all sampled streams dead`（后两者表示接口活着但抽到的流播不了）。

## 检查了什么（三步递进）

1. **API 可达** — `GET {api}?ac=detail&pg=1` 必须是 HTTP 200、可 JSON 解析、`list` 非空
2. **播放列表可读** — 抽样若干条，解析 `vod_play_url`（`集名$地址#…`）取首条地址，必须拿到 `#EXTM3U`（若是 master 播放列表会再跟一层变体）
3. **分片可取** — 在 media 列表里抽 1 个分片，必须 HTTP 200 且体积 > 10KB

## ⚠️ 结论怎么用（很重要）

本仓库的检查跑在 **GitHub Actions 的海外机房出口**：

- **海外不通 → 大概率不可用**（必要条件不满足，可降权/排除）
- **海外通 → 不代表国内可用**：不少国内 CDN 会拦截机房/海外 IP，也有反过来对国内某些运营商差的
- 因此**本仓库只做筛选，不做最终裁决**。使用方必须在自己的网络里复测（同样的三步）后再采纳 ——
  `sources.json` 里的字段名刻意写成 `overseas` 就是为了提醒这一点
- 同理，`dead.txt` 建议只用于**新候选的降权**（不要拿它删掉自己已经在用、且本地验证过的源）

## 运行

GitHub Actions 自动跑（见 `.github/workflows/check.yml`）：

| 任务 | 频率 | 说明 |
|---|---|---|
| **自动发现新候选** | 每次运行 | 从公开的 TVBox/接口汇总里捞新接口，去重后追加 `candidates.txt` |
| 快速筛 | 每 6 小时 | 每源抽 3 条 |
| 深度筛 | 每天 03:00 UTC | 每源抽 8 条（结果更可靠） |

结果自动 commit 回仓库。异常通知走 **GitHub 自身**（不需要任何 IM/邮件凭据）：
**所有候选源全灭** 或 **检查器运行失败** 时，自动开 issue 并 **assign 给仓库 owner** ——
assignment 会触发 GitHub 的邮件通知。你也可以在 Actions 页面 `Run workflow` 手动触发，或本地跑：

```bash
python3 scripts/discover.py               # 只挖新候选(追加 candidates.txt)
python3 scripts/check_sources.py          # 快速筛
python3 scripts/check_sources.py --deep   # 深度筛
```

只依赖 Python 标准库，无需第三方包。

### 候选池怎么长大

`scripts/discover.py` 每次运行都会从公开渠道捞新接口写进 `candidates.txt`：

1. **TVBox/影视仓接口配置**（raw JSON，里面的 `url`/`api` 字段就是采集接口；GitHub raw 走镜像兜底）
2. **公开的"采集接口分享"文章**（HTML 直接正则捞地址）
3. **多仓二级展开**（配置内容若是一串配置文件地址，再跟一层）

去重按**规范化地址**（剥掉 `/at/xml`、`/from/xxx/`、查询串）——否则同一源的 XML 变体会被当成新源。
每轮最多追加 40 条，避免候选池爆炸。进料口是公开网页/仓库，**失效了不影响运行**（有几个互补）。

## 给使用方（消费契约）

1. 读 `sources.json`；想省事也可以只读 `dead.txt`（排除）与 `streams-blocked.txt`（降权）
2. 用 `api` 字段作**规范化去重键**（不要用 `key`：不同来源派生的 key 可能不同，会重复入库）
3. 把结果当**候选**而不是终选：在**你自己的出口**上跑同样的三步，通过才入池
4. 源被淘汰时，同步清掉它在你库里的线路（避免用户点到死链）

## 参与

- **加源**：PR `candidates.txt`，一行一条：`名称|接口地址`
- **提问题/建议**：开 issue
- **本仓库不接受**：任何密钥、私有域名、账号、个人信息 —— 这里只放**公开**的接口地址

## 许可

MIT
