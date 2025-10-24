# fetch_and_post.py（UA + RSSHub→Nitterフォールバック + 中身判定 + RT除外）
import os, json, time, hashlib, sys, re, urllib.parse
import requests, feedparser, yaml

STATE_FILE = "state.json"

BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36 (GitHubActions FeedBridge)"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.7,en;q=0.3",
}

# ← moeyy.cn を先頭に（優先）
RSSHUB_MIRRORS = [
    "https://rsshub.moeyy.cn",
    "https://rsshub.rssforever.com",
    "https://rsshub.app",
]

# Nitter の候補（動かない物もあるので複数用意）
NITTER_MIRRORS = [
    "https://nitter.net",
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
    "https://nitter.fdn.fr",
]

def load_yaml(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def _looks_like_feed(content: bytes) -> bool:
    sniff = content[:4096].lower()
    return b"<rss" in sniff or b"<feed" in sniff or b"<entry" in sniff or b"<item" in sniff

def _headers_for(url: str):
    h = dict(BASE_HEADERS)
    if url.startswith("https://androplus.org/"):
        h["Referer"] = "https://androplus.org/"
    return h

def _http_get(url: str, timeout=20, retries=3):
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, headers=_headers_for(url), timeout=timeout)
            if r.status_code in (403, 429, 502, 503, 504) and i < retries - 1:
                time.sleep(2 * (i + 1))
                continue
            r.raise_for_status()
            return r
        except Exception as e:
            last = e
    raise last or RuntimeError("request failed")

# --------- X (Twitter) URL の判定 & Nitter URL 生成 ---------
def _parse_x_user(url: str):
    m = re.search(r"/twitter/user/([^/?]+)", url)
    if not m: 
        return None
    user = m.group(1)
    qs = urllib.parse.urlparse(url).query
    params = urllib.parse.parse_qs(qs)
    include_rts = params.get("includeRts", ["0"])[0] != "0"
    return {"user": user, "include_rts": include_rts}

def _parse_x_keyword(url: str):
    m = re.search(r"/twitter/keyword/([^/?]+)", url)
    if not m: 
        return None
    kw_decoded = urllib.parse.unquote(m.group(1))
    qs = urllib.parse.urlparse(url).query
    params = urllib.parse.parse_qs(qs)
    include_rts = params.get("includeRts", ["0"])[0] != "0"
    limit = int(params.get("limit", ["5"])[0])
    return {"q": kw_decoded, "include_rts": include_rts, "limit": limit}

def _nitter_user_feeds(user: str):
    for base in NITTER_MIRRORS:
        yield f"{base}/{user}/rss"

def _nitter_search_feeds(q: str):
    q_param = urllib.parse.quote(q, safe="")
    for base in NITTER_MIRRORS:
        yield f"{base}/search/rss?f=tweets&q={q_param}"

def _strip_retweets(entries, include_rts: bool):
    if include_rts:
        return entries
    filtered = []
    for e in entries:
        title = (e.get("title") or "").strip()
        if title.startswith("RT ") or title.startswith("RT@") or title.startswith("RT @"):
            continue
        filtered.append(e)
    return filtered
# ------------------------------------------------------------

def fetch_via_rsshub(url: str):
    # rsshub を優先順に試す（中身がフィードで entries>0 なら採用）
    for base in RSSHUB_MIRRORS:
        cand = url
        for any_base in RSSHUB_MIRRORS:
            cand = cand.replace(any_base, base)
        try:
            r = _http_get(cand)
            if not _looks_like_feed(r.content):
                print(f"[INFO] rsshub_nonfeed: {cand}")
                continue
            feed = feedparser.parse(r.content)
            if getattr(feed, "entries", None):
                print(f"[INFO] rsshub_ok: {cand} entries={len(feed.entries)}")
                return feed
            print(f"[INFO] rsshub_empty: {cand}")
        except Exception as e:
            print(f"[INFO] rsshub_err: {cand} -> {e}")
    return None

