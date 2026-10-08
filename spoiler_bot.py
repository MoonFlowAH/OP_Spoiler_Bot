"""One Piece spoiler relay for Discord.

Watches OtakuKart's live "One Piece Chapter N: Spoilers, Summary and Pics"
threads and posts each new spoiler update to a Discord channel through a
webhook, grouped under a header message per chapter.

Usage:
    python spoiler_bot.py              poll forever (interval from config.json)
    python spoiler_bot.py --once       check once and exit (for Task Scheduler)
    python spoiler_bot.py --dry-run    print what would be posted, send nothing

Only the Python standard library is used (works on Python 3.8+).
"""

import argparse
import hashlib
import html
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(HERE, "state.json")

SITE = "https://otakukart.com"
API = SITE + "/wp-json/wp/v2/posts"
SITE_UA = "Mozilla/5.0 (compatible; OnePieceSpoilerRelay/1.0; Discord webhook)"
DISCORD_UA = "DiscordBot (https://otakukart.com, 1.0)"

SLUG_RE = re.compile(r"^one-piece-chapter-(\d+)-spoilers")
ARTICLE_RE = re.compile(
    r'<article\b[^>]*class="lc-update"[^>]*data-update-id="([^"]+)"[^>]*>(.*?)</article>',
    re.S,
)
BODY_RE = re.compile(
    r'class="lc-update__body[^>]*>(.*?)(?:</div>\s*<(?:footer|div|form)\b|</div>\s*$)',
    re.S,
)
HEADING_RE = re.compile(r"<h([23])\b[^>]*>(.*?)</h\1>", re.S)
# Write-ups outside the live updates ("BRIEF SPOILERS", "FULL SUMMARY", ...) are the
# headed sections that follow the hints heading
SECTIONS_START_RE = re.compile(r"\bhints?\b|official preview", re.I)
SECTION_SKIP_RE = re.compile(r"more spoilers soon|check back", re.I)
# Sections that get their own role ping: the brief spoilers and the long/full summary
SECTION_PING_RE = re.compile(
    r"\b(brief|full|long(er)?|detailed|complete)\b|(?<!before )\bsummary\b", re.I
)
IMG_RE = re.compile(r'<img\b[^>]*?\ssrc="([^"]+)"', re.I)

MONTHS = ["january", "february", "march", "april", "may", "june", "july",
          "august", "september", "october", "november", "december"]
# Official chapters go up on MANGA Plus and VIZ on the release date at 15:00 UTC
RELEASE_HOUR_UTC = 15
OFFICIAL_LINKS = (
    "[MANGA Plus](https://mangaplus.shueisha.co.jp/titles/100020) • "
    "[VIZ](https://www.viz.com/shonenjump/chapters/one-piece)"
)

COLORS = {"unconfirmed": 0xE67E22, "hint": 0x3498DB, "confirmed": 0x2ECC71}
DEFAULT_COLOR = 0xD32F2F
MAX_DESC = 4000


def log(msg):
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    # Spoiler titles can hold characters the Windows console can't print
    encoding = sys.stdout.encoding or "utf-8"
    print(line.encode(encoding, "replace").decode(encoding), flush=True)


# ---------------------------------------------------------------- site side

def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": SITE_UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def find_chapter_posts(limit=2):
    """Return the newest `limit` spoiler threads as [(chapter, post_id)], newest first."""
    query = urllib.parse.urlencode({
        "search": "one piece chapter spoilers",
        "per_page": 20,
        "_fields": "id,slug",
    })
    found = {}
    for post in fetch_json(API + "?" + query):
        m = SLUG_RE.match(post.get("slug", ""))
        if m:
            found.setdefault(int(m.group(1)), post["id"])
    return sorted(found.items(), reverse=True)[:limit]


def fetch_post(post_id):
    return fetch_json("%s/%d?_fields=id,link,title,content" % (API, post_id))


