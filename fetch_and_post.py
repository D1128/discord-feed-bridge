# fetch_and_post.py（UA + RSSHubミラー優先 + フィード中身チェック + ログ強化）
import os, json, time, hashlib, sys
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

# ← moeyy.cn を最優先に（重要）
RSSHUB_MIRRORS = [
    "https://rsshub.moeyy.cn",
    "https://rsshub.rssforever.com",
    "https://rsshub.app",
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
    """レスポンスがRSS/Atomっぽいかの簡易判定（Cloudflare等のHTMLを弾く）"""
    sniff = content[:4096].lower()
    return (
        b"<rss" in sniff or
        b"<feed" in sniff or
        b"<entry" in sniff or
        b"<item" in sniff
    )

def _iter_rsshub_candidates(url: str):
    if "rsshub." not in url:
        yield url
        return
    # どのベースが来ても優先順に置換
    for base in RSSHUB_MIRRORS:
        u = url
        for any_base in RSSHUB_MIRRORS:
            u = u.replace(any_base, base)
        yield u

def _headers_for(url: str):
    h = dict(BASE_HEADERS)
    if url.startswith("https://androplus.org/"):
        h["Referer"] = "https://androplus.org/"
    return h

def fetch_and_parse(url: str, timeout: int = 20):
    """UA付きで取得→中身確認→feedparser、rsshubはミラーを順に試す"""
    last_err = None
    for candidate in _iter_rsshub_candidates(url):
        headers = _headers_for(candidate)
        for attempt in range(3):
            try:
                r = requests.get(candidate, headers=headers, timeout=timeout)
                status = r.status_code
                if status in (403, 429, 502, 503, 504) and attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                r.raise_for_status()
                # 中身がフィードっぽくなければ次を試す
                if not _looks_like_feed(r.content):
                    last_err = f"non-feed body (status={status})"
                    break  # 次のミラーへ
                feed = feedparser.parse(r.content)
                if getattr(feed, "entries", None):
                    print(f"[INFO] rsshub_ok: {candidate} entries={len(feed.entries)}")
                    return feed
                else:
                    print(f"[INFO] rsshub_empty: {candidate} entries=0")
                    break  # 次のミラーへ
            except Exception as ex:
                last_err = str(ex)
                # 次の試行 or 次のミラーへ
        # 次のミラーへ
    # すべてダメ→最後に素のURLでフォールバック
    try:
        headers = _headers_for(url)
        feed = feedparser.parse(url, request_headers=headers)
        return feed
    except Exception as ex:
        raise RuntimeError(last_err or str(ex))

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

    # シャーディング対応（並列時の重複防止）
    targets = [s for i, s in enumerate(sources) if i % shard_total == shard_idx]

    for s in targets:
        url = s["url"]
        name = s.get("name", url)

        # webhook解決（ENV最優先 → feeds.yamlのwebhooksセクション）
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
        for e in entries[:10]:  # 直近10件だけ判定
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
