from __future__ import annotations

import asyncio
from contextlib import contextmanager
import hashlib
import html
import json
import logging
import math
import os
import re
import smtplib
import sqlite3
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.message import EmailMessage
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Iterator
from dotenv import load_dotenv
from websockets.asyncio.server import ServerConnection, serve

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env", override=False)
WEB_DIR = ROOT / "web"
DB_PATH = Path(os.environ.get("AI_HOT_MONITOR_DB", ROOT / "data" / "monitor.db"))
DEFAULTS = {
    "scope": "AI 编程",
    "interval_minutes": "30",
    "ai_base_url": "https://api.openai-proxy.org/v1",
    "ai_model": "deepseek-v4.1-flash",
    "ai_api_key": "",
    "twitter_api_key": "",
    "smtp_host": "",
    "smtp_port": "587",
    "smtp_username": "",
    "smtp_password": "",
    "smtp_sender": "",
    "smtp_recipient": "",
    "smtp_starttls": "1",
    "smtp_use_ssl": "0",
    "telegram_bot_token": "",
    "telegram_chat_id": "",
}
ENV_SETTINGS = {
    "scope": "AI_HOT_MONITOR_SCOPE",
    "interval_minutes": "AI_HOT_MONITOR_INTERVAL_MINUTES",
    "ai_base_url": "AI_BASE_URL",
    "ai_model": "AI_MODEL",
    "ai_api_key": "AI_API_KEY",
    "twitter_api_key": "TWITTERAPI_API_KEY",
    "smtp_host": "SMTP_HOST",
    "smtp_port": "SMTP_PORT",
    "smtp_username": "SMTP_USERNAME",
    "smtp_password": "SMTP_PASSWORD",
    "smtp_sender": "SMTP_SENDER",
    "smtp_recipient": "SMTP_RECIPIENT",
    "smtp_starttls": "SMTP_STARTTLS",
    "smtp_use_ssl": "SMTP_USE_SSL",
    "telegram_bot_token": "TELEGRAM_BOT_TOKEN",
    "telegram_chat_id": "TELEGRAM_CHAT_ID",
}
LEGACY_ENV_SETTINGS = {
    "ai_base_url": "OPENROUTER_BASE_URL",
    "ai_model": "OPENROUTER_MODEL",
    "ai_api_key": "OPENROUTER_API_KEY",
}
SCAN_LOCK = threading.Lock()
SCAN_STATE: dict[str, Any] = {"running": False, "error": None, "last_started": None, "progress": ""}
SCAN_LOGGER = logging.getLogger("ai_hot_monitor.scan")
SCAN_LOG_LOCK = threading.Lock()
SCAN_LOG_PATH = ROOT / "data" / "monitor.log"
WS_CLIENTS: set[ServerConnection] = set()
WS_LOOP: asyncio.AbstractEventLoop | None = None
TWITTER_REQUEST_LOCK = threading.Lock()
TWITTER_LAST_REQUEST = 0.0


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


class DuckDuckGoResultParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self.current: dict[str, str] | None = None
        self.capture = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if "result__a" in classes:
            self._finish()
            self.current = {"title": "", "url": attributes.get("href") or "", "summary": ""}
            self.capture = "title"
        elif "result__snippet" in classes and self.current is not None:
            self.capture = "summary"

    def handle_data(self, data: str) -> None:
        if self.current is not None and self.capture:
            self.current[self.capture] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.capture == "title":
            self.capture = ""

    def _finish(self) -> None:
        if self.current and self.current["title"].strip() and self.current["url"].strip():
            self.results.append({key: value.strip() for key, value in self.current.items()})
        self.current = None
        self.capture = ""


def clean_html(value: str) -> str:
    parser = TextExtractor()
    parser.feed(html.unescape(value or ""))
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()