def to_text(fragment):
    """Flatten an HTML fragment into Discord-friendly plain text."""
    fragment = re.sub(r"<(script|style|svg|form|button)\b.*?</\1>", "", fragment, flags=re.S | re.I)
    fragment = re.sub(r"<li\b[^>]*>", "\n- ", fragment, flags=re.I)
    fragment = re.sub(r"<br\s*/?>|</(p|div|h[1-6]|li|ul|ol|blockquote|tr)>", "\n", fragment, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", fragment)).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def find_images(fragment):
    images = []
    for src in IMG_RE.findall(fragment):
        src = html.unescape(src)
        if "pbs.twimg.com" in src:
            src = src.replace("name=small", "name=large")
        if src.startswith("http") and src not in images:
            images.append(src)
    return images[:4]


def attr(fragment, pattern):
    m = re.search(pattern, fragment, re.S)
    return html.unescape(m.group(1)).strip() if m else None


def parse_post(post):
    """Split a spoiler thread into postable items, in page order."""
    content = post["content"]["rendered"]
    link = post["link"]
    items = []

    for update_id, inner in ARTICLE_RE.findall(content):
        body = BODY_RE.search(inner)
        body = body.group(1) if body else ""
        items.append({
            "id": update_id,
            "title": attr(inner, r'data-title="([^"]*)"') or "Spoiler update",
            "url": attr(inner, r'data-url="([^"]+)"') or "%s#u-%s" % (link, update_id),
            "timestamp": attr(inner, r'<time\b[^>]*datetime="([^"]+)"'),
            "text": to_text(body),
            "images": find_images(body),
        })

    rest = ARTICLE_RE.sub("", content)
    headings = list(HEADING_RE.finditer(rest))
    titles = [to_text(h.group(2)) for h in headings]
    starts = [i for i, t in enumerate(titles) if SECTIONS_START_RE.search(t)]
    first = starts[-1] + 1 if starts else len(headings)
    for i in range(first, len(headings)):
        title = titles[i]
        if not title or SECTION_SKIP_RE.search(title):
            continue
        end = headings[i + 1].start() if i + 1 < len(headings) else len(rest)
        body = rest[headings[i].end():end]
        text = to_text(body)
        images = find_images(body)
        if not text and not images:
            continue
        items.append({
            "id": "section-" + re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-"),
            "title": title,
            "url": link,
            "timestamp": None,
            "text": text,
            "images": images,
            "ping": bool(SECTION_PING_RE.search(title)),
        })

    # The site sometimes reuses one update id for two different spoilers
    counts = {}
    for item in items:
        counts[item["id"]] = counts.get(item["id"], 0) + 1
        if counts[item["id"]] > 1:
            item["id"] = "%s-%d" % (item["id"], counts[item["id"]])

    for item in items:
        raw = json.dumps([item["title"], item["text"], item["images"]], sort_keys=True)
        item["hash"] = hashlib.sha1(raw.encode("utf-8")).hexdigest()

    release = re.search(r"coming out on ([^.\n]+)", to_text(rest))
    return items, (release.group(1).strip() if release else None)


# ------------------------------------------------------------- discord side

def split_text(text, limit=MAX_DESC):
    """Cut text into chunks of at most `limit` characters, preferring line breaks."""
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    chunks.append(text)
    return chunks


def item_messages(item, chapter):
    """Embeds for each Discord message an item needs; long summaries span several."""
    title = item["title"].lower()
    color = next((c for key, c in COLORS.items() if key in title), DEFAULT_COLOR)
    chunks = split_text(item["text"])
    messages = []
    for n, chunk in enumerate(chunks):
        name = item["title"][:230]
        if len(chunks) > 1:
            name += " (part %d/%d)" % (n + 1, len(chunks))
        embed = {
            "title": name,
            "url": item["url"],
            "description": chunk,
            "color": color,
            "footer": {"text": "One Piece Chapter %d • via OtakuKart" % chapter},
        }
        if item["timestamp"]:
            embed["timestamp"] = item["timestamp"]
        messages.append([embed])
    # Embeds sharing a url are merged by Discord into one embed with an image gallery
    for n, image in enumerate(item["images"]):
        if n == 0:
            messages[0][0]["image"] = {"url": image}
        else:
            messages[0].append({"url": item["url"], "image": {"url": image}})
    return messages


def header_payload(chapter, link, release, role_id):
    desc = "Spoilers will be posted below as they are revealed."
    if release:
        desc += "\n**Official release:** " + release
    payload = {
        "embeds": [{
            "title": "🏴‍☠️ One Piece Chapter %d — Spoilers" % chapter,
            "url": link,
            "description": desc,
            "color": DEFAULT_COLOR,
        }],
        "allowed_mentions": {"parse": []},
    }
    if role_id:
        payload["content"] = "<@&%s>" % role_id
        payload["allowed_mentions"] = {"roles": [str(role_id)]}
    return payload


def release_time(release):
    """Turn the thread's release date ("Sunday, 11 October 2026") into a UTC datetime."""
    text = release or ""
    m = re.search(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+),?\s+(\d{4})", text)
    if m:
        day, month, year = m.groups()
    else:
        m = re.search(r"([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", text)
        if not m:
            return None
        month, day, year = m.groups()
    if month.lower() not in MONTHS:
        return None
    try:
        return datetime(int(year), MONTHS.index(month.lower()) + 1, int(day), RELEASE_HOUR_UTC)
    except ValueError:
        return None


def release_payload(chapter, role_id):
    payload = {
        "embeds": [{
            "title": "📖 One Piece Chapter %d is out!" % chapter,
            "description": "Read it on the official release, free on release day:\n"
                           + OFFICIAL_LINKS,
            "color": DEFAULT_COLOR,
        }],
        "allowed_mentions": {"parse": []},
    }
    if role_id:
        payload["content"] = "<@&%s>" % role_id
        payload["allowed_mentions"] = {"roles": [str(role_id)]}
    return payload


def discord_send(webhook_url, payload, message_id=None, dry_run=False):
    """Post a new webhook message, or edit `message_id`. Returns the message id."""
    if dry_run:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return message_id or "dry-run"
    if message_id:
        url, method = "%s/messages/%s" % (webhook_url, message_id), "PATCH"
    else:
        url, method = webhook_url + "?wait=true", "POST"
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": DISCORD_UA}
    for _ in range(5):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                msg_id = json.loads(resp.read().decode("utf-8"))["id"]
            time.sleep(1)  # stay well under the webhook rate limit
            return msg_id
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if e.code == 429:
                try:
                    wait = float(json.loads(body).get("retry_after", 2))
                except ValueError:
                    wait = 2.0
                time.sleep(wait + 0.5)
                continue
            raise RuntimeError("Discord returned HTTP %d: %s" % (e.code, body[:300]))
    raise RuntimeError("Discord kept rate limiting the webhook")


# --------------------------------------------------------------------- main

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_PATH)


