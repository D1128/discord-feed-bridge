# fetch_and_post.py（チャンネル別 Webhook 版・UA/ミラーフォールバック付き）
import os, json, time, hashlib, sys
import requests, feedparser, yaml

STATE_FILE = "state.json"

# 共通ヘッダ（403 回避に有効）
BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36 "
        "(GitHubActions FeedBridge)"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.7,en;q=0.3",
}

# RSSHub が落ち/ブロックされることがあるので、順に試す
RSSHUB_MIRRORS = [
    "https://rsshub.app",             # 元
    "https://rsshub.moeyy.cn",        # ミラー1
    "https://rsshub.rssforever.com",  # ミラー2
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

def iter_with_rsshub_mirrors(url: str):
    """url が rsshub のとき、ミラーに差し替えた候補を順に返す。"""
    if "rsshub." not in url:
        yield url
        return
    for base in RSSHUB_MIRRORS:
        # どのベースでも置換できるように、すべての候補を試す
        u = url
        for b in RSSHUB_MIRRORS:
            u = u.replace(b, base)
        yield u

def parse_feed_with_ua(url: str, timeout: int = 20):
    """
    User-Agent 付きで取得 → feedparser で解析。
    - 403/429 は短いバックオフ付きで数回リトライ
    - rsshub はミラーを順に試す
    - AndroPlus など一部サイトは Referer を付与
    """
    # 参照付与などの個別対応
    def make_headers(u: str):
        h = dict(BASE_HEADERS)
        if u.startswith("https://androplus.org/"):
            h["Referer"] = "https://androplus.org/"
        return h

    # rsshub ミラーを順に試す
    for candidate in iter_with_rsshub_mirrors(url):
        headers = make_headers(candidate)
        # 軽いリトライ（最大3回）
        for attempt in range(3):
            try:
                r = requests.get(candidate, headers=headers, timeout=timeout)
                # 特定のステータスは待って再試行
                if r.status_code in (403, 429, 502, 503, 504) and attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                r.raise_for_status()
                # bytes をそのまま解析（UA/Referer 判定をすり抜けやすい）
                return feedparser.parse(r.content)
            except Exception as ex:
                # 次の試行へ（最後の試行だったら次のミラーへ）
                if attempt >= 2:
                    break

    # すべて失敗時の最後のフォールバック（feedparser 内蔵フェッチ）
    try:
        return feedparser.parse(url, request_headers=BASE_HEADERS)
    except Exception:
        # ここで完全に失敗したら呼び出し元で例外処理
        raise

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

    # 並列分割（複数ワークフローで分担）
    targets = [s for i, s in enumerate(sources) if i % shard_total == shard_idx]

    for s in targets:
        url = s["url"]
        name = s.get("name", url)
        # webhook名（news/yt/x/xkw）→ 環境変数 DISCORD_WEBHOOK_NEWS / _YT / _X / _XKW を優先
        name_key = s.get("webhook", "news").upper()
        webhook = (
            os.environ.get(f"DISCORD_WEBHOOK_{name_key}")
            or os.environ.get("DISCORD_WEBHOOK")               # 予備
            or (cfg.get("webhooks") or {}).get(s.get("webhook", "news"))   # feeds.yaml の値
        )
        if not webhook:
            print(f"[WARN] no webhook for {name} ({url})", file=sys.stderr)
            continue

        try:
            feed = parse_feed_with_ua(url)
            count = len(getattr(feed, "entries", []))
            print(f"[INFO] fetched: {name} ({url}) entries={count}")
        except Exception as ex:
            print(f"[WARN] fetch failed: {url} -> {ex}", file=sys.stderr)
            continue

        last_ids = state.get(url, [])
        new_items = []
        # 取り過ぎ防止（最新10件のみチェック）
        for e in feed.entries[:10]:
            uid = entry_uid(e)
            if uid not in last_ids:
                new_items.append((uid, e))
        # 古い順に流す
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

        # 既読管理（直近50件）
        if new_items:
            state[url] = ([u for u, _ in new_items] + last_ids)[:50]
        else:
            state[url] = last_ids[:50]

    save_state(state)
    print(f"done: posted={posted}")

if __name__ == "__main__":
    shard_idx = int(os.environ.get("SHARD_INDEX", "0"))
    shard_total = int(os.environ.get("SHARD_TOTAL", "1"))
    main(shard_idx, shard_total)
