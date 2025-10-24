# fetch_and_post.py（チャンネル別 Webhook 版）
import os, json, time, hashlib, sys
import requests, feedparser, yaml

STATE_FILE = "state.json"
HEADERS = {"User-Agent": "GH-Actions-FeedBridge/1.0"}

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

def fetch(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text

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
        # webhook名（news/yt/x）→ 環境変数 DISCORD_WEBHOOK_NEWS / _YT / _X を探す
        name_key = s.get("webhook", "news").upper()
        webhook = (
            os.environ.get(f"DISCORD_WEBHOOK_{name_key}")
            or os.environ.get("DISCORD_WEBHOOK")               # 予備
            or cfg["webhooks"].get(s.get("webhook", "news"))   # feeds.yaml の値
        )
        if not webhook:
            print(f"[WARN] no webhook for {name} ({url})", file=sys.stderr)
            continue

        try:
            feed = feedparser.parse(fetch(url))
        except Exception as ex:
            print(f"[WARN] fetch failed: {url} -> {ex}", file=sys.stderr)
            continue

        last_ids = state.get(url, [])
        new_items = []
        for e in feed.entries[:10]:
            uid = entry_uid(e)
            if uid not in last_ids:
                new_items.append((uid, e))
        new_items.reverse()

        for uid, e in new_items:
            msg = format_message(name, e)
            try:
                post_discord(webhook, msg)
                posted += 1
                time.sleep(0.3)
            except Exception as ex:
                print(f"[WARN] post failed: {url} -> {ex}", file=sys.stderr)

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