def check(config, state, dry_run=False):
    webhook = config["webhook_url"]
    log("Scouting OtakuKart for new spoilers...")
    chapters = find_chapter_posts()
    if not chapters:
        log("No One Piece spoiler thread found on the site.")
        return
    newest = chapters[0][0]

    # Oldest first, so a finishing chapter is flushed before the next one starts
    for chapter, post_id in reversed(chapters):
        post = fetch_post(post_id)
        items, release = parse_post(post)
        key = str(chapter)

        if key not in state and chapter != newest:
            # A chapter that was already over before the bot first saw it: don't replay it
            state[key] = {"header": "skipped", "release": "skipped", "items": {i["id"]: {"hash": i["hash"]} for i in items}}
            continue

        ch_state = state.setdefault(key, {"header": None, "items": {}})
        sent = 0
        pinged = False
        for item in items:
            seen = ch_state["items"].get(item["id"])
            if seen and seen["hash"] == item["hash"]:
                continue
            sent += 1
            if not ch_state["header"]:
                ch_state["header"] = discord_send(
                    webhook,
                    # No ping here: the first spoiler right below it carries one
                    header_payload(chapter, post["link"], release, None),
                    dry_run=dry_run,
                )
            old_ids = []
            if seen:
                old_ids = seen.get("messages") or ([seen["message"]] if seen.get("message") else [])
            role_id = config.get("ping_role_id")
            new_ids = []
            for n, embeds in enumerate(item_messages(item, chapter)):
                payload = {"embeds": embeds, "allowed_mentions": {"parse": []}}
                if n == 0 and role_id and not seen and (item.get("ping") or not pinged):
                    # Every new spoiler pings, confirmed or not. Several found in the same
                    # check share one ping; the brief spoilers and full summary always
                    # get their own.
                    payload["content"] = "<@&%s>" % role_id
                    payload["allowed_mentions"] = {"roles": [str(role_id)]}
                    pinged = True
                edit_id = old_ids[n] if n < len(old_ids) else None
                new_ids.append(discord_send(webhook, payload, message_id=edit_id, dry_run=dry_run))
            # Keep ids of parts a shortened summary no longer fills, so they can be reused
            new_ids += old_ids[len(new_ids):]
            ch_state["items"][item["id"]] = {"hash": item["hash"], "messages": new_ids}
            log("Chapter %d: %s '%s' (%s)" % (
                chapter, "edited" if old_ids else "posted", item["title"], item["id"]))
            if not dry_run:
                save_state(state)

        if not ch_state.get("release"):
            when = release_time(release)
            now = datetime.utcnow()
            if when and now >= when:
                if now - when > timedelta(days=2):
                    ch_state["release"] = "skipped"  # long out already: don't announce late
                else:
                    ch_state["release"] = discord_send(
                        webhook, release_payload(chapter, config.get("ping_role_id")),
                        dry_run=dry_run)
                    log("Chapter %d: announced the official release." % chapter)

        if sent:
            log("Chapter %d: %d spoiler(s) delivered to Discord." % (chapter, sent))
        elif not items:
            log("Chapter %d: thread is up, but no spoilers on it yet." % chapter)
        else:
            log("Chapter %d: calm seas, nothing new (%d spoiler(s) already seen)."
                % (chapter, len(items)))

    if not dry_run:
        save_state(state)