def normalize_title(title: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", title.casefold())[:240]


def parse_feed(payload: bytes, platform: str = "Google News") -> list[dict[str, Any]]:
    root = ET.fromstring(payload)
    results = []
    for item in root.findall(".//item"):
        title = clean_html(item.findtext("title", ""))
        link = item.findtext("link", "").strip()
        if not title or not link:
            continue
        source = item.find("source")
        publisher = clean_html(source.text or "") if source is not None else (urllib.parse.urlparse(link).hostname or platform)
        results.append({
            "title": title,
            "url": link,
            "summary": clean_html(item.findtext("description", ""))[:1600],
            "source": publisher,
            "publisher": publisher,
            "source_url": source.attrib.get("url", "") if source is not None else link,
            "published_at": item.findtext("pubDate", ""),
            "platform": platform,
        })
    return results


def parse_duckduckgo_search(payload: str, region: str = "global") -> list[dict[str, Any]]:
    parser = DuckDuckGoResultParser()
    parser.feed(payload)
    parser.close()
    parser._finish()
    results = []
    seen_urls = set()
    for item in parser.results:
        url = html.unescape(item["url"])
        parsed = urllib.parse.urlparse(url)
        if parsed.hostname in {"duckduckgo.com", "www.duckduckgo.com"} and parsed.path in {"/l/", "/l"}:
            url = urllib.parse.parse_qs(parsed.query).get("uddg", [url])[0]
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or url in seen_urls:
            continue
        seen_urls.add(url)
        publisher = parsed.hostname.removeprefix("www.")
        results.append({
            "title": clean_html(item["title"]), "summary": clean_html(item["summary"])[:1600],
            "url": url, "source": publisher, "publisher": publisher, "source_url": url,
            "published_at": "", "platform": "DuckDuckGo", "region": region,
            "language": "zh" if region == "china" else "en",
        })
    return results


def parse_hacker_news_search(payload: dict[str, Any]) -> list[dict[str, Any]]:
    results = []
    for item in payload.get("hits", []):
        if not isinstance(item, dict) or not item.get("objectID") or not item.get("title"):
            continue
        url = str(item.get("url") or f"https://news.ycombinator.com/item?id={item['objectID']}").strip()
        if urllib.parse.urlparse(url).scheme not in {"http", "https"}:
            continue
        publisher = urllib.parse.urlparse(url).hostname or "Hacker News"
        results.append({
            "title": clean_html(str(item["title"])),
            "summary": clean_html(str(item.get("story_text") or ""))[:1600],
            "url": url, "source": f"Hacker News · {item.get('author', 'unknown')}",
            "publisher": publisher.removeprefix("www."), "source_url": url,
            "published_at": str(item.get("created_at", "")), "platform": "Hacker News",
            "external_id": str(item["objectID"]), "region": "global", "language": "en",
        })
    return results


def _nonnegative_count(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        count = int(value)
    except (TypeError, ValueError):
        return None
    return count if count >= 0 else None


def parse_twitter_search(payload: dict[str, Any]) -> list[dict[str, Any]]:
    results = []
    for item in payload.get("tweets", []):
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        external_id = str(item.get("id", "")).strip()
        if not text or not external_id:
            continue
        author = item.get("author") if isinstance(item.get("author"), dict) else {}
        likes = _nonnegative_count(item.get("likeCount"))
        retweets = _nonnegative_count(item.get("retweetCount"))
        views = _nonnegative_count(item.get("viewCount"))
        followers = _nonnegative_count(author.get("followers"))
        if item.get("isReply") is True or str(item.get("inReplyToId", "")).strip():
            content_type = "reply"
        elif item.get("retweeted_tweet") or item.get("retweetedTweet") or item.get("isRetweet"):
            content_type = "retweet"
        elif item.get("quoted_tweet") or item.get("quotedTweet") or item.get("isQuote"):
            content_type = "quote"
        else:
            content_type = "original"
        username = str(author.get("userName", "")).strip()
        url = str(item.get("url", "")).strip()
        if not url:
            account_path = username or "i"
            url = f"https://x.com/{account_path}/status/{external_id}"
        results.append({
            "title": text[:300],
            "summary": text[:1600],
            "url": url,
            "source": f"X @{username}" if username else "X",
            "source_url": url,
            "published_at": str(item.get("createdAt", "")),
            "platform": "twitterapi.io",
            "external_id": external_id,
            "publisher": username,
            "region": "global",
            "language": str(item.get("lang", "")),
            "like_count": likes,
            "retweet_count": retweets,
            "reply_count": _nonnegative_count(item.get("replyCount")),
            "view_count": views,
            "author_followers": followers,
            "author_verified": bool(author.get("isBlueVerified")),
            "content_type": content_type,
        })
    return results


def is_x_source(source: dict[str, Any]) -> bool:
    hostname = urllib.parse.urlparse(str(source.get("url", ""))).hostname or ""
    return source.get("platform") == "twitterapi.io" or hostname.casefold().removeprefix("www.") in {
        "x.com", "twitter.com",
    }


def twitter_source_is_eligible(source: dict[str, Any]) -> bool:
    if not is_x_source(source) or source.get("content_type") != "original":
        return False
    counts = (
        _nonnegative_count(source.get("like_count")),
        _nonnegative_count(source.get("retweet_count")),
        _nonnegative_count(source.get("view_count")),
        _nonnegative_count(source.get("author_followers")),
    )
    return all(count is not None for count in counts) and counts[0] >= 10 and counts[1] >= 5 and counts[2] >= 500 and counts[3] >= 100


def parse_account_identifier(value: str) -> tuple[str, str, str] | None:
    value = value.strip()
    handle_match = re.fullmatch(r"@([A-Za-z0-9_]{1,15})", value)
    if handle_match:
        return "x", "username", handle_match.group(1)
    x_uid_match = re.fullmatch(r"(?:x|twitter):([0-9]{5,})", value, re.IGNORECASE)
    if x_uid_match:
        return "x", "user_id", x_uid_match.group(1)
    if re.fullmatch(r"[0-9]{8,}", value):
        return "x", "user_id", value

    parsed = urllib.parse.urlparse(value if "://" in value else f"https://{value}")
    hostname = (parsed.hostname or "").casefold().removeprefix("www.")
    parts = [part for part in parsed.path.split("/") if part]
    if hostname in {"x.com", "twitter.com"} and len(parts) == 1:
        if parts[0].casefold() not in {"home", "search", "explore", "notifications", "messages", "settings", "i"}:
            return "x", "username", parts[0].lstrip("@")
    if hostname == "space.bilibili.com" and parts and parts[0].isdigit():
        return "bilibili", "user_id", parts[0]
    if hostname in {"weibo.com", "www.weibo.com"} and parts:
        user_id = parts[1] if parts[0] == "u" and len(parts) > 1 else parts[0]
        if re.fullmatch(r"[A-Za-z0-9_-]+", user_id):
            return "weibo", "user_id", user_id
    return None


@contextmanager
def connect_db() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db() -> None:
    with connect_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS keywords (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phrase TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS stories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fingerprint TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT '',
                published_at TEXT NOT NULL DEFAULT '',
                matched_keyword TEXT NOT NULL DEFAULT '',
                verdict TEXT NOT NULL DEFAULT 'pending',
                confidence REAL NOT NULL DEFAULT 0,
                relevance_score INTEGER NOT NULL DEFAULT 0,
                urgency TEXT NOT NULL DEFAULT 'medium',
                summary_zh TEXT NOT NULL DEFAULT '',
                translation TEXT NOT NULL DEFAULT '',
                translation_language TEXT NOT NULL DEFAULT '',
                rationale TEXT NOT NULL DEFAULT '',
                relevance_rationale TEXT NOT NULL DEFAULT '',
                corroborations INTEGER NOT NULL DEFAULT 1,
                discovered_at TEXT NOT NULL,
                notified INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                found_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'ok'
            );
            CREATE TABLE IF NOT EXISTS story_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                story_id INTEGER NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
                platform TEXT NOT NULL,
                external_id TEXT NOT NULL,
                region TEXT NOT NULL DEFAULT '',
                language TEXT NOT NULL DEFAULT '',
                publisher TEXT NOT NULL DEFAULT '',
                publisher_domain TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL,
                published_at TEXT NOT NULL DEFAULT '',
                discovered_at TEXT NOT NULL,
                like_count INTEGER,
                retweet_count INTEGER,
                reply_count INTEGER,
                view_count INTEGER,
                author_followers INTEGER,
                author_verified INTEGER NOT NULL DEFAULT 0,
                content_type TEXT NOT NULL DEFAULT '',
                UNIQUE(platform, external_id)
            );
            CREATE TABLE IF NOT EXISTS query_translations (
                original TEXT PRIMARY KEY,
                english TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS query_expansions (
                original TEXT PRIMARY KEY,
                expanded TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)
        for key, value in DEFAULTS.items():
            db.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (key, value))
        story_columns = {row["name"] for row in db.execute("PRAGMA table_info(stories)")}
        for column, declaration in (
            ("relevance_score", "INTEGER NOT NULL DEFAULT 0"),
            ("urgency", "TEXT NOT NULL DEFAULT 'medium'"),
            ("summary_zh", "TEXT NOT NULL DEFAULT ''"),
            ("translation", "TEXT NOT NULL DEFAULT ''"),
            ("translation_language", "TEXT NOT NULL DEFAULT ''"),
            ("relevance_rationale", "TEXT NOT NULL DEFAULT ''"),
        ):
            if column not in story_columns:
                db.execute(f"ALTER TABLE stories ADD COLUMN {column} {declaration}")
        keyword_columns = {row["name"] for row in db.execute("PRAGMA table_info(keywords)")}
        if "enabled" not in keyword_columns:
            db.execute("ALTER TABLE keywords ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1")
        db.execute("""INSERT OR IGNORE INTO story_sources
            (story_id,platform,external_id,publisher,title,summary,url,published_at,discovered_at)
            SELECT id,'legacy','legacy:' || id,source,title,summary,url,published_at,discovered_at FROM stories""")
        source_columns = {row["name"] for row in db.execute("PRAGMA table_info(story_sources)")}
        for column, declaration in (
            ("region", "TEXT NOT NULL DEFAULT ''"),
            ("language", "TEXT NOT NULL DEFAULT ''"),
            ("like_count", "INTEGER"),
            ("retweet_count", "INTEGER"),
            ("reply_count", "INTEGER"),
            ("view_count", "INTEGER"),
            ("author_followers", "INTEGER"),
            ("author_verified", "INTEGER NOT NULL DEFAULT 0"),
            ("content_type", "TEXT NOT NULL DEFAULT ''"),
        ):
            if column not in source_columns:
                db.execute(f"ALTER TABLE story_sources ADD COLUMN {column} {declaration}")


def get_settings() -> dict[str, str]:
    with connect_db() as db:
        values = {row["key"]: row["value"] for row in db.execute("SELECT key, value FROM settings")}
    settings = {**DEFAULTS, **values}
    for setting, environment_name in ENV_SETTINGS.items():
        value = os.environ.get(environment_name, "").strip()
        legacy_openrouter_config = "openrouter.ai" in settings.get("ai_base_url", "")
        if not value and setting in LEGACY_ENV_SETTINGS and legacy_openrouter_config:
            value = os.environ.get(LEGACY_ENV_SETTINGS[setting], "").strip()
        if value:
            settings[setting] = value
    return settings


def ai_api_key_required(base_url: str) -> bool:
    hostname = (urllib.parse.urlparse(base_url).hostname or "").casefold()
    return hostname not in {"localhost", "127.0.0.1", "::1", "ollama"} and not hostname.endswith(".local")


def save_settings(values: dict[str, Any]) -> None:
    allowed = {
        "scope", "interval_minutes", "ai_base_url", "ai_model", "ai_api_key", "twitter_api_key",
        "smtp_host", "smtp_port", "smtp_username", "smtp_password", "smtp_sender", "smtp_recipient",
        "smtp_starttls", "smtp_use_ssl", "telegram_bot_token", "telegram_chat_id",
    }
    updates = {key: str(value).strip() for key, value in values.items() if key in allowed and value is not None}
    if "scope" in updates:
        updates["scope"] = updates["scope"][:120] or DEFAULTS["scope"]
    if "interval_minutes" in updates:
        try:
            updates["interval_minutes"] = str(max(5, min(1440, int(updates["interval_minutes"]))))
        except ValueError:
            updates["interval_minutes"] = DEFAULTS["interval_minutes"]
    for key in ("ai_base_url", "ai_model"):
        if key in updates:
            updates[key] = updates[key][:500]
    if "smtp_port" in updates:
        try:
            updates["smtp_port"] = str(max(1, min(65535, int(updates["smtp_port"]))))
        except ValueError:
            updates["smtp_port"] = DEFAULTS["smtp_port"]
    for key in ("smtp_starttls", "smtp_use_ssl"):
        if key in updates:
            updates[key] = "1" if updates[key].lower() in {"1", "true", "yes", "on"} else "0"
    if "ai_base_url" in updates and "ai_api_key" not in updates:
        with connect_db() as db:
            saved_base_url = db.execute("SELECT value FROM settings WHERE key='ai_base_url'").fetchone()
        previous_host = urllib.parse.urlparse(saved_base_url["value"] if saved_base_url else "").hostname
        next_host = urllib.parse.urlparse(updates["ai_base_url"]).hostname
        if previous_host and next_host and previous_host.casefold() != next_host.casefold():
            updates["ai_api_key"] = ""
    with connect_db() as db:
        db.executemany("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", updates.items())


NEWS_REGIONS = {
    "china": {"hl": "zh-CN", "gl": "CN", "ceid": "CN:zh-Hans"},
    "global": {"hl": "en-US", "gl": "US", "ceid": "US:en"},
}


def fetch_feed(query: str, region: str = "china") -> list[dict[str, str]]:
    locale = NEWS_REGIONS.get(region, NEWS_REGIONS["china"])
    params = urllib.parse.urlencode({"q": query, **locale})
    request = urllib.request.Request(
        f"https://news.google.com/rss/search?{params}",
        headers={"User-Agent": "AIHotMonitor/1.0 (RSS reader)"},
    )
    with urllib.request.urlopen(request, timeout=18) as response:
        stories = parse_feed(response.read(2_000_000))
    for story in stories:
        story["region"] = region
        story["language"] = "zh" if region == "china" else "en"
    return stories


def fetch_bing_search(query: str) -> list[dict[str, Any]]:
    params = urllib.parse.urlencode({"q": query, "format": "rss", "count": 10})
    request = urllib.request.Request(
        f"https://www.bing.com/search?{params}",
        headers={"User-Agent": "AIHotMonitor/1.0 (personal non-commercial RSS aggregator)"},
    )
    with urllib.request.urlopen(request, timeout=18) as response:
        return parse_feed(response.read(2_000_000), "Bing RSS")


def fetch_duckduckgo_search(query: str, region: str = "global") -> list[dict[str, Any]]:
    locale = "cn-zh" if region == "china" else "us-en"
    params = urllib.parse.urlencode({"q": query, "kl": locale})
    request = urllib.request.Request(
        f"https://duckduckgo.com/?{params}",
        headers={"User-Agent": "AIHotMonitor/1.0 (personal topic monitor)", "Accept": "text/html"},
    )
    with urllib.request.urlopen(request, timeout=18) as response:
        payload = response.read(2_000_000).decode("utf-8", errors="replace")
    return parse_duckduckgo_search(payload, region)


def fetch_hacker_news_search(query: str) -> list[dict[str, Any]]:
    params = urllib.parse.urlencode({"query": query, "tags": "story", "hitsPerPage": 20})
    request = urllib.request.Request(
        f"https://hn.algolia.com/api/v1/search_by_date?{params}",
        headers={"User-Agent": "AIHotMonitor/1.0 (personal topic monitor)"},
    )
    with urllib.request.urlopen(request, timeout=18) as response:
        payload = json.loads(response.read(1_000_000))
    return parse_hacker_news_search(payload)


def _twitter_api_get(path: str, params: dict[str, str], api_key: str) -> dict[str, Any]:
    global TWITTER_LAST_REQUEST
    with TWITTER_REQUEST_LOCK:
        delay = 5.0 - (time.monotonic() - TWITTER_LAST_REQUEST)
        if TWITTER_LAST_REQUEST and delay > 0:
            time.sleep(delay)
        TWITTER_LAST_REQUEST = time.monotonic()
        query = urllib.parse.urlencode(params)
        request = urllib.request.Request(
            f"https://api.twitterapi.io{path}?{query}",
            headers={"X-API-Key": api_key, "User-Agent": "AIHotMonitor/1.0"},
        )
        with urllib.request.urlopen(request, timeout=18) as response:
            payload = json.loads(response.read(1_000_000))
    if not isinstance(payload, dict):
        raise ValueError("TwitterAPI.io 返回了无效响应")
    return payload


def fetch_twitter_search(query: str, api_key: str) -> list[dict[str, Any]]:
    if not api_key:
        return []
    results = []
    seen_ids: set[str] = set()
    for query_type in ("Top", "Latest"):
        cursor = ""
        for _ in range(2):
            payload = _twitter_api_get("/twitter/tweet/advanced_search", {
                "query": query, "queryType": query_type, "cursor": cursor,
            }, api_key)
            for source in parse_twitter_search(payload):
                if source["external_id"] not in seen_ids:
                    seen_ids.add(source["external_id"])
                    results.append(source)
            next_cursor = str(payload.get("next_cursor", ""))
            if not payload.get("has_next_page") or not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
    return results


def fetch_twitter_account(identifier_type: str, identifier: str, api_key: str) -> list[dict[str, Any]]:
    if not api_key:
        return []
    profile: dict[str, Any] = {}
    if identifier_type == "username":
        payload = _twitter_api_get("/twitter/user/info", {"userName": identifier}, api_key)
        profile = payload.get("data", payload)
        if isinstance(profile, dict) and isinstance(profile.get("user"), dict):
            profile = profile["user"]
        if not isinstance(profile, dict):
            profile = {}
    user_id = str(profile.get("id") or profile.get("userId") or "")
    user_param = {"userId": user_id} if user_id else {"userName": identifier}
    results = []
    cursor = ""
    seen_ids: set[str] = set()
    for _ in range(2):
        payload = _twitter_api_get("/twitter/user/last_tweets", {
            **user_param, "cursor": cursor, "includeReplies": "false",
        }, api_key)
        tweets = payload.get("tweets", [])
        normalized_tweets = []
        for item in tweets if isinstance(tweets, list) else []:
            if not isinstance(item, dict):
                continue
            item = dict(item)
            author = item.get("author") if isinstance(item.get("author"), dict) else {}
            item["author"] = {
                **{key: profile[key] for key in ("userName", "id", "followers", "isBlueVerified") if key in profile},
                **author,
            }
            normalized_tweets.append(item)
        for source in parse_twitter_search({"tweets": normalized_tweets}):
            if source["external_id"] not in seen_ids:
                source["source"] = f"X @{source['publisher']}" if source["publisher"] else "X"
                seen_ids.add(source["external_id"])
                results.append(source)
        next_cursor = str(payload.get("next_cursor", ""))
        if not payload.get("has_next_page") or not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
    return results


def translate_query_to_english(query: str, settings: dict[str, str]) -> str:
    if not re.search(r"[\u4e00-\u9fff]", query):
        return ""
    with connect_db() as db:
        cached = db.execute("SELECT english FROM query_translations WHERE original=?", (query,)).fetchone()
    if cached:
        return str(cached["english"])

    base_url = settings.get("ai_base_url", "").rstrip("/")
    model = settings.get("ai_model", "").strip()
    api_key = settings.get("ai_api_key", "").strip()
    if not base_url or not model or (ai_api_key_required(base_url) and not api_key):
        return ""

    schema = {
        "type": "object",
        "properties": {"english_query": {"type": "string"}},
        "required": ["english_query"],
        "additionalProperties": False,
    }
    body_data = {
        "model": model,
        "temperature": 0,
        "max_tokens": 96,
        "messages": [
            {"role": "system", "content": "Translate search terms into concise natural English keywords. Return only a JSON object with an english_query string."},
            {"role": "user", "content": query},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "search_query", "strict": True, "schema": schema},
        },
    }
    if "openrouter.ai" in base_url:
        body_data["provider"] = {"require_parameters": True}
    else:
        body_data["response_format"] = {"type": "json_object"}
    body = json.dumps(body_data).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(f"{base_url}/chat/completions", data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read(500_000))
        content = data["choices"][0]["message"]["content"]
        result = json.loads(content)
        english = str(result.get("english_query", "")).strip()[:240]
        if english:
            with connect_db() as db:
                db.execute("INSERT OR REPLACE INTO query_translations(original,english,updated_at) VALUES(?,?,?)",
                           (query, english, datetime.now(timezone.utc).isoformat()))
        return english
    except (OSError, KeyError, ValueError, TypeError, json.JSONDecodeError, urllib.error.URLError):
        return ""


