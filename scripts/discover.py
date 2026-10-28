#!/usr/bin/env python3
"""候选源自动发现 —— 从公开的接口汇总里持续挖新的 macCMS 采集接口

为什么必须有这一步: 采集站会整族失效/换域名, 候选池不自己长大, 体检就只是在检查
若干手工填的源, 失去了意义。本脚本是候选池的"进料口"。

来源(公开, 无需凭证):
  1. TVBox/影视仓 接口配置文件(raw JSON; 里面的 url/api 字段就是采集接口)
  2. 公开的"采集接口分享"文章(HTML, 直接正则捞地址)
  3. 二级展开: 配置文件里若是"多仓"(内容是 .json/.txt 地址列表), 再跟一层
去重: 按**规范化接口地址**比对(canonical) —— 去掉 /at/xml、/from/xxx/、查询串、末尾斜杠,
      否则同一源的 XML 变体会被当成新源重复入库。
输出: 把新发现的追加到 candidates.txt(标记 auto), 每轮上限 MAX_NEW 条。
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CANDIDATES = os.path.join(ROOT, "candidates.txt")
MAX_NEW = 40           # 每轮最多新增多少条(防候选池爆炸)
EXPAND_LIMIT = 10      # 二级展开最多跟几个
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")

# 公开的进料口(失效了不影响运行, 多留几个互补)
FEEDS = [
    ("TVBox-接口合集", "https://raw.githubusercontent.com/saajnn/TVbox-interface-1/main/CATVOD-main.json"),
    ("gao-0821", "https://raw.githubusercontent.com/gaotianliuyun/gao/master/0821.json"),
    ("gao-JSON-仓库", "https://raw.githubusercontent.com/gaotianliuyun/gao/master/JSON.json"),
    ("采集接口分享-博客园", "https://www.cnblogs.com/HGNET/p/16185339.html"),
    ("采集接口分享-CSDN", "https://blog.csdn.net/muzihuaner/article/details/124395958"),
    ("采集接口合集-CSDN2", "https://blog.csdn.net/weixin_43595092/article/details/122877889"),
]

API_RE = re.compile(r'https?://[^\s"\'\\<>,\)\]\}]{0,140}?(?:provide/vod|/inc/api\.php)[^\s"\'\\<>,\)\]\}]{0,80}', re.I)
SUB_RE = re.compile(r'https?://[^\s"\'\\<>,\)\]\}]{{0,160}}?\.(?:json|txt)(?:\?[^\s"\'\\<>,\)\]\}]{{0,40}})?', re.I)


def http(url: str, timeout: int = 25) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(4 * 1024 * 1024).decode("utf-8", "ignore")
    except Exception:
        return 0, ""


def mirrors(url: str) -> list[str]:
    """GitHub raw 的可达性不该成为单点: 给出镜像兜底(本地网络常拦 raw, Actions 原生能取)"""
    m = re.match(r"https?://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)", url)
    if not m:
        return [url]
    owner, repo, branch, path = m.groups()
    return [
        url,
        f"https://cdn.jsdelivr.net/gh/{owner}/{repo}@{branch}/{path}",
        f"https://gh-proxy.com/{url}",
        f"https://ghfast.top/{url}",
    ]


def fetch(url: str, timeout: int = 25) -> tuple[str, str]:
    """带镜像兜底的抓取 → (实际用的地址, 正文)"""
    for u in mirrors(url):
        code, body = http(u, timeout)
        if code == 200 and body:
            return u, body
    return url, ""


def canonical(api: str) -> str:
    """规范化: 同一源的不同转码形态(/at/xml、/from/xxx/)归一到同一身份"""
    u = api.split("?")[0].strip().rstrip("/")
    u = re.sub(r"/at/(xml|json)$", "", u, flags=re.I)
    u = re.sub(r"/from/[a-z0-9]+/?$", "", u, flags=re.I)
    p = urllib.parse.urlparse(u)
    host = (p.hostname or "").lower().removeprefix("www.")
    port = f":{p.port}" if p.port and p.port not in (80, 443) else ""
    return f"{host}{port}{p.path}".rstrip("/")


def clean(api: str) -> str:
    """保留可用的采集接口形态: 统一收敛到 provide/vod 基址(丢掉 /at/xml 这类转码后缀)"""
    u = api.split("?")[0].strip().rstrip("/")
    u = re.sub(r"/at/(xml|json)$", "", u, flags=re.I)
    u = re.sub(r"/from/[a-z0-9]+/?$", "", u, flags=re.I)
    return u


def load_known() -> tuple[set[str], list[str], int]:
    known, lines, auto = set(), [], 0
    if not os.path.exists(CANDIDATES):
        return known, lines, auto
    for line in open(CANDIDATES, encoding="utf-8"):
        raw = line.rstrip("\n")
        lines.append(raw)
        body = raw.split("#", 1)[0].strip()
        if not body:
            continue
        api = body.split("|", 1)[1].strip() if "|" in body else body.strip()
        if api.startswith("http"):
            known.add(canonical(api))
            if body.startswith("自动发现") or body.startswith("auto"):
                auto += 1
    return known, lines, auto


# ---------- 进料口 4: GitHub code search ----------
# 为什么需要: 另外 3 个静态 feed 实测只能反复吐出同一批 17 个老接口(新增 0/轮),
# 而 GitHub 上有 2 万+ 公开文件包含 `provide/vod`(各类 TVBox/影视仓接口合集), 是当前唯一还在长的进料口。
# 轮转: 每次只读一页(100 个文件), 按 cursor 轮换页码与查询词 —— 否则每轮都看同样的 top100, 永远只挖到同样几条。
GH_QUERIES = ["provide/vod in:file", '"api.php/provide/vod" in:file', "provide/vod/from in:file"]
GH_FILES_PER_ROUND = 30
ROUND_BUDGET_S = 240          # 一轮发现的总时间预算(超出就收工, 别把 CI 拖到超时)
CURSOR = os.path.join(ROOT, "discover_cursor.txt")


def _deadline() -> float:
    return __import__("time").time() + ROUND_BUDGET_S



def _gh_token() -> str:
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""


def _gh_json(path: str, timeout: int = 25):
    token = _gh_token()
    if not token:
        return None
    req = urllib.request.Request(
        "https://api.github.com" + path,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "User-Agent": UA, "X-GitHub-Api-Version": "2022-11-28"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read(4 * 1024 * 1024).decode("utf-8", "ignore"))
    except Exception:
        return None


def _cursor() -> int:
    try:
        return int(open(CURSOR).read().strip() or "0")
    except Exception:
        return 0


def _cursor_write(v: int) -> None:
    try:
        with open(CURSOR, "w") as f:
            f.write(str(v))
    except OSError:
        pass


def github_harvest() -> dict[str, str]:
    """从 GitHub code search 挖采集接口文件里的接口地址(近半年有更新的仓库优先)"""
    if not _gh_token():
        print("  · GitHub搜索            跳过(无 token)")
        return {}
    rot = _cursor()
    q = GH_QUERIES[rot % len(GH_QUERIES)]
    page = 1 + (rot // len(GH_QUERIES)) % 5
    d = _gh_json(f"/search/code?q={urllib.parse.quote(q)}&per_page=100&page={page}")
    if not d or not d.get("items"):
        print(f"  · GitHub搜索            取不到(q={q[:24]} p={page})")
        return {}
    files, seen_repo_path = [], set()
    for it in d["items"]:
        repo = it["repository"]["full_name"]
        path = it["path"]
        if not path.lower().endswith((".json", ".txt", ".md", ".py", ".ts", ".js", ".conf", ".yaml", ".yml")):
            continue
        if (repo, path) in seen_repo_path:
            continue
        seen_repo_path.add((repo, path))
        files.append((repo, path, it["repository"].get("pushed_at") or ""))
    files.sort(key=lambda x: x[2], reverse=True)
    found: dict[str, str] = {}
    t_end = _deadline()
    scanned = 0
    for repo, path, _ in files[:GH_FILES_PER_ROUND]:
        if __import__("time").time() > t_end:
            print("  · GitHub搜索            时间预算用尽, 本轮提前收工")
            break
        scanned += 1
        _, body = fetch(f"https://raw.githubusercontent.com/{repo}/HEAD/{path}", timeout=8)
        if not body:
            # raw 被墙/取不到时用 contents API 兜底(base64)
            d2 = _gh_json(f"/repos/{repo}/contents/{urllib.parse.quote(path)}")
            try:
                body = base64.b64decode(d2["content"]).decode("utf-8", "ignore") if isinstance(d2, dict) and d2.get("content") else ""
            except Exception:
                body = ""
        if not body:
            continue
        for m in API_RE.findall(body):
            api = clean(m)
            if len(api) > 8 and canonical(api) not in found:
                found[canonical(api)] = api
    print(f"  · GitHub搜索            扫 {scanned} 文件 → 提取 {len(found)} 个(q={q[:20]} p={page})")
    _cursor_write(rot + 1)
    return found


def api_reachable(api: str) -> bool:
    """便宜的广度筛: 接口能返回 JSON 且 list 非空(不等于能播 —— 可播率由使用方本地终审)。
    只用来挡掉整段死域名, 不当作剔除依据。"""
    sep = "&" if "?" in api else "?"
    for ac in ("ac=detail&pg=1", "ac=videolist&pg=1"):
        code, body = http(f"{api}{sep}{ac}", timeout=10)
        if code == 200 and body.lstrip().startswith("{"):
            try:
                if json.loads(body).get("list"):
                    return True
            except Exception:
                continue
    return False



def harvest(feeds: list[tuple[str, str]]) -> dict[str, str]:
    """抓取 + 提取; 并对"多仓"(内容是地址列表的)做一级展开"""
    found: dict[str, str] = {}          # canonical → 原始地址
    subs: list[str] = []

    def grab(item: tuple[str, str]) -> tuple[str, str]:
        return item[0], fetch(item[1])[1]

    with ThreadPoolExecutor(max_workers=5) as ex:
        bodies = list(ex.map(grab, feeds))
    for name, body in bodies:
        if not body:
            print(f"  · {name:20s} 取不到(跳过)")
            continue
        hits = 0
        for m in API_RE.findall(body):
            api = clean(m)
            if len(api) > 8 and canonical(api) not in found:
                found[canonical(api)] = api
                hits += 1
        for m in SUB_RE.findall(body):
            if len(subs) < EXPAND_LIMIT * 3 and m not in subs and "githubusercontent" not in m:
                subs.append(m)
        print(f"  · {name:20s} 新提取 {hits} 个")

    # 二级展开(多仓)
    if subs:
        print(f"  二级展开 {min(len(subs), EXPAND_LIMIT)} 个地址…")
        with ThreadPoolExecutor(max_workers=5) as ex:
            for name, body in ex.map(grab, [(f"sub{i}", u) for i, u in enumerate(subs[:EXPAND_LIMIT])]):
                if not body:
                    continue
                for m in API_RE.findall(body):
                    api = clean(m)
                    if len(api) > 8 and canonical(api) not in found:
                        found[canonical(api)] = api
    return found


def main() -> int:
    print("候选源自动发现:")
    known, lines, auto = load_known()
    print(f"  现有候选 {len(known)} 个(其中自动发现 {auto} 个)")
    found = harvest(FEEDS)
    found.update(github_harvest())
    fresh = {k: v for k, v in found.items() if k not in known}
    print(f"  发现接口 {len(found)} 个, 其中新增 {len(fresh)} 个")
    if not fresh:
        print("  无需追加")
        return 0
    # 广度筛: 只保留「接口本身能返回数据」的候选(挡掉整段死域名), 可播率交给使用方本地终审。
    # ⚠️ 这一步只证明 API 通, **不证明能播** —— 海外出口拉流特性差异很大, 不能据此弃源。
    cands = list(fresh.values())
    t_end = _deadline()
    with ThreadPoolExecutor(max_workers=12) as ex:
        alive = []
        for api, ok in zip(cands, ex.map(api_reachable, cands)):
            if ok:
                alive.append(api)
            if __import__("time").time() > t_end:
                print("  (广度筛时间预算用尽, 剩余候选下轮再筛)")
                break
    print(f"  接口可达 {len(alive)} 个")
    if not alive:
        print("  无可追加(全部接口不可达)")
        return 0
    add = alive[:MAX_NEW]
    if not lines or lines[-1].strip():
        lines.append("")
    lines.append(f"# ---- 自动发现 {__import__('time').strftime('%Y-%m-%d')} (共 {len(fresh)} 个新接口, 可达 {len(alive)}, 本轮写入 {len(add)} 个) ----")
    for api in add:
        host = urllib.parse.urlparse(api).hostname or "src"
        lines.append(f"自动发现·{host}|{api}")
    with open(CANDIDATES, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"  已追加 {len(add)} 条到 candidates.txt:")
    for api in add[:12]:
        print("    +", api)
    if len(add) > 12:
        print(f"    … 另有 {len(add) - 12} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
