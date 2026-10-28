#!/usr/bin/env python3
"""候选源筛选(本地出口): API 可达 + 真能播(任一条线路的首集 m3u8 = 200 且 #EXTM3U)。

用法: python3 scripts/screen_local.py candidates.txt   # 产出 /tmp/screen_good.txt(rate>=0.5)
用途: 自动发现只做广度(接口能返回 list), 到底能不能播必须由**自己网络的出口**判定;
      海外/机房出口与家宽差异极大(实测 rstu6/zuidazym3u8 机房 403、家宽 200)。
拿到 good 列表后再按需写回 candidates.txt 并跑 check_sources.py 生成 sources.json。

判据比后端 localRate 宽一档：后端只探 cand[0]（第一条线路第一个地址），
实测 ffzy 家族第一条线路是 /share/ HTML 分享页 → 后端直接判 0。
这里逐线路探，任一条通就算这条片子可播（真实用户也会换线路）。
"""
import json, re, sys, urllib.parse, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

UA_API = "okhttp/4.12.0"
UA_PLAY = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def get(url, ua, timeout=10, maxbytes=200000, referer=None):
    h = {"User-Agent": ua, "Accept": "*/*"}
    if referer:
        h["Referer"] = referer
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
            return r.status, r.read(maxbytes)
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception:
        return 0, b""


def api_list(api):
    sep = "&" if "?" in api else "?"
    for ac in ("ac=detail&pg=1", "ac=videolist&pg=1", ""):
        q = f"{api}{sep}{ac}" if ac else api
        st, body = get(q, UA_API, timeout=15, maxbytes=3000000)
        if st == 200 and body.strip().startswith(b"{"):
            try:
                d = json.loads(body.decode("utf-8", "ignore"))
            except Exception:
                continue
            lst = d.get("list") or []
            if lst:
                return lst
    return []


def ep_urls(vod_play_url, per_line=2, max_lines=4):
    out = []
    for seg in str(vod_play_url).split("$$$")[:max_lines]:
        n = 0
        for ep in seg.split("#"):
            if "$" in ep:
                u = ep.split("$", 1)[1].strip()
                if u:
                    out.append(u)
                    n += 1
            if n >= per_line:
                break
    return out


def playable(url):
    host = urllib.parse.urlsplit(url)
    ref = f"{host.scheme}://{host.netloc}/"
    for ua in (UA_PLAY, UA_API):
        for r in (None, ref):
            st, b = get(url, ua, timeout=8, maxbytes=1000, referer=r)
            if st == 200 and b.lstrip().startswith(b"#EXTM3U"):
                return True
    return False


def screen(name, api, samples=3):
    rec = {"name": name, "api": api, "items": 0, "sampled": 0, "playable": 0, "rate": 0.0, "err": ""}
    try:
        lst = api_list(api)
    except Exception as e:
        rec["err"] = f"api:{e}"[:40]
        return rec
    rec["items"] = len(lst)
    if not lst:
        rec["err"] = "api_no_data"
        return rec
    for v in lst[:samples]:
        urls = ep_urls(v.get("vod_play_url", ""))
        if not urls:
            continue
        rec["sampled"] += 1
        if any(playable(u) for u in urls[:6]):
            rec["playable"] += 1
    rec["rate"] = round(rec["playable"] / rec["sampled"], 2) if rec["sampled"] else 0.0
    return rec


def main(path):
    rows = []
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "|" not in line:
            continue
        name, api = line.split("|", 1)
        rows.append((name.strip(), api.strip()))
    print(f"待筛 {len(rows)} 条", flush=True)
    out = []
    with ThreadPoolExecutor(max_workers=12) as ex:
        futs = {ex.submit(screen, n, a): (n, a) for n, a in rows}
        for i, f in enumerate(as_completed(futs), 1):
            r = f.result()
            out.append(r)
            if r["rate"] > 0:
                print(f"  ✅ {r['name'][:18]:20s} rate={r['rate']} items={r['items']} {r['api'][:60]}", flush=True)
            if i % 25 == 0:
                print(f"  …{i}/{len(rows)}", flush=True)
    out.sort(key=lambda x: -x["rate"])
    json.dump(out, open("/tmp/screen_result.json", "w"), ensure_ascii=False, indent=1)
    good = [r for r in out if r["rate"] >= 0.5]
    print(f"\n=== 可播率>=0.5 的源: {len(good)} 个 ===")
    for r in good:
        print(f"{r['name'][:20]:22s} rate={r['rate']} items={r['items']} {r['api']}")
    with open("/tmp/screen_good.txt", "w", encoding="utf-8") as f:
        for r in good:
            f.write(f"{r['name']}|{r['api']}\n")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/harvest_candidates.txt")
