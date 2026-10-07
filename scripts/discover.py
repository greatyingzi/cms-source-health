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
    fresh = {k: v for k, v in found.items() if k not in known}
    print(f"  发现接口 {len(found)} 个, 其中新增 {len(fresh)} 个")
    if not fresh:
        print("  无需追加")
        return 0
    add = list(fresh.values())[:MAX_NEW]
    if not lines or lines[-1].strip():
        lines.append("")
    lines.append(f"# ---- 自动发现 {__import__('time').strftime('%Y-%m-%d')} (共 {len(fresh)} 个, 本轮写入 {len(add)} 个) ----")
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