def expand_search_query(query: str, settings: dict[str, str]) -> list[str]:
    with connect_db() as db:
        cached = db.execute("SELECT expanded FROM query_expansions WHERE original=?", (query,)).fetchone()
    if cached:
        try:
            expansions = json.loads(cached["expanded"])
            return [str(item) for item in expansions if str(item).strip()][:2]
        except (TypeError, ValueError, json.JSONDecodeError):
            pass

    base_url = settings.get("ai_base_url", "").rstrip("/")
    model = settings.get("ai_model", "").strip()
    api_key = settings.get("ai_api_key", "").strip()
    if not base_url or not model or (ai_api_key_required(base_url) and not api_key):
        return []

    schema = {
        "type": "object",
        "properties": {"queries": {"type": "array", "items": {"type": "string"}, "maxItems": 2}},
        "required": ["queries"],
        "additionalProperties": False,
    }
    body_data = {
        "model": model,
        "temperature": 0,
        "max_tokens": 120,
        "messages": [
            {"role": "system", "content": (
                "为给定的热点监控词生成最多两个具体、紧密相关的搜索短语，可用产品名、常见别称或直接相关功能；"
                "不要扩展成泛化的 AI/科技词。只返回 JSON 对象 queries。"
            )},
            {"role": "user", "content": query},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "query_expansion", "strict": True, "schema": schema},
        },
    }
    if "openrouter.ai" in base_url:
        body_data["provider"] = {"require_parameters": True}
    else:
        body_data["response_format"] = {"type": "json_object"}
    request = urllib.request.Request(
        f"{base_url}/chat/completions", data=json.dumps(body_data).encode("utf-8"),
        headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {api_key}"} if api_key else {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read(500_000))
        content = payload["choices"][0]["message"]["content"]
        result = json.loads(content)
        expansions = []
        for item in result.get("queries", []):
            phrase = re.sub(r"\s+", " ", str(item)).strip()[:100]
            if phrase and phrase.casefold() != query.casefold() and phrase.casefold() not in {
                existing.casefold() for existing in expansions
            }:
                expansions.append(phrase)
            if len(expansions) == 2:
                break
        with connect_db() as db:
            db.execute("INSERT OR REPLACE INTO query_expansions(original,expanded,updated_at) VALUES(?,?,?)",
                       (query, json.dumps(expansions, ensure_ascii=False), datetime.now(timezone.utc).isoformat()))
        return expansions
    except (OSError, KeyError, ValueError, TypeError, json.JSONDecodeError, urllib.error.URLError) as exc:
        log_scan(f"[warning] Query Expand for '{query}' unavailable: {str(exc)[:120]}")
        return []


def build_search_query(*terms: str) -> str:
    unique_terms = list(dict.fromkeys(term.strip() for term in terms if term and term.strip()))
    if len(unique_terms) < 2:
        return unique_terms[0] if unique_terms else ""
    return " OR ".join(f'"{term.replace(chr(34), "")}"' for term in unique_terms)


def translation_target_for_story(story: dict[str, Any]) -> str:
    languages = {
        str(source.get("language", "")).casefold().split("-")[0]
        for source in story.get("sources", []) if source.get("language")
    }
    if languages & {"zh", "ja", "ko"}:
        return "zh"
    if languages & {"es", "fr", "pt", "de", "it", "ru", "ar", "hi", "th", "vi"}:
        return "en"
    text = " ".join((str(story.get("title", "")), str(story.get("summary", ""))))
    if re.search(r"[\u3040-\u30ff\uac00-\ud7af]", text):
        return "zh"
    if re.search(r"[\u4e00-\u9fff]", text):
        return "zh"
    return "en"


def assess_story(story: dict[str, Any], corroborations: int, settings: dict[str, str]) -> dict[str, Any]:
    translation_language = translation_target_for_story(story)
    base_url = settings.get("ai_base_url", "").rstrip("/")
    model = settings.get("ai_model", "").strip()
    if not base_url or not model:
        return {"verdict": "unverified", "confidence": 0, "relevance_score": 0, "urgency": "medium",
            "summary_zh": "", "relevance_rationale": "", "translation": "", "translation_language": translation_language,
            "rationale": "未配置 AI 核验模型。"}
    if ai_api_key_required(base_url) and not settings.get("ai_api_key", "").strip():
        return {"verdict": "unverified", "confidence": 0, "relevance_score": 0, "urgency": "medium",
            "summary_zh": "", "relevance_rationale": "", "translation": "", "translation_language": translation_language,
            "rationale": "未配置 AI API Key。"}

    evidence = story.get("sources", [])
    evidence_text = "\n".join(
        f"- {source.get('publisher') or source.get('platform')}: {source.get('title', '')}；"
        f"{source.get('summary', '')[:500]}；{source.get('url', '')}"
        for source in evidence
    )
    target_name = "简体中文" if translation_language == "zh" else "英文"
    prompt = (
        "你是个人热点筛选助手，优先保留对用户监控主题有直接价值的新消息。真实性、主题相关性和紧急度是三个独立维度。"
        "真实性谨慎识别冒充官方、旧闻翻炒、夸张标题和无来源消息，证据不足时返回 uncertain。"
        "relevance_score 表示对监控主题的直接匹配和实际信息价值，范围0-100；50-60分为有一定相关性但需结合理由判断的区间，低于55分默认不进入工作台的相关性筛选结果。"
        "根据原始关键词及查询扩展词判断语义相关性，不要求标题或正文逐字包含原始关键词；扩展词只用于理解同一产品、别称或直接相关功能，不可据此泛化到整个AI行业。"
        "relevance_rationale 用简体中文简要解释该内容为什么与监控主题相关或不相关，结合标题/摘要中的具体信息，不得重复标题。"
        "最终只返回一个 JSON 对象，包含要求的全部字段，不要 Markdown 代码块或额外说明。"
        "不要因为出现 AI、模型或科技名词就判高相关。urgency 根据现实影响和时效选择 urgent/high/medium/low。"
        f"summary_zh 用简洁中文概括；translation 将原文核心内容翻译为{target_name}，若原文已经是目标语言则返回空字符串。"
        "只根据给定来源，不要把推测说成事实。\n"
        f"监控主题/关键词：{story.get('matched_keyword', '')}\n查询扩展词：{', '.join(story.get('query_expansions', [])) or '无'}\n"
        f"标题：{story['title']}\n摘要：{story['summary']}\n发布源：{story['source']}\n"
        f"独立发布者数：{corroborations}\n链接域名：{urllib.parse.urlparse(story['url']).netloc}\n"
        f"来源证据：\n{evidence_text}"
    )
    few_shot = [
        {"role": "user", "content": "监控主题：Claude Code。标题：MacBook 新款屏幕亮度提升。摘要：苹果更新笔记本显示屏参数，与 Claude Code 无直接关系。"},
        {"role": "assistant", "content": json.dumps({
            "verdict": "uncertain", "confidence": 0.3, "relevance_score": 8, "urgency": "low",
            "summary_zh": "苹果更新笔记本屏幕参数。",
            "relevance_rationale": "内容只涉及屏幕参数，与 Claude Code 无直接关联。",
            "rationale": "内容没有涉及 Claude Code 或其直接关联产品。",
            "evidence_gaps": ["缺少与监控主题相关的事实"], "translation": "", "translation_language": "en",
        }, ensure_ascii=False)},
        {"role": "user", "content": "监控主题：OpenAI Codex 编程代理。标题：OpenAI 为 Codex 增加后台任务。摘要：官方更新说明 Codex 可在后台并行执行开发任务。"},
        {"role": "assistant", "content": json.dumps({
            "verdict": "uncertain", "confidence": 0.55, "relevance_score": 94, "urgency": "high",
            "summary_zh": "Codex 新增后台并行任务能力。",
            "relevance_rationale": "更新直接涉及 Codex 的开发能力，与监控主题高度匹配。",
            "rationale": "官方产品更新与监控主题直接相关；真实性仍需按来源证据判断。",
            "evidence_gaps": ["需核对官方更新原文"], "translation": "", "translation_language": "en",
        }, ensure_ascii=False)},
    ]
    body_data = {
        "model": model,
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": "按示例校准主题匹配度。相关性低不等于内容虚假；缺乏证据时真实性必须判为 uncertain。只返回符合要求的 JSON 对象，不要 Markdown 或额外文本。"},
            *few_shot,
            {"role": "user", "content": prompt},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "story_assessment",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "verdict": {"type": "string", "enum": ["credible", "suspicious", "uncertain"]},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "relevance_score": {"type": "integer", "minimum": 0, "maximum": 100},
                        "relevance_rationale": {"type": "string"},
                        "urgency": {"type": "string", "enum": ["urgent", "high", "medium", "low"]},
                        "summary_zh": {"type": "string"},
                        "translation": {"type": "string"},
                        "translation_language": {"type": "string", "enum": ["zh", "en"]},
                        "rationale": {"type": "string"},
                        "evidence_gaps": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["verdict", "confidence", "relevance_score", "urgency", "summary_zh",
                                 "relevance_rationale", "translation", "translation_language", "rationale", "evidence_gaps"],
                    "additionalProperties": False,
                },
            },
        },
    }
    if "openrouter.ai" in base_url:
        body_data["provider"] = {"require_parameters": True}
    else:
        body_data["response_format"] = {"type": "json_object"}
    body = json.dumps(body_data).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if settings.get("ai_api_key"):
        headers["Authorization"] = f"Bearer {settings['ai_api_key']}"
    request = urllib.request.Request(f"{base_url}/chat/completions", data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=35) as response:
            data = json.loads(response.read(1_000_000))
        content = data["choices"][0]["message"]["content"]
        match = re.search(r"\{.*?\}", content, flags=re.DOTALL)
        result = json.loads(match.group(0) if match else content)
        verdict = result.get("verdict", "uncertain")
        if verdict not in {"credible", "suspicious", "uncertain"}:
            verdict = "uncertain"
        try:
            confidence = max(0.0, min(1.0, float(result.get("confidence", 0))))
        except (TypeError, ValueError):
            confidence = 0.0
        try:
            relevance = max(0, min(100, int(result.get("relevance_score", 0) or 0)))
        except (TypeError, ValueError):
            relevance = 0
        urgency = result.get("urgency", "medium")
        if urgency not in {"urgent", "high", "medium", "low"}:
            urgency = "medium"
        return {
            "verdict": verdict,
            "confidence": confidence,
            "relevance_score": relevance,
            "urgency": urgency,
            "summary_zh": str(result.get("summary_zh", ""))[:500],
            "relevance_rationale": str(result.get("relevance_rationale", ""))[:500],
            "translation": str(result.get("translation", ""))[:1200],
            "translation_language": translation_language,
            "rationale": str(result.get("rationale", "模型未提供理由"))[:240],
        }
    except urllib.error.HTTPError as exc:
        try:
            error_payload = json.loads(exc.read(500_000))
            error_info = error_payload.get("error", {})
            error_code = str(error_info.get("code") or "").strip()
            error_message = str(error_info.get("message") or exc.reason).strip()
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            error_code = ""
            error_message = str(exc.reason)
        error_detail = f"HTTP {exc.code}"
        if error_code:
            error_detail += f" {error_code}"
        return {"verdict": "unverified", "confidence": 0, "relevance_score": 0, "urgency": "medium",
                "summary_zh": "", "relevance_rationale": "", "translation": "", "translation_language": translation_language,
                "rationale": f"AI 核验不可用：{error_detail} {error_message}"[:240]}
    except (OSError, KeyError, ValueError, TypeError, json.JSONDecodeError, urllib.error.URLError) as exc:
        return {"verdict": "unverified", "confidence": 0, "relevance_score": 0, "urgency": "medium",
            "summary_zh": "", "relevance_rationale": "", "translation": "", "translation_language": translation_language,
                "rationale": f"AI 核验不可用：{str(exc)[:120]}"}


