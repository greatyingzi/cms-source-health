#!/usr/bin/env python3
"""macCMS 采集源健康检查器 —— 海外视角(必要不充分)筛

用途: 定时(见 .github/workflows/check.yml)检查一批苹果CMS(macCMS)采集站接口是否还能用,
      产出机器可读的 sources.json + dead.txt, 供下游(播放器/聚合站)取用。

检查三件事(递进):
  1) API 可达   : GET {api}?ac=detail&pg=1 → HTTP 200 + 可 JSON 解析 + list 非空
  2) 播放列表可读: 抽 N 条, 解析 vod_play_url(集名$地址#…), 取首条地址 → 必须拿到 #EXTM3U
  3) 分片可取   : media 列表里抽 1 个分片 → HTTP 200 且体积 > 10KB

⚠️ 重要: 本检查运行在 GitHub(Actions) 的**海外**出口 ——
   "海外不通" 基本可判定该源不可用(必要条件下不满足);
   但 "海外通" **不等于** 国内可用(不少国内 CDN 会拦海外 IP, 也有反过来对国内某运营商差的)。
   所以本仓库结论只做**筛选**; 最终可用性必须由使用方在自己的网络里复测后再定。

依赖: 仅 Python 标准库(urllib), 无第三方包。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CANDIDATES = os.path.join(ROOT, "candidates.txt")
OUT = os.path.join(ROOT, "sources.json")
DEAD = os.path.join(ROOT, "dead.txt")
BLOCKED = os.path.join(ROOT, "streams-blocked.txt")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")
TIMEOUT = 20
SAMPLES_DEFAULT = 3        # 快速筛每源抽几条
SAMPLES_DEEP = 8           # 深度筛每源抽几条
HISTORY_MAX = 40           # 每源保留多少条历史(控文件体积)


def http(url: str, timeout: int = TIMEOUT) -> tuple[int, bytes, dict]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(3 * 1024 * 1024), dict(r.headers)
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read(64 * 1024), dict(e.headers)
        except Exception:
            return e.code, b"", {}
    except Exception:
        return 0, b"", {}


def parse_candidates() -> list[dict]:
    """candidates.txt 格式: 每行  name|api   (name 可空; # 开头为注释)"""
    out, seen = [], set()
    if not os.path.exists(CANDIDATES):
        return out
    for line in open(CANDIDATES, encoding="utf-8"):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if "|" in line:
            name, api = line.split("|", 1)
            name, api = name.strip(), api.strip()
        else:
            api, name = line, ""
        if not re.match(r"^https?://", api) or api in seen:
            continue
        seen.add(api)
        key = re.sub(r"[^a-z0-9]+", "-", (urllib.parse.urlparse(api).hostname or "src").lower()).strip("-")
        out.append({"key": key, "name": name or key, "api": api})
    return out


def play_urls(item: dict) -> list[str]:
    """从 vod_play_url 里取出前几条真实地址(格式: 集名$地址#集名$地址)"""
    urls: list[str] = []
    for group in str(item.get("vod_play_url") or "").split("$$$"):
        for seg in group.split("#"):
            parts = seg.split("$")
            if len(parts) >= 2 and re.match(r"^https?://", parts[1]):
                urls.append(parts[1])
    return urls


def playlist_ok(url: str, depth: int = 0) -> bool:
    """拉播放列表: 必须是 m3u8; 是 master 就再跟一层变体"""
    code, body, _ = http(url)
    if code != 200:
        return False
    text = body.decode("utf-8", "ignore")
    if not text.lstrip().startswith("#EXTM3U"):
        return False
    if "#EXT-X-STREAM-INF" in text and depth < 1:
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                variant = urllib.parse.urljoin(url, line)
                return playlist_ok(variant, depth + 1)
    return "#EXTINF" in text or "#EXT-X-STREAM-INF" in text or len(text) > 40


def segment_ok(playlist_url: str) -> bool:
    """media 列表里抽第一个分片, 要求 200 且体积够(排除 404 页/空文件)"""
    code, body, _ = http(playlist_url)
    if code != 200:
        return False
    text = body.decode("utf-8", "ignore")
    first = next((l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")), "")
    if not first:
        return True                      # 没有分片行(极短列表), 不因此判死
    seg = urllib.parse.urljoin(playlist_url, first)
    code, body, _ = http(seg)
    return code == 200 and len(body) > 10 * 1024


def check_one(src: dict, samples: int) -> dict:
    api = src["api"]
    sep = "&" if "?" in api else "?"
    started = time.time()
    code, body, _ = http(f"{api}{sep}ac=detail&pg=1")
    res = {"api_ok": False, "sampled": 0, "playable": 0, "rate": 0.0, "reason": "", "ms": 0}
    if code != 200:
        res["reason"] = f"api http {code}"
        res["ms"] = int((time.time() - started) * 1000)
        return res
    try:
        data = json.loads(body.decode("utf-8", "ignore"))
    except Exception:
        res["reason"] = "api not json"
        res["ms"] = int((time.time() - started) * 1000)
        return res
    items = (data.get("list") or [])[:samples]
    if not items:
        res["reason"] = "api empty list"
        res["ms"] = int((time.time() - started) * 1000)
        return res
    res["api_ok"] = True

    playable = 0
    sampled = 0
    for it in items:
        urls = play_urls(it)
        if not urls:
            continue
        sampled += 1
        try:
            ok = playlist_ok(urls[0]) and segment_ok(urls[0])
        except Exception:
            ok = False
        if ok:
            playable += 1
    res.update({"sampled": sampled, "playable": playable,
                "rate": round(playable / sampled, 3) if sampled else 0.0})
    if sampled == 0:
        res["reason"] = "no play url in feeds"
    elif playable == 0:
        res["reason"] = "all sampled streams dead"
    res["ms"] = int((time.time() - started) * 1000)
    return res


def load_prev() -> dict:
    try:
        return json.load(open(OUT, encoding="utf-8"))
    except Exception:
        return {}


def main() -> int:
    deep = "--deep" in sys.argv or os.environ.get("DEEP") == "1"
    samples = SAMPLES_DEEP if deep else SAMPLES_DEFAULT
    cands = parse_candidates()
    prev = load_prev()
    prev_map = {s["api"]: s for s in prev.get("sources", [])}
    print(f"candidates: {len(cands)}  samples/source: {samples}  mode: {'deep' if deep else 'quick'}")

    results = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        for src, r in zip(cands, ex.map(lambda s: check_one(s, samples), cands)):
            now = int(time.time())
            old = prev_map.get(src["api"], {})
            history = (old.get("history") or [])[-HISTORY_MAX:]
            history.append({"at": now, "rate": r["rate"], "api_ok": r["api_ok"]})
            results.append({
                **src,
                "first_seen": old.get("first_seen", now),
                "last_ok": now if (r["api_ok"] and r["rate"] > 0) else old.get("last_ok"),
                "last_fail": None if (r["api_ok"] and r["rate"] > 0) else now,
                "overseas": {**r, "checked_at": now},
                "history": history[-HISTORY_MAX:],
            })
            mark = "OK " if r["rate"] > 0 else "BAD"
            print(f"  {mark} {src['name'][:14]:16s} api={'Y' if r['api_ok'] else 'N'} "
                  f"{r['playable']}/{r['sampled']} rate={r['rate']} {r['reason']}")

    out = {
        "version": 1,
        "generated_at": int(time.time()),
        "checker": "github-actions/overseas",
        "note": "overseas 视角: 用于筛选; 最终可用性请在使用方网络内复测",
        "sources": results,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
        f.write("\n")

    # ★ 两级信号(实测教训: 索尼/最大 的 CDN 只拦机房 IP, 海外云出口拉流全 404, 但国内/普通出口完全可用):
    #   dead.txt            = 接口本身不可用(api_ok=false)         → 强信号, 可直接剔除
    #   streams-blocked.txt = 接口通但流在海外云出口全失败          → 弱信号, 只降权/待复测, 别据此判死
    api_dead = [s["api"] for s in results if not s["overseas"]["api_ok"]]
    stream_blocked = [s["api"] for s in results
                      if s["overseas"]["api_ok"] and s["overseas"]["sampled"] > 0 and s["overseas"]["playable"] == 0]
    with open(DEAD, "w", encoding="utf-8") as f:
        f.write("\n".join(api_dead) + ("\n" if api_dead else ""))
    with open(BLOCKED, "w", encoding="utf-8") as f:
        f.write("\n".join(stream_blocked) + ("\n" if stream_blocked else ""))

    alive = len(results) - len(api_dead)
    print(f"\nsources={len(results)} 接口可用={alive} 接口失效={len(api_dead)} "
          f"流被海外云拦={len(stream_blocked)} → {os.path.relpath(OUT, ROOT)}")
    # 接口可用数为 0 才视为"全灭"(流被拦不算)
    return 0 if alive else 1


if __name__ == "__main__":
    sys.exit(main())
