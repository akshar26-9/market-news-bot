#!/usr/bin/env python3
"""
telegram_news_bot.py
--------------------
Indian + global market news -> short brief -> your Telegram.

Modes (set with the MODE env var):
  MODE=breaking   run every 15 min; only fires on high-impact keywords
  MODE=digest     run 3-4x/day; a "here's what happened" roundup

Secrets needed:
  TG_TOKEN        from @BotFather                          (required)
  TG_CHAT_ID      your numeric Telegram id, from @userinfobot (required)
  GEMINI_API_KEY  free, no card: aistudio.google.com/apikey (optional)
  GROQ_API_KEY    free, no card: console.groq.com/keys      (optional)

With no AI key set, you get a clean list of clickable headlines and pay nothing.
"""

import os
import re
import json
import html
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests

# --------------------------------------------------------------------------
# CONFIG - open each URL in a browser before trusting it; feeds move and die.
# --------------------------------------------------------------------------

FEEDS = {
    "India": [
        "https://www.moneycontrol.com/rss/marketreports.xml",
        "https://www.moneycontrol.com/rss/buzzingstocks.xml",
        "https://www.moneycontrol.com/rss/economy.xml",
        "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
        "https://www.livemint.com/rss/markets",
        "https://www.business-standard.com/rss/markets-106.rss",
    ],
    "Global": [
        "https://finance.yahoo.com/news/rssindex",
        "https://feeds.marketwatch.com/marketwatch/topstories/",
        "https://www.investing.com/rss/news_25.rss",
        "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258",
        "https://www.federalreserve.gov/feeds/press_all.xml",
    ],
}

# Only these trigger an instant alert. Trim this list hard or you WILL be spammed.
BREAKING_KEYWORDS = [
    "rbi", "repo rate", "fed", "fomc", "rate cut", "rate hike", "inflation",
    "cpi", "gdp", "sebi", "circuit", "crash", "plunge", "surge", "halt",
    "war", "tariff", "sanction", "default", "bankrupt", "downgrade",
    "results beat", "profit warning", "ipo opens", "block deal", "fii",
]

LOOKBACK_MIN = {"breaking": 20, "digest": 300}
MAX_ITEMS = 40
SEEN_FILE = Path("seen.json")
SEEN_KEEP = 800
TG_LIMIT = 3900          # Telegram caps messages at 4096 chars

# --------------------------------------------------------------------------


def load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except Exception:
            return set()
    return set()


def save_seen(seen: set) -> None:
    SEEN_FILE.write_text(json.dumps(list(seen)[-SEEN_KEEP:]))


def item_id(entry) -> str:
    raw = entry.get("id") or entry.get("link") or entry.get("title", "")
    return hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()[:16]


def published_dt(entry):
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return None


def fetch_items(lookback_min: int, seen: set):
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=lookback_min)
    out = []
    for region, urls in FEEDS.items():
        for url in urls:
            try:
                feed = feedparser.parse(url)
            except Exception as e:
                print(f"[warn] {url}: {e}")
                continue
            for entry in feed.entries[:25]:
                iid = item_id(entry)
                if iid in seen:
                    continue
                pub = published_dt(entry)
                if pub and pub < cutoff:
                    continue
                title = re.sub(r"\s+", " ", entry.get("title", "")).strip()
                if not title:
                    continue
                out.append({
                    "id": iid,
                    "region": region,
                    "title": title,
                    "source": feed.feed.get("title", url.split("/")[2]),
                    "link": entry.get("link", ""),
                })
    return out


def is_breaking(items):
    return [i for i in items if any(k in i["title"].lower() for k in BREAKING_KEYWORDS)]


def plain_headlines(items) -> str:
    """No-AI fallback. Headlines become clickable links - Telegram renders these."""
    out = []
    for region in ("India", "Global"):
        picks = [i for i in items if i["region"] == region][:8]
        if not picks:
            continue
        out.append(f"<b>{region.upper()}</b>")
        for i in picks:
            title = html.escape(i["title"][:140])
            out.append(f"• <a href=\"{html.escape(i['link'])}\">{title}</a>" if i["link"] else f"• {title}")
        out.append("")
    return "\n".join(out).strip()


def prompt_for(mode: str) -> str:
    if mode == "breaking":
        return (
            "These are market headlines from the last few minutes. Write a Telegram alert: "
            "max 4 bullets, one line each, only genuinely market-moving items. "
            "Lead each bullet with an emoji. Plain text only, no markdown, no HTML tags. "
            "If nothing is actually significant, reply with exactly: SKIP"
        )
    return (
        "These are market headlines from the last few hours. Write a Telegram digest: "
        "a one-line mood summary, then 'INDIA:' with up to 5 bullets, then 'GLOBAL:' with up to 5 bullets. "
        "One short line per bullet, plain language, no fluff, no disclaimers. "
        "Plain text only, no markdown, no HTML tags."
    )


def summarise(items, mode: str) -> str:
    headlines = "\n".join(f"- [{i['region']}] {i['title']} ({i['source']})" for i in items[:MAX_ITEMS])
    text = f"{prompt_for(mode)}\n\n{headlines}"

    gemini = os.environ.get("GEMINI_API_KEY")
    groq = os.environ.get("GROQ_API_KEY")

    try:
        if gemini:
            r = requests.post(
                "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
                headers={"x-goog-api-key": gemini, "Content-Type": "application/json"},
                json={"contents": [{"parts": [{"text": text}]}]},
                timeout=45,
            )
            r.raise_for_status()
            return html.escape(r.json()["candidates"][0]["content"]["parts"][0]["text"].strip())

        if groq:
            r = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {groq}", "Content-Type": "application/json"},
                json={
                    "model": "llama-3.3-70b-versatile",
                    "max_tokens": 700,
                    "messages": [{"role": "user", "content": text}],
                },
                timeout=45,
            )
            r.raise_for_status()
            return html.escape(r.json()["choices"][0]["message"]["content"].strip())

    except Exception as e:
        print(f"[warn] AI summary failed, sending plain headlines instead: {e}")

    return plain_headlines(items)


def send_telegram(text: str) -> None:
    """Splits long briefs across messages so nothing gets truncated."""
    token = os.environ["TG_TOKEN"]
    chat_id = os.environ["TG_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    chunks, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > TG_LIMIT:
            chunks.append(current)
            current = ""
        current += line + "\n"
    if current.strip():
        chunks.append(current)

    for chunk in chunks:
        resp = requests.post(url, json={
            "chat_id": chat_id,
            "text": chunk.strip(),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=30)
        print(resp.status_code, resp.text[:300])
        resp.raise_for_status()


def main():
    mode = os.environ.get("MODE", "digest").lower()
    seen = load_seen()

    items = fetch_items(LOOKBACK_MIN.get(mode, 300), seen)
    if not items:
        print("nothing new")
        return

    if mode == "breaking":
        items = is_breaking(items)
        if not items:
            print("nothing breaking")
            return

    body = summarise(items, mode)

    if body.strip().upper().startswith("SKIP"):
        print("model judged it not worth sending")
        seen.update(i["id"] for i in items)
        save_seen(seen)
        return

    stamp = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=5, minutes=30)))
    header = "🚨 <b>MARKET ALERT</b>" if mode == "breaking" else "📊 <b>MARKET BRIEF</b>"
    send_telegram(f"{header} — {stamp.strftime('%d %b, %H:%M')} IST\n\n{body}")

    seen.update(i["id"] for i in items)
    save_seen(seen)


if __name__ == "__main__":
    main()