def send_telegram(text: str, settings: dict[str, str]) -> bool:
    token = settings.get("telegram_bot_token", "")
    chat_id = settings.get("telegram_chat_id", "")
    if not token or not chat_id:
        return False
    body = urllib.parse.urlencode({"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}).encode()
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body, method="POST")
    with urllib.request.urlopen(request, timeout=15) as response:
        result = json.loads(response.read(200_000))
    return bool(result.get("ok"))


def send_email(subject: str, text: str, settings: dict[str, str]) -> bool:
    host = settings.get("smtp_host", "").strip()
    sender = settings.get("smtp_sender", "").strip()
    recipient = settings.get("smtp_recipient", "").strip()
    if not host or not sender or not recipient:
        return False
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient
    message.set_content(text)
    port = int(settings.get("smtp_port", "587"))
    username = settings.get("smtp_username", "").strip()
    password = settings.get("smtp_password", "")
    context = ssl.create_default_context()
    if settings.get("smtp_use_ssl") == "1":
        with smtplib.SMTP_SSL(host, port, timeout=20, context=context) as server:
            if username:
                server.login(username, password)
            server.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=20) as server:
            if settings.get("smtp_starttls", "1") == "1":
                server.starttls(context=context)
            if username:
                server.login(username, password)
            server.send_message(message)
    return True