def fetch_via_nitter_for_x(url: str):
    # ユーザー or キーワードに応じて Nitter を試す
    info_user = _parse_x_user(url)
    info_kw = _parse_x_keyword(url)
    if info_user:
        for cand in _nitter_user_feeds(info_user["user"]):
            try:
                r = _http_get(cand)
                if not _looks_like_feed(r.content):
                    continue
                feed = feedparser.parse(r.content)
                entries = _strip_retweets(feed.entries or [], include_rts=info_user["include_rts"])
                if entries:
                    feed.entries = entries
                    print(f"[INFO] nitter_ok(user): {cand} entries={len(entries)}")
                    return feed
                print(f"[INFO] nitter_empty(user): {cand}")
            except Exception as e:
                print(f"[INFO] nitter_err(user): {cand} -> {e}")
    elif info_kw:
        for cand in _nitter_search_feeds(info_kw["q"]):
            try:
                r = _http_get(cand)
                if not _looks_like_feed(r.content):
                    continue
                feed = feedparser.parse(r.content)
                entries = _strip_retweets(feed.entries or [], include_rts=info_kw["include_rts"])
                if entries:
                    # limit を軽く尊重（Nitterは件数絞りが無いので上から切る）
                    feed.entries = entries[: max(1, info_kw["limit"])]
                    print(f"[INFO] nitter_ok(search): {cand} entries={len(feed.entries)}")
                    return feed
                print(f"[INFO] nitter_empty(search): {cand}")
            except Exception as e:
                print(f"[INFO] nitter_err(search): {cand} -> {e}")
    return None

def fetch_and_parse(url: str):
    # まず rsshub を試す
    feed = fetch_via_rsshub(url) if "rsshub." in url else None
    if feed is not None:
        return feed
    # rsshub でダメ/空 → X系なら Nitter を試す
    if "twitter/user/" in url or "twitter/keyword/" in url:
        feed = fetch_via_nitter_for_x(url)
        if feed is not None:
            return feed
    # 最後の保険：そのまま feedparser に渡す
    try:
        return feedparser.parse(url, request_headers=_headers_for(url))
    except Exception as e:
        raise RuntimeError(str(e))

def post_discord(webhook, content):
    data = {"content": content}
    while True:
        resp = requests.post(webhook, json=data, timeout=30)
        if resp.status_code == 429:
            try:
                retry = resp.json().get("retry_after", 1.5)
            except Exception:
                retry = 1.5
            time.sleep(float(retry))
            continue
        resp.raise_for_status()
        break

def entry_uid(entry):
    return entry.get("id") or entry.get("link") or hashlib.md5(str(entry).encode()).hexdigest()

def format_message(src_name, e):
    title = e.get("title", "(no title)")
    link = e.get("link", "")
    return f"**{src_name}**\n{title}\n{link}"

def main(shard_idx=0, shard_total=1):
    cfg = load_yaml("feeds.yaml")
    state = load_state()
    sources = cfg["sources"]
    posted = 0

    targets = [s for i, s in enumerate(sources) if i % shard_total == shard_idx]

    for s in targets:
        url = s["url"]
        name = s.get("name", url)

        # webhook解決（ENV最優先 → feeds.yaml）
        name_key = s.get("webhook", "news").upper()
        webhook = (
            os.environ.get(f"DISCORD_WEBHOOK_{name_key}")
            or os.environ.get("DISCORD_WEBHOOK")
            or (cfg.get("webhooks") or {}).get(s.get("webhook", "news"))
        )
        if not webhook:
            print(f"[WARN] no webhook for {name} ({url})", file=sys.stderr)
            continue

        try:
            feed = fetch_and_parse(url)
        except Exception as ex:
            print(f"[WARN] fetch failed: {url} -> {ex}", file=sys.stderr)
            continue

        entries = getattr(feed, "entries", []) or []
        print(f"[INFO] fetched: {name} ({url}) entries={len(entries)}")

        last_ids = state.get(url, [])
        new_items = []
        for e in entries[:10]:
            uid = entry_uid(e)
            if uid not in last_ids:
                new_items.append((uid, e))
        new_items.reverse()
        print(f"[INFO] new_items={len(new_items)} -> post to {s.get('webhook','news')}")

        for uid, e in new_items:
            msg = format_message(name, e)
            try:
                post_discord(webhook, msg)
                posted += 1
                time.sleep(0.3)
            except Exception as ex:
                print(f"[WARN] post failed: {url} -> {ex}", file=sys.stderr)

        state[url] = ([u for u, _ in new_items] + last_ids)[:50] if new_items else last_ids[:50]

    save_state(state)
    print(f"done: posted={posted}")

if __name__ == "__main__":
    shard_idx = int(os.environ.get("SHARD_INDEX", "0"))
    shard_total = int(os.environ.get("SHARD_TOTAL", "1"))
    main(shard_idx, shard_total)