def read_commands(manual):
    """Console commands typed into the bot's window while it waits."""
    while True:
        try:
            command = input().strip().lower()
        except (EOFError, OSError):
            return  # no console attached
        if command in ("check", "c", "update"):
            manual.set()
        elif command in ("quit", "exit", "stop", "q"):
            log("Bot stopped. See you next chapter!")
            os._exit(0)
        elif command:
            print("Unknown command '%s'. Use 'check' or 'quit'." % command, flush=True)


def main():
    parser = argparse.ArgumentParser(description="Relay One Piece spoilers to Discord.")
    parser.add_argument("--once", action="store_true", help="check once and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the Discord payloads instead of sending them")
    args = parser.parse_args()

    config = load_json(CONFIG_PATH, {})
    config["webhook_url"] = (
        os.environ.get("DISCORD_WEBHOOK_URL") or config.get("webhook_url") or ""
    ).strip()
    if not config["webhook_url"] and not args.dry_run:
        sys.exit("No webhook set. Paste your channel's webhook URL into config.json "
                 "(\"webhook_url\") or set DISCORD_WEBHOOK_URL.")
    config["ping_role_id"] = (
        os.environ.get("DISCORD_PING_ROLE_ID") or config.get("ping_role_id") or ""
    ).strip()
    interval = max(1, float(config.get("poll_minutes", 5))) * 60

    state = load_json(STATE_PATH, {})
    # Fixed local check times, e.g. ["11:30", "23:30"]; a single "daily_time" also works
    times = config.get("daily_times") or config.get("daily_time") or []
    if isinstance(times, str):
        times = [t for t in re.split(r"[,\s]+", times) if t]
    times = sorted(tuple(int(p) for p in t.split(":")) for t in times)
    if times:
        schedule = "daily at " + ", ".join("%02d:%02d" % t for t in times)
    else:
        schedule = "every %g minute(s)" % (interval / 60)
    looping = not (args.once or args.dry_run)
    print("=" * 56)
    print("  ONE PIECE SPOILER BOT  -  Ani-Manga Bar")
    print("  Watching: otakukart.com live spoiler threads")
    print("  Schedule: %s" % (schedule if looping else "single check"))
    if looping:
        print("  Commands: type 'check' + Enter to check right now,")
        print("            'quit' + Enter to stop.")
        print("  Keep this window open.")
    print("=" * 56, flush=True)

    manual = threading.Event()
    if looping:
        threading.Thread(target=read_commands, args=(manual,), daemon=True).start()

    while True:
        try:
            check(config, state, dry_run=args.dry_run)
        except Exception as e:  # keep the loop alive through site/Discord hiccups
            log("Check failed: %s" % e)
            if not looping:
                sys.exit(1)  # let a scheduler (e.g. GitHub Actions) see the failure
        if not looping:
            break
        now = datetime.now()
        if times:
            candidates = [
                (now + timedelta(days=d)).replace(hour=h, minute=m, second=0, microsecond=0)
                for d in (0, 1) for h, m in times
            ]
            target = min(c for c in candidates if c > now)
            log("Dropping anchor. Next check at %s."
                % target.strftime("%A %Y-%m-%d %H:%M"))
        else:
            # Align checks to the clock: every 60 minutes means on the hour
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            elapsed = (now - midnight).total_seconds()
            target = midnight + timedelta(seconds=(elapsed // interval + 1) * interval)
            log("Dropping anchor. Next check at %s." % target.strftime("%H:%M"))
        # Short waits so PC sleep/wake doesn't skew it; a typed 'check' ends the wait early
        while datetime.now() < target and not manual.wait(15):
            pass
        if manual.is_set():
            manual.clear()
            log("Manual check requested. Setting sail!")


if __name__ == "__main__":
    main()