async def websocket_handler(websocket: ServerConnection) -> None:
    WS_CLIENTS.add(websocket)
    try:
        await websocket.send(json.dumps({"type": "connected"}))
        async for _ in websocket:
            pass
    finally:
        WS_CLIENTS.discard(websocket)


async def websocket_server() -> None:
    global WS_LOOP
    WS_LOOP = asyncio.get_running_loop()
    app_port = int(os.environ.get("AI_HOT_MONITOR_PORT", "8765"))
    ws_port = int(os.environ.get("AI_HOT_MONITOR_WS_PORT", "8766"))
    origins = [f"http://127.0.0.1:{app_port}", f"http://localhost:{app_port}"]
    async with serve(websocket_handler, "127.0.0.1", ws_port, origins=origins, compression=None):
        await asyncio.Future()


def start_websocket_server() -> None:
    def worker() -> None:
        try:
            asyncio.run(websocket_server())
        except OSError as exc:
            SCAN_STATE.update(error=f"WebSocket 启动失败：{str(exc)[:120]}")

    threading.Thread(target=worker, daemon=True, name="hot-monitor-websocket").start()


async def broadcast_websocket(payload: dict[str, Any]) -> int:
    clients = list(WS_CLIENTS)
    if not clients:
        return 0
    message = json.dumps(payload, ensure_ascii=False)
    results = await asyncio.gather(*(client.send(message) for client in clients), return_exceptions=True)
    delivered = 0
    for client, result in zip(clients, results):
        if isinstance(result, Exception):
            WS_CLIENTS.discard(client)
        else:
            delivered += 1
    return delivered


def publish_websocket(payload: dict[str, Any]) -> int:
    loop = WS_LOOP
    if not loop or not loop.is_running():
        return 0
    try:
        return asyncio.run_coroutine_threadsafe(broadcast_websocket(payload), loop).result(timeout=4)
    except (TimeoutError, RuntimeError):
        return 0


def send_notifications(story: dict[str, Any], sources: list[dict[str, Any]], settings: dict[str, str]) -> bool:
    if story.get("verdict") != "credible" or story.get("urgency") not in {"urgent", "high"}:
        return False
    evidence = [{key: source.get(key, "") for key in ("publisher", "url", "published_at", "platform")} for source in sources]
    source_lines = "\n".join(f"- {item['publisher'] or item['platform']}: {item['url']}" for item in evidence)
    text = (
        f"热点：{story['title']}\n"
        f"时间：{story.get('published_at') or story.get('discovered_at', '')}\n"
        f"紧急度：{story.get('urgency', 'medium')}\n"
        f"相关性：{story.get('relevance_score', 0)}/100\n"
        f"摘要：{story.get('summary_zh') or story.get('summary', '')}\n"
        f"真实性判断：{story.get('verdict', 'unverified')}；{story.get('rationale', '')}\n"
        f"交叉来源：{len(sources)}\n{source_lines}"
    )
    delivered = publish_websocket({"type": "alerted_story", "story": story, "sources": evidence}) > 0
    if settings.get("smtp_host") and settings.get("smtp_sender") and settings.get("smtp_recipient"):
        try:
            delivered = send_email(f"AI 热点：{story['title'][:120]}", text, settings) or delivered
        except (OSError, smtplib.SMTPException, ValueError):
            pass
    if settings.get("telegram_bot_token") and settings.get("telegram_chat_id"):
        try:
            delivered = send_telegram(text, settings) or delivered
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            pass
    return delivered


def title_similarity(left: str, right: str) -> float:
    a, b = set(re.findall(r"[\w\u4e00-\u9fff]{2,}", left.casefold())), set(re.findall(r"[\w\u4e00-\u9fff]{2,}", right.casefold()))
    return len(a & b) / max(1, len(a | b))


def source_identity(source: dict[str, Any]) -> str:
    platform = str(source.get("platform", ""))
    external_id = str(source.get("external_id", "")).strip()
    stable_id = external_id or str(source.get("source_url") or source.get("url") or "")
    return f"{platform}:{stable_id}"


def cluster_stories(stories: list[dict[str, Any]], threshold: float = 0.5) -> list[dict[str, Any]]:
    clusters: list[dict[str, Any]] = []
    for story in stories:
        sources = story.get("sources") or [story]
        matches = [
            cluster for cluster in clusters
            if title_similarity(story["title"], cluster["title"]) >= threshold
        ]
        if not matches:
            clusters.append({**story, "sources": []})
            matches = [clusters[-1]]
        target = matches[0]
        for source in sources:
            identity = source_identity(source)
            if identity and all(source_identity(existing) != identity for existing in target["sources"]):
                target["sources"].append(source)
        if len(story.get("summary", "")) > len(target.get("summary", "")):
            for key in ("title", "summary", "url", "source", "published_at"):
                if story.get(key):
                    target[key] = story[key]
        for duplicate in matches[1:]:
            for source in duplicate["sources"]:
                identity = source_identity(source)
                if identity and all(source_identity(existing) != identity for existing in target["sources"]):
                    target["sources"].append(source)
            clusters.remove(duplicate)
    return clusters


OFFICIAL_DOMAINS = {
    "openai.com", "anthropic.com", "deepmind.google", "googleblog.com", "ai.google.dev",
    "x.ai", "meta.com", "deepseek.com", "mistral.ai", "qwen.ai", "moonshot.ai",
    "microsoft.com", "nvidia.com", "huggingface.co",
}
TRUSTED_PUBLISHER_DOMAINS = {
    "reuters.com", "apnews.com", "bbc.com", "theguardian.com", "nytimes.com", "wsj.com",
    "bloomberg.com", "ft.com", "cnbc.com", "theverge.com", "techcrunch.com", "wired.com",
    "arstechnica.com", "technologyreview.com", "scmp.com", "nikkei.com", "caixin.com",
    "yicai.com", "36kr.com",
}
OFFICIAL_X_ACCOUNTS = {"openai", "anthropicai", "googledeepmind", "xai", "deepseek_ai", "qwen"}


def publisher_domain(source: dict[str, Any]) -> str:
    stored_domain = str(source.get("publisher_domain", "")).casefold().removeprefix("www.")
    if stored_domain:
        return stored_domain
    source_url = str(source.get("source_url") or source.get("url") or "")
    domain = urllib.parse.urlparse(source_url).hostname or ""
    return domain.casefold().removeprefix("www.")


def verification_evidence(sources: list[dict[str, Any]]) -> tuple[int, bool]:
    publishers = set()
    official = False
    for source in sources:
        if source.get("platform") == "twitterapi.io":
            account = str(source.get("publisher", "")).casefold().lstrip("@")
            official = official or account in OFFICIAL_X_ACCOUNTS
            continue
        domain = publisher_domain(source)
        official = official or any(domain == trusted or domain.endswith(f".{trusted}") for trusted in OFFICIAL_DOMAINS)
        trusted = next((
            trusted_domain for trusted_domain in TRUSTED_PUBLISHER_DOMAINS
            if domain == trusted_domain or domain.endswith(f".{trusted_domain}")
        ), None)
        if trusted:
            publishers.add(trusted)
    return len(publishers), official


def apply_verification_gate(assessment: dict[str, Any], sources: list[dict[str, Any]]) -> dict[str, Any]:
    result = {
        "verdict": assessment.get("verdict", "uncertain"),
        "confidence": assessment.get("confidence", 0),
        "relevance_score": assessment.get("relevance_score", 0),
        "urgency": assessment.get("urgency", "medium"),
        "summary_zh": assessment.get("summary_zh", ""),
        "rationale": assessment.get("rationale", ""),
        **{k: v for k, v in assessment.items() if k not in {"verdict","confidence","relevance_score","urgency","summary_zh","rationale"}},
    }
    count, official = verification_evidence(sources)
    result["corroborations"] = max(1, count)
    if result.get("verdict") == "credible" and not official and count < 2:
        result["verdict"] = "uncertain"
        result["rationale"] = (str(result.get("rationale", "")) + " 目前缺少官方一手来源或第二个独立发布者。")[:240]
    return result


def wait_for_source(last_requests: dict[str, float], source: str, interval: float = 1.0) -> None:
    elapsed = time.monotonic() - last_requests.get(source, 0.0)
    if last_requests.get(source) and elapsed < interval:
        time.sleep(interval - elapsed)
    last_requests[source] = time.monotonic()


def get_enabled_keywords() -> list[str]:
    with connect_db() as db:
        return [row["phrase"] for row in db.execute(
            "SELECT phrase FROM keywords WHERE enabled=1 ORDER BY id",
        )]


def log_scan(message: str) -> None:
    text = str(message)
    SCAN_STATE["progress"] = text
    print(text, flush=True)
    try:
        SCAN_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with SCAN_LOG_LOCK:
            with SCAN_LOG_PATH.open("a", encoding="utf-8") as handle:
                handle.write(f"{datetime.now(timezone.utc).isoformat()} {text}\n")
    except OSError:
        pass


def save_story_source(db: sqlite3.Connection, story_id: int, source: dict[str, Any], discovered_at: str) -> bool:
    source_url = str(source.get("url", ""))
    platform = str(source.get("platform", "unknown"))
    external_id = str(source.get("external_id") or source.get("source_url") or source_url)
    if not external_id:
        external_id = hashlib.sha256(str(source.get("title", "")).encode()).hexdigest()
    publisher = str(source.get("publisher") or source.get("source") or "")
    source_fields = {
        "region": str(source.get("region", "")),
        "language": str(source.get("language", "")),
        "publisher": publisher,
        "publisher_domain": publisher_domain(source),
        "title": str(source.get("title", "")),
        "summary": str(source.get("summary", "")),
        "url": source_url,
        "published_at": str(source.get("published_at", "")),
        "content_type": str(source.get("content_type", "")),
    }
    previous = db.execute("""SELECT region,language,publisher,publisher_domain,title,summary,url,published_at,content_type
        FROM story_sources WHERE platform=? AND external_id=?""", (platform, external_id)).fetchone()
    assessment_changed = previous is None or any(
        value and previous[key] != value for key, value in source_fields.items()
    )
    cursor = db.execute("""INSERT INTO story_sources
        (story_id,platform,external_id,region,language,publisher,publisher_domain,title,summary,url,published_at,discovered_at,
         like_count,retweet_count,reply_count,view_count,author_followers,author_verified,content_type)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(platform,external_id) DO UPDATE SET
            region=COALESCE(NULLIF(excluded.region,''),story_sources.region),
            language=COALESCE(NULLIF(excluded.language,''),story_sources.language),
            publisher=COALESCE(NULLIF(excluded.publisher,''),story_sources.publisher),
            publisher_domain=COALESCE(NULLIF(excluded.publisher_domain,''),story_sources.publisher_domain),
            title=COALESCE(NULLIF(excluded.title,''),story_sources.title),
            summary=COALESCE(NULLIF(excluded.summary,''),story_sources.summary),
            url=COALESCE(NULLIF(excluded.url,''),story_sources.url),
            published_at=COALESCE(NULLIF(excluded.published_at,''),story_sources.published_at),
            like_count=COALESCE(excluded.like_count,story_sources.like_count),
            retweet_count=COALESCE(excluded.retweet_count,story_sources.retweet_count),
            reply_count=COALESCE(excluded.reply_count,story_sources.reply_count),
            view_count=COALESCE(excluded.view_count,story_sources.view_count),
            author_followers=COALESCE(excluded.author_followers,story_sources.author_followers),
            author_verified=MAX(excluded.author_verified,story_sources.author_verified),
            content_type=COALESCE(NULLIF(excluded.content_type,''),story_sources.content_type)
        WHERE (excluded.region<>'' AND story_sources.region IS NOT excluded.region)
           OR (excluded.language<>'' AND story_sources.language IS NOT excluded.language)
           OR (excluded.publisher<>'' AND story_sources.publisher IS NOT excluded.publisher)
           OR (excluded.publisher_domain<>'' AND story_sources.publisher_domain IS NOT excluded.publisher_domain)
           OR (excluded.title<>'' AND story_sources.title IS NOT excluded.title)
           OR (excluded.summary<>'' AND story_sources.summary IS NOT excluded.summary)
           OR (excluded.url<>'' AND story_sources.url IS NOT excluded.url)
           OR (excluded.published_at<>'' AND story_sources.published_at IS NOT excluded.published_at)
           OR (excluded.like_count IS NOT NULL AND story_sources.like_count IS NOT excluded.like_count)
           OR (excluded.retweet_count IS NOT NULL AND story_sources.retweet_count IS NOT excluded.retweet_count)
           OR (excluded.reply_count IS NOT NULL AND story_sources.reply_count IS NOT excluded.reply_count)
           OR (excluded.view_count IS NOT NULL AND story_sources.view_count IS NOT excluded.view_count)
           OR (excluded.author_followers IS NOT NULL AND story_sources.author_followers IS NOT excluded.author_followers)
           OR (excluded.author_verified=1 AND story_sources.author_verified=0)
           OR (excluded.content_type<>'' AND story_sources.content_type IS NOT excluded.content_type)""", (
        story_id, platform, external_id, str(source.get("region", "")), str(source.get("language", "")),
        publisher, source_fields["publisher_domain"], source_fields["title"], source_fields["summary"],
        source_url, str(source.get("published_at", "")), discovered_at, source.get("like_count"),
        source.get("retweet_count"), source.get("reply_count"), source.get("view_count"),
        source.get("author_followers"), int(bool(source.get("author_verified"))),
        str(source.get("content_type", "")),
    ))
    return assessment_changed


def run_scan() -> None:
    started = datetime.now(timezone.utc).isoformat()
    SCAN_STATE.update(running=True, error=None, last_started=started, progress="准备扫描")
    settings = get_settings()
    queries = list(dict.fromkeys(get_enabled_keywords()))
    scope = ", ".join(queries) or "manual"
    items: list[dict[str, Any]] = []
    errors = []
    translation_warning = False
    last_source_requests: dict[str, float] = {}
    hacker_news_queries: set[str] = set()
    twitter_key = settings.get("twitter_api_key", "")
    source_counts = {
        "Google News RSS": 0,
        "Bing RSS": 0,
        "DuckDuckGo": 0,
        "Hacker News": 0,
        "Twitter": 0,
    }
    twitter_eligible_count = 0
    log_scan(f"\n[scan] Starting with {len(queries)} enabled keyword(s): {', '.join(queries) or '(none)'}")

    for query in queries:
        account_identifier = parse_account_identifier(query)
        if account_identifier and account_identifier[0] != "x":
            platform = "Bilibili" if account_identifier[0] == "bilibili" else "微博"
            message = f"{platform}账号直查尚未启用；当前未验证到合规可用的公开接口"
            errors.append(message)
            log_scan(f"[skip] {query}: {message}")
            continue
        english_query = translate_query_to_english(query, settings)
        expansions = expand_search_query(query, settings)
        if expansions:
            log_scan(f"[query expand] {query} -> {', '.join(expansions)}")
        if re.search(r"[\u4e00-\u9fff]", query) and not english_query:
            translation_warning = True
        for region, regional_query in (("china", query), ("global", english_query or query)):
            providers = (
                ("Google News RSS", lambda: fetch_feed(regional_query, region)),
                ("Bing RSS", lambda: fetch_bing_search(regional_query)),
                ("DuckDuckGo", lambda: fetch_duckduckgo_search(regional_query, region)),
            )
            for provider, fetch in providers:
                wait_for_source(last_source_requests, provider)
                try:
                    results = fetch()
                    source_counts[provider] += len(results)
                    log_scan(f'{provider} search for "{regional_query}" ({region}): found {len(results)} results')
                    for source in results:
                        source["matched_keyword"] = query
                        source["query_expansions"] = expansions
                        items.append({**source, "sources": [source]})
                except (OSError, ValueError, ET.ParseError, urllib.error.URLError) as exc:
                    message = f"{provider} / {query}: {str(exc)[:100]}"
                    errors.append(message)
                    log_scan(f"[error] {message}")

        hn_query = english_query or (query if not re.search(r"[\u4e00-\u9fff]", query) else "")
        if hn_query and hn_query not in hacker_news_queries:
            wait_for_source(last_source_requests, "Hacker News", 0.5)
            try:
                hn_results = fetch_hacker_news_search(hn_query)
                source_counts["Hacker News"] += len(hn_results)
                log_scan(f'Hacker News search for "{hn_query}": found {len(hn_results)} results')
                for source in hn_results:
                    source["matched_keyword"] = query
                    source["query_expansions"] = expansions
                    items.append({**source, "sources": [source]})
            except (OSError, ValueError, urllib.error.URLError) as exc:
                message = f"Hacker News / {query}: {str(exc)[:100]}"
                errors.append(message)
                log_scan(f"[error] {message}")
            hacker_news_queries.add(hn_query)

        if twitter_key:
            twitter_query = f"{query} OR {english_query}" if english_query else query
            try:
                if account_identifier:
                    x_sources = fetch_twitter_account(account_identifier[1], account_identifier[2], twitter_key)
                else:
                    x_sources = fetch_twitter_search(twitter_query, twitter_key)
                source_counts["Twitter"] += len(x_sources)
                eligible_count = sum(twitter_source_is_eligible(source) for source in x_sources)
                twitter_eligible_count += eligible_count
                log_scan(
                    f'Twitter: {len(x_sources)} unique posts for "{query}" -> {eligible_count} quality-eligible '
                    "(original posts; likes>=10, retweets>=5, views>=500, followers>=100)"
                )
                for source in x_sources:
                    source["matched_keyword"] = query
                    source["query_expansions"] = expansions
                    items.append({**source, "sources": [source]})
            except (OSError, ValueError, urllib.error.URLError) as exc:
                message = f"X / {query}: {str(exc)[:100]}"
                errors.append(message)
                log_scan(f"[error] {message}")

    if not twitter_key:
        message = "TwitterAPI.io 未配置；本次跳过 X 搜索"
        errors.append(message)
        log_scan(f"[skip] {message}")
    if translation_warning:
        message = "部分中文查询未能生成英文检索词，国际覆盖使用原词搜索"
        errors.append(message)
        log_scan(f"[warning] {message}")

    clusters = cluster_stories(items)
    log_scan(f"[results] {len(items)} raw -> {len(clusters)} title clusters; X quality-eligible: {twitter_eligible_count}")
    if len(clusters) > 30:
        log_scan(f"[limit] analyzing first 30 of {len(clusters)} clusters")
        clusters = clusters[:30]
    found_count = 0
    existing_count = 0
    for index, story in enumerate(clusters, 1):
        SCAN_STATE.update(progress=f"AI 分析 {index}/{len(clusters)}: {str(story.get('title', ''))[:80]}")
        log_scan(f"[assess {index}/{len(clusters)}] {str(story.get('title', ''))[:120]}")
        normalized = normalize_title(story["title"])
        if not normalized:
            continue
        fingerprint = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        now = datetime.now(timezone.utc).isoformat()
        is_new_story = False
        with connect_db() as db:
            existing = db.execute("""SELECT id,notified,verdict,confidence,relevance_score,urgency,summary_zh,
                translation,translation_language,rationale,relevance_rationale FROM stories WHERE fingerprint=?""",
                (fingerprint,)).fetchone()
            if existing:
                story_id = existing["id"]
                was_notified = bool(existing["notified"])
                existing_count += 1
            else:
                db.execute("""INSERT OR IGNORE INTO stories
                    (fingerprint,title,summary,url,source,published_at,matched_keyword,verdict,confidence,relevance_score,urgency,summary_zh,rationale,corroborations,discovered_at,notified)
                    VALUES(?,?,?,?,?,?,?,'pending',0,0,'medium','','',1,?,0)""", (
                    fingerprint, story["title"], story.get("summary", ""), story.get("url", ""),
                    story.get("source", ""), story.get("published_at", ""), story.get("matched_keyword", ""), now,
                ))
                saved_story = db.execute("SELECT id,notified FROM stories WHERE fingerprint=?", (fingerprint,)).fetchone()
                story_id = saved_story["id"]
                was_notified = bool(saved_story["notified"])
                found_count += 1
                is_new_story = True

            source_changed = False
            for source in story["sources"]:
                source_changed = save_story_source(db, story_id, source, now) or source_changed
            evidence = [dict(row) for row in db.execute(
                "SELECT platform,external_id,region,language,publisher,publisher_domain,title,summary,url,published_at, "
                "like_count,retweet_count,reply_count,view_count,author_followers,author_verified,content_type "
                "FROM story_sources WHERE story_id=?",
                (story_id,),
            )]

        corroborations, official = verification_evidence(evidence)
        story["sources"] = evidence
        if existing and not source_changed and existing["relevance_rationale"]:
            assessment = {key: existing[key] for key in (
                "verdict", "confidence", "relevance_score", "urgency", "summary_zh",
                "translation", "translation_language", "rationale", "relevance_rationale",
            )}
        else:
            assessment = assess_story(story, corroborations, settings)
        evidence = [source for source in evidence if not is_x_source(source) or twitter_source_is_eligible(source)]
        corroborations, _ = verification_evidence(evidence)
        assessment = apply_verification_gate(assessment, evidence)
        story.update(assessment)
        should_notify = (
            assessment.get("urgency") in {"urgent", "high"}
            and assessment.get("verdict") == "credible"
            and bool(evidence)
            and not was_notified
        )
        with connect_db() as db:
            db.execute("""UPDATE stories SET verdict=?,confidence=?,relevance_score=?,urgency=?,summary_zh=?,
                translation=?,translation_language=?,rationale=?,relevance_rationale=?,corroborations=? WHERE id=?""", (
                assessment.get("verdict", "uncertain"), assessment.get("confidence", 0), assessment.get("relevance_score", 0),
                assessment.get("urgency", "medium"), assessment.get("summary_zh", ""), assessment.get("translation", ""),
                assessment.get("translation_language", ""), assessment.get("rationale", ""),
                assessment.get("relevance_rationale", ""), corroborations, story_id,
            ))
        if is_new_story:
            title = re.sub(r"\s+", " ", str(story.get("title", ""))).strip()
            if len(title) > 160:
                title = title[:157] + "..."
            log_scan(f"[new hotspot] [{assessment.get('urgency', 'medium')}] {title} ({assessment.get('verdict', 'uncertain')})")
        if should_notify:
            notified = int(send_notifications(story, evidence, settings))
            if notified:
                with connect_db() as db:
                    db.execute("UPDATE stories SET notified=1 WHERE id=?", (story_id,))

    finished = datetime.now(timezone.utc).isoformat()
    status = "error" if errors and not items else "partial" if errors else "ok"
    with connect_db() as db:
        db.execute("INSERT INTO scans(scope,started_at,finished_at,found_count,status) VALUES(?,?,?,?,?)",
                   (scope, started, finished, found_count, status))
    SCAN_STATE.update(running=False, error="; ".join(errors[:3]) if errors else None)
    providers_summary = " | ".join(f"{provider}: {count}" for provider, count in source_counts.items())
    log_scan(
        f"[complete] Total: {len(items)} raw -> {len(clusters)} unique -> {found_count} new "
        f"({existing_count} already stored); {status}. {providers_summary}"
    )


def start_scan() -> bool:
    if not SCAN_LOCK.acquire(blocking=False):
        return False

    def worker() -> None:
        try:
            run_scan()
        except Exception as exc:
            SCAN_STATE.update(running=False, error=str(exc)[:240], progress=f"扫描失败：{str(exc)[:120]}")
            log_scan(f"[scan error] {str(exc)[:240]}")
        finally:
            try:
                SCAN_LOCK.release()
            except RuntimeError:
                pass
            try:
                publish_websocket({"type": "scan_completed"})
            except Exception:
                pass

    threading.Thread(target=worker, daemon=True, name="hot-monitor-scan").start()
    return True


STORY_SORTS = {"recent", "discovered", "relevance", "importance", "hot"}
URGENCY_RANK = {"urgent": 4, "high": 3, "medium": 2, "low": 1}


def _story_timestamp(value: Any) -> float:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0


def calculate_hot_score(story: dict[str, Any]) -> float:
    score = 0.0
    for source in story.get("sources", []):
        def metric(name: str) -> int:
            try:
                return max(0, int(source.get(name) or 0))
            except (TypeError, ValueError, OverflowError):
                return 0

        views = metric("view_count")
        score += metric("like_count") + metric("retweet_count") * 2 + metric("reply_count")
        score += math.log10(max(views, 1)) * 10
    return score


def sort_stories(stories: list[dict[str, Any]], sort_by: str = "recent") -> list[dict[str, Any]]:
    if sort_by not in STORY_SORTS:
        sort_by = "recent"

    def key(story: dict[str, Any]) -> tuple[float, ...]:
        discovered = _story_timestamp(story.get("discovered_at"))
        published = _story_timestamp(story.get("published_at")) or discovered
        relevance = float(story.get("relevance_score") or 0)
        importance = URGENCY_RANK.get(str(story.get("urgency", "medium")).lower(), 0)
        hot = calculate_hot_score(story)
        try:
            story_id = int(story.get("id") or 0)
        except (TypeError, ValueError, OverflowError):
            story_id = 0

        if sort_by == "relevance":
            return (-relevance, -published, -discovered, -story_id)
        if sort_by == "importance":
            return (-importance, -relevance, -discovered, -story_id)
        if sort_by == "hot":
            return (-hot, -discovered, -story_id)
        if sort_by == "discovered":
            return (-discovered, -story_id)
        return (-published, -discovered, -story_id)

    return sorted(stories, key=key)


def dashboard_state(
    limit: int = 500,
    sort_by: str = "recent",
    page: int = 1,
    urgency: str = "all",
    source_platform: str = "all",
    minimum_relevance: int = 0,
) -> dict[str, Any]:
    limit = max(1, min(500, int(limit)))
    page = max(1, int(page))
    sort_by = sort_by if sort_by in STORY_SORTS else "recent"
    urgency = urgency if urgency in {*URGENCY_RANK, "all"} else "all"
    try:
        minimum_relevance = max(0, min(100, int(minimum_relevance)))
    except (TypeError, ValueError, OverflowError):
        minimum_relevance = 0
    settings = get_settings()
    with connect_db() as db:
        keywords = [dict(row) for row in db.execute("SELECT id,phrase,created_at,enabled FROM keywords ORDER BY id DESC")]
        stories = [dict(row) for row in db.execute("SELECT * FROM stories")]
        source_rows = [dict(row) for row in db.execute(
            "SELECT story_id,platform,external_id,region,language,publisher,publisher_domain,title,summary,url,published_at,"
            "like_count,retweet_count,reply_count,view_count,author_followers,author_verified,content_type "
            "FROM story_sources ORDER BY id",
        )]
        scans = [dict(row) for row in db.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 12")]
        total_story_count = db.execute("SELECT COUNT(*) FROM stories").fetchone()[0]
        totals = {row["verdict"]: row["count"] for row in db.execute("SELECT verdict,COUNT(*) AS count FROM stories GROUP BY verdict")}
        urgency_totals = {row["urgency"]: row["count"] for row in db.execute(
            "SELECT urgency,COUNT(*) AS count FROM stories GROUP BY urgency",
        )}
        source_options = [row["platform"] for row in db.execute(
            "SELECT DISTINCT platform FROM story_sources ORDER BY platform",
        )]
        last_scan = db.execute("SELECT finished_at FROM scans ORDER BY id DESC LIMIT 1").fetchone()
    public_settings = {key: settings[key] for key in ("scope", "interval_minutes", "ai_base_url", "ai_model")}
    sources_by_story: dict[int, list[dict[str, Any]]] = {}
    for source in source_rows:
        sources_by_story.setdefault(source["story_id"], []).append(source)
    visible_stories = []
    for story in stories:
        all_sources = sources_by_story.get(story["id"], [])
        current_source_urls = {
            str(source.get("url", "")).rstrip("/")
            for source in all_sources if source.get("platform") != "legacy" and source.get("url")
        }
        all_sources = [source for source in all_sources if not (
            source.get("platform") == "legacy" and str(source.get("url", "")).rstrip("/") in current_source_urls
        )]
        for source in all_sources:
            source["is_x_source"] = is_x_source(source)
            source["quality_eligible"] = not source["is_x_source"] or twitter_source_is_eligible(source)
            has_complete_metrics = all(source.get(field) is not None for field in (
                "like_count", "retweet_count", "view_count", "author_followers",
            )) and bool(source.get("content_type"))
            source["quality_status"] = (
                "not_applicable" if not source["is_x_source"] else
                "unknown" if not has_complete_metrics else
                "eligible" if source["quality_eligible"] else "ineligible"
            )
        eligible_sources = [source for source in all_sources if source["quality_eligible"]]
        story["sources"] = all_sources
        story["source_quality_warning"] = any(
            source["quality_status"] == "ineligible" for source in all_sources
        )
        story["relevance_unavailable"] = str(story.get("rationale", "")).startswith((
            "AI 核验不可用", "未配置 AI 核验", "未配置 AI API Key", "未配置 OpenRouter API Key",
        ))
        primary_is_x = is_x_source({"url": story.get("url", ""), "platform": ""}) or str(
            story.get("source", "")
        ).casefold().startswith("x @")
        has_eligible_primary = any(
            is_x_source(source) and twitter_source_is_eligible(source)
            and source.get("url") == story.get("url") for source in eligible_sources
        )
        if primary_is_x and not has_eligible_primary:
            replacement = next((source for source in eligible_sources if not is_x_source(source)), None)
            if replacement:
                story.update({
                    "title": replacement.get("title") or story.get("title", ""),
                    "summary": replacement.get("summary") or "",
                    "summary_zh": "",
                    "url": replacement.get("url") or "",
                    "source": replacement.get("publisher") or replacement.get("platform") or "未知来源",
                    "published_at": replacement.get("published_at") or story.get("published_at", ""),
                })
        visible_stories.append(story)
    ai_configured = bool(
        settings.get("ai_base_url") and settings.get("ai_model")
        and (not ai_api_key_required(settings.get("ai_base_url", "")) or settings.get("ai_api_key"))
    )
    filtered_stories = [story for story in visible_stories if (
        (urgency == "all" or story.get("urgency", "medium") == urgency)
        and (source_platform == "all" or any(item.get("platform") == source_platform for item in story["sources"]))
        and (not minimum_relevance or not ai_configured or story.get("verdict") == "unverified"
             or int(story.get("relevance_score") or 0) >= minimum_relevance)
    )]
    stories = sort_stories(filtered_stories, sort_by)
    filtered_story_count = len(stories)
    start = (page - 1) * limit
    stories = stories[start:start + limit]
    public_settings.update({
        "ai_configured": ai_configured,
        "ai_key_saved": bool(settings.get("ai_api_key")),
        "twitter_configured": bool(settings.get("twitter_api_key")),
        "smtp_configured": bool(settings.get("smtp_host") and settings.get("smtp_sender") and settings.get("smtp_recipient")),
        "smtp_password_saved": bool(settings.get("smtp_password")),
        "smtp_host": settings.get("smtp_host", ""),
        "smtp_port": settings.get("smtp_port", "587"),
        "smtp_username": settings.get("smtp_username", ""),
        "smtp_sender": settings.get("smtp_sender", ""),
        "smtp_recipient": settings.get("smtp_recipient", ""),
        "smtp_starttls": settings.get("smtp_starttls", "1") == "1",
        "smtp_use_ssl": settings.get("smtp_use_ssl", "0") == "1",
        "telegram_configured": bool(settings.get("telegram_bot_token") and settings.get("telegram_chat_id")),
    })
    return {"keywords": keywords, "stories": stories, "scans": scans, "totals": totals,
            "urgency_totals": urgency_totals, "source_options": source_options,
            "total_story_count": total_story_count,
            "filtered_story_count": filtered_story_count, "page": page, "page_size": limit,
            "has_more": start + len(stories) < filtered_story_count,
            "last_scan": last_scan["finished_at"] if last_scan else None,
            "scan_state": dict(SCAN_STATE), "settings": public_settings}


class Handler(BaseHTTPRequestHandler):
    server_version = "AIHotMonitor/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def _json(self, status: int, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict[str, Any]:
        length = min(int(self.headers.get("Content-Length", 0)), 100_000)
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self) -> None:
        parsed_path = urllib.parse.urlparse(self.path)
        path = parsed_path.path
        if path == "/api/state":
            query = urllib.parse.parse_qs(parsed_path.query)
            requested_limit = query.get("pageSize", query.get("limit", ["500"]))[0]
            try:
                limit = int(requested_limit)
            except ValueError:
                limit = 500
            try:
                page = int(query.get("page", ["1"])[0])
            except ValueError:
                page = 1
            try:
                minimum_relevance = int(query.get("relevance", ["0"])[0])
            except ValueError:
                minimum_relevance = 0
            return self._json(200, dashboard_state(
                limit=limit, sort_by=query.get("sort", ["recent"])[0], page=page,
                urgency=query.get("urgency", ["all"])[0], source_platform=query.get("source", ["all"])[0],
                minimum_relevance=minimum_relevance,
            ))
        if path == "/api/health":
            return self._json(200, {"ok": True})
        target = WEB_DIR / ("index.html" if path == "/" else path.lstrip("/"))
        try:
            target = target.resolve()
            if not target.is_relative_to(WEB_DIR.resolve()) or not target.is_file():
                raise FileNotFoundError
            data = target.read_bytes()
            content_type = "text/html; charset=utf-8" if target.suffix == ".html" else "text/css; charset=utf-8" if target.suffix == ".css" else "text/javascript; charset=utf-8" if target.suffix == ".js" else "image/svg+xml" if target.suffix == ".svg" else "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (OSError, ValueError):
            self.send_error(404)

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        try:
            body = self._body()
            if path == "/api/keywords":
                phrase = str(body.get("phrase", "")).strip()[:100]
                if len(phrase) < 2:
                    return self._json(400, {"error": "关键词至少需要 2 个字符"})
                with connect_db() as db:
                    db.execute("INSERT OR IGNORE INTO keywords(phrase,created_at) VALUES(?,?)", (phrase, datetime.now(timezone.utc).isoformat()))
                return self._json(201, dashboard_state())
            if path == "/api/scan":
                if not get_enabled_keywords():
                    return self._json(400, {"error": "请先启用至少一个监控词"})
                if not start_scan():
                    return self._json(409, {"error": "扫描正在进行"})
                return self._json(202, {"started": True})
            if path == "/api/settings":
                save_settings(body)
                return self._json(200, dashboard_state())
            if path == "/api/notifications/test-email":
                settings = get_settings()
                if not settings.get("smtp_host") or not settings.get("smtp_sender") or not settings.get("smtp_recipient"):
                    return self._json(400, {"error": "请先填写 SMTP 主机、发件人和收件人"})
                try:
                    ok = send_email("AI 热点监控测试邮件", "SMTP 连通性验证成功。", settings)
                except (OSError, smtplib.SMTPException, ValueError) as exc:
                    return self._json(502, {"error": f"发送失败：{str(exc)[:180]}"})
                return self._json(200 if ok else 502, {"ok": ok, "error": None if ok else "SMTP 未确认发送"})
            if path == "/api/notifications/test":
                settings = get_settings()
                if not settings.get("telegram_bot_token") or not settings.get("telegram_chat_id"):
                    return self._json(400, {"error": "请先填写 Telegram Bot Token 和 Chat ID"})
                try:
                    ok = send_telegram("AI 热点监控已连通。验证提醒发送成功。", settings)
                except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
                    return self._json(502, {"error": f"发送失败：{str(exc)[:180]}"})
                return self._json(200 if ok else 502, {"ok": ok, "error": None if ok else "Telegram 未确认发送"})
        except (json.JSONDecodeError, ValueError) as exc:
            return self._json(400, {"error": f"请求格式无效：{str(exc)[:100]}"})
        except sqlite3.IntegrityError:
            return self._json(409, {"error": "该关键词已添加"})
        return self._json(404, {"error": "接口不存在"})

    def do_DELETE(self) -> None:
        match = re.fullmatch(r"/api/keywords/(\d+)", urllib.parse.urlparse(self.path).path)
        if not match:
            return self._json(404, {"error": "接口不存在"})
        with connect_db() as db:
            db.execute("DELETE FROM keywords WHERE id=?", (int(match.group(1)),))
        self._json(200, dashboard_state())

    def do_PATCH(self) -> None:
        match = re.fullmatch(r"/api/keywords/(\d+)", urllib.parse.urlparse(self.path).path)
        if not match:
            return self._json(404, {"error": "接口不存在"})
        try:
            body = self._body()
        except (json.JSONDecodeError, ValueError) as exc:
            return self._json(400, {"error": f"请求格式无效：{str(exc)[:100]}"})
        if not isinstance(body.get("enabled"), bool):
            return self._json(400, {"error": "enabled 必须为布尔值"})
        with connect_db() as db:
            result = db.execute("UPDATE keywords SET enabled=? WHERE id=?",
                                (int(body["enabled"]), int(match.group(1))))
            if not result.rowcount:
                return self._json(404, {"error": "监控词不存在"})
        self._json(200, dashboard_state())


def main() -> None:
    init_db()
    start_websocket_server()
    host = os.environ.get("AI_HOT_MONITOR_HOST", "127.0.0.1")
    port = int(os.environ.get("AI_HOT_MONITOR_PORT", "8765"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"AI 热点监控已启动：http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭服务…")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
