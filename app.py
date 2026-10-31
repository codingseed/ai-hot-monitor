from __future__ import annotations

import asyncio
from contextlib import contextmanager
import hashlib
import html
import json
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
    "ai_base_url": "https://openrouter.ai/api/v1",
    "ai_model": "deepseek/deepseek-v4.1-flash",
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
    "ai_base_url": "OPENROUTER_BASE_URL",
    "ai_model": "OPENROUTER_MODEL",
    "ai_api_key": "OPENROUTER_API_KEY",
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
SCAN_LOCK = threading.Lock()
SCAN_STATE: dict[str, Any] = {"running": False, "error": None, "last_started": None}
WS_CLIENTS: set[ServerConnection] = set()
WS_LOOP: asyncio.AbstractEventLoop | None = None


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def clean_html(value: str) -> str:
    parser = TextExtractor()
    parser.feed(html.unescape(value or ""))
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()


def normalize_title(title: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", title.casefold())[:240]


def parse_feed(payload: bytes) -> list[dict[str, str]]:
    root = ET.fromstring(payload)
    results = []
    for item in root.findall(".//item"):
        title = clean_html(item.findtext("title", ""))
        link = item.findtext("link", "").strip()
        if not title or not link:
            continue
        source = item.find("source")
        publisher = clean_html(source.text or "") if source is not None else "Google News"
        results.append({
            "title": title,
            "url": link,
            "summary": clean_html(item.findtext("description", ""))[:1600],
            "source": publisher,
            "publisher": publisher,
            "source_url": source.attrib.get("url", "") if source is not None else "",
            "published_at": item.findtext("pubDate", ""),
            "platform": "Google News",
        })
    return results


def parse_twitter_search(payload: dict[str, Any]) -> list[dict[str, str]]:
    results = []
    for item in payload.get("tweets", []):
        text = str(item.get("text", "")).strip()
        external_id = str(item.get("id", "")).strip()
        if not text or not external_id:
            continue
        author = item.get("author") if isinstance(item.get("author"), dict) else {}
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
        })
    return results


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
                created_at TEXT NOT NULL
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
                rationale TEXT NOT NULL DEFAULT '',
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
                UNIQUE(platform, external_id)
            );
            CREATE TABLE IF NOT EXISTS query_translations (
                original TEXT PRIMARY KEY,
                english TEXT NOT NULL,
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
        ):
            if column not in story_columns:
                db.execute(f"ALTER TABLE stories ADD COLUMN {column} {declaration}")
        db.execute("""INSERT OR IGNORE INTO story_sources
            (story_id,platform,external_id,publisher,title,summary,url,published_at,discovered_at)
            SELECT id,'legacy','legacy:' || id,source,title,summary,url,published_at,discovered_at FROM stories""")
        source_columns = {row["name"] for row in db.execute("PRAGMA table_info(story_sources)")}
        for column in ("region", "language"):
            if column not in source_columns:
                db.execute(f"ALTER TABLE story_sources ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")


def get_settings() -> dict[str, str]:
    with connect_db() as db:
        values = {row["key"]: row["value"] for row in db.execute("SELECT key, value FROM settings")}
    settings = {**DEFAULTS, **values}
    for setting, environment_name in ENV_SETTINGS.items():
        value = os.environ.get(environment_name, "").strip()
        if value:
            settings[setting] = value
    return settings


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


def fetch_twitter_search(query: str, api_key: str) -> list[dict[str, str]]:
    if not api_key:
        return []
    params = urllib.parse.urlencode({"query": query, "queryType": "Latest", "cursor": ""})
    request = urllib.request.Request(
        f"https://api.twitterapi.io/twitter/tweet/advanced_search?{params}",
        headers={"X-API-Key": api_key, "User-Agent": "AIHotMonitor/1.0"},
    )
    with urllib.request.urlopen(request, timeout=18) as response:
        payload = json.loads(response.read(1_000_000))
    return parse_twitter_search(payload)


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
    if not base_url or not model or ("openrouter.ai" in base_url and not api_key):
        return ""

    schema = {
        "type": "object",
        "properties": {"english_query": {"type": "string"}},
        "required": ["english_query"],
        "additionalProperties": False,
    }
    body = json.dumps({
        "model": model,
        "temperature": 0,
        "max_tokens": 96,
        "messages": [
            {"role": "system", "content": "Translate search terms into concise natural English keywords. Return only the translated query."},
            {"role": "user", "content": query},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "search_query", "strict": True, "schema": schema},
        },
        "provider": {"require_parameters": True},
    }).encode("utf-8")
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


def assess_story(story: dict[str, Any], corroborations: int, settings: dict[str, str]) -> dict[str, Any]:
    base_url = settings.get("ai_base_url", "").rstrip("/")
    model = settings.get("ai_model", "").strip()
    if not base_url or not model:
        return {"verdict": "unverified", "confidence": 0, "relevance_score": 0, "urgency": "medium",
            "summary_zh": "", "rationale": "未配置 AI 核验模型。"}
    if "openrouter.ai" in base_url and not settings.get("ai_api_key", "").strip():
        return {"verdict": "unverified", "confidence": 0, "relevance_score": 0, "urgency": "medium",
            "summary_zh": "", "rationale": "未配置 OpenRouter API Key。"}

    evidence = story.get("sources", [])
    evidence_text = "\n".join(
        f"- {source.get('publisher') or source.get('platform')}: {source.get('url', '')}"
        for source in evidence
    )
    prompt = (
        "你是热点分析助手。真实性判断、关键词相关性与紧急度是三个独立维度，不要用真实性代替紧急度。"
        "真实性谨慎识别冒充官方、旧闻翻炒、夸张标题和无来源消息，证据不足时返回 uncertain。"
        "relevance_score 是与关注关键词的匹配度，范围0-100；urgency 根据时效影响选择 urgent/high/medium/low。"
        "summary_zh 用简洁中文概括消息。只根据来源证据，不要把推测说成事实。\n"
        f"标题：{story['title']}\n摘要：{story['summary']}\n发布源：{story['source']}\n"
        f"独立发布者数：{corroborations}\n链接域名：{urllib.parse.urlparse(story['url']).netloc}\n"
        f"来源证据：\n{evidence_text}"
    )
    body = json.dumps({
        "model": model,
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": "你负责谨慎核验新闻来源与证据。缺乏证据时必须判为 uncertain。"},
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
                        "urgency": {"type": "string", "enum": ["urgent", "high", "medium", "low"]},
                        "summary_zh": {"type": "string"},
                        "rationale": {"type": "string"},
                        "evidence_gaps": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["verdict", "confidence", "relevance_score", "urgency", "summary_zh", "rationale", "evidence_gaps"],
                    "additionalProperties": False,
                },
            },
        },
        "provider": {"require_parameters": True},
    }).encode("utf-8")
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
            "rationale": str(result.get("rationale", "模型未提供理由"))[:240],
        }
    except (OSError, KeyError, ValueError, TypeError, json.JSONDecodeError, urllib.error.URLError) as exc:
        return {"verdict": "unverified", "confidence": 0, "relevance_score": 0, "urgency": "medium",
                "summary_zh": "", "rationale": f"AI 核验不可用：{str(exc)[:120]}"}


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


def run_scan() -> None:
    started = datetime.now(timezone.utc).isoformat()
    SCAN_STATE.update(running=True, error=None, last_started=started)
    settings = get_settings()
    scope = settings.get("scope", DEFAULTS["scope"])
    with connect_db() as db:
        keywords = [row["phrase"] for row in db.execute("SELECT phrase FROM keywords ORDER BY id")]
    queries = list(dict.fromkeys([*keywords, scope]))
    items: list[dict[str, Any]] = []
    errors = []
    translation_warning = False
    last_twitter_request = 0.0
    twitter_key = settings.get("twitter_api_key", "")

    for query in queries:
        english_query = translate_query_to_english(query, settings)
        if re.search(r"[\u4e00-\u9fff]", query) and not english_query:
            translation_warning = True
        for region, regional_query in (("china", query), ("global", english_query or query)):
            try:
                for source in fetch_feed(regional_query, region):
                    source["matched_keyword"] = query
                    items.append({**source, "sources": [source]})
            except (OSError, ET.ParseError, urllib.error.URLError) as exc:
                errors.append(f"{region} RSS / {query}: {str(exc)[:100]}")

        if twitter_key:
            twitter_query = f"{query} OR {english_query}" if english_query else query
            delay = 5.0 - (time.monotonic() - last_twitter_request)
            if last_twitter_request and delay > 0:
                time.sleep(delay)
            try:
                for source in fetch_twitter_search(twitter_query, twitter_key):
                    source["matched_keyword"] = query
                    items.append({**source, "sources": [source]})
            except (OSError, ValueError, urllib.error.URLError) as exc:
                errors.append(f"X / {query}: {str(exc)[:100]}")
            last_twitter_request = time.monotonic()

    if not twitter_key:
        errors.append("TwitterAPI.io 未配置；本次跳过 X 搜索")
    if translation_warning:
        errors.append("部分中文查询未能生成英文检索词，国际覆盖使用原词搜索")

    clusters = cluster_stories(items)
    found_count = 0
    for story in clusters:
        normalized = normalize_title(story["title"])
        if not normalized:
            continue
        fingerprint = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        now = datetime.now(timezone.utc).isoformat()
        with connect_db() as db:
            existing = db.execute("SELECT id,notified FROM stories WHERE fingerprint=?", (fingerprint,)).fetchone()
            if existing:
                story_id = existing["id"]
                was_notified = bool(existing["notified"])
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

            for source in story["sources"]:
                source_url = str(source.get("url", ""))
                platform = str(source.get("platform", "unknown"))
                external_id = str(source.get("external_id") or source.get("source_url") or source_url)
                if not external_id:
                    external_id = hashlib.sha256(str(source.get("title", "")).encode()).hexdigest()
                publisher = str(source.get("publisher") or source.get("source") or "")
                db.execute("""INSERT OR IGNORE INTO story_sources
                    (story_id,platform,external_id,region,language,publisher,publisher_domain,title,summary,url,published_at,discovered_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    story_id, platform, external_id, str(source.get("region", "")), str(source.get("language", "")),
                    publisher, publisher_domain(source),
                    str(source.get("title", story["title"])), str(source.get("summary", "")), source_url,
                    str(source.get("published_at", "")), now,
                ))
            evidence = [dict(row) for row in db.execute(
                "SELECT platform,external_id,region,language,publisher,publisher_domain,title,summary,url,published_at FROM story_sources WHERE story_id=?",
                (story_id,),
            )]

        corroborations, official = verification_evidence(evidence)
        story["sources"] = evidence
        assessment = assess_story(story, corroborations, settings)
        assessment = apply_verification_gate(assessment, evidence)
        story.update(assessment)
        should_notify = assessment.get("urgency") in {"urgent", "high"} and not was_notified
        with connect_db() as db:
            db.execute("""UPDATE stories SET verdict=?,confidence=?,relevance_score=?,urgency=?,summary_zh=?,
                rationale=?,corroborations=? WHERE id=?""", (
                assessment.get("verdict", "uncertain"), assessment.get("confidence", 0), assessment.get("relevance_score", 0),
                assessment.get("urgency", "medium"), assessment.get("summary_zh", ""), assessment.get("rationale", ""),
                corroborations, story_id,
            ))
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


def start_scan() -> bool:
    if not SCAN_LOCK.acquire(blocking=False):
        return False

    def worker() -> None:
        try:
            run_scan()
        except Exception as exc:
            SCAN_STATE.update(running=False, error=str(exc)[:240])
        finally:
            SCAN_LOCK.release()

    threading.Thread(target=worker, daemon=True, name="hot-monitor-scan").start()
    return True


def scheduler() -> None:
    while True:
        try:
            interval = max(5, int(get_settings().get("interval_minutes", "30"))) * 60
            with connect_db() as db:
                last = db.execute("SELECT finished_at FROM scans ORDER BY id DESC LIMIT 1").fetchone()
            elapsed = interval if not last else time.time() - datetime.fromisoformat(last["finished_at"]).timestamp()
            if elapsed >= interval and not SCAN_STATE["running"]:
                start_scan()
        except Exception as exc:
            SCAN_STATE.update(error=str(exc)[:240])
        time.sleep(20)


def dashboard_state() -> dict[str, Any]:
    settings = get_settings()
    with connect_db() as db:
        keywords = [dict(row) for row in db.execute("SELECT id,phrase,created_at FROM keywords ORDER BY id DESC")]
        stories = [dict(row) for row in db.execute("SELECT * FROM stories ORDER BY discovered_at DESC LIMIT 80")]
        story_ids = [story["id"] for story in stories]
        if story_ids:
            placeholders = ",".join("?" for _ in story_ids)
            source_rows = [dict(row) for row in db.execute(
                f"SELECT story_id,platform,external_id,region,language,publisher,publisher_domain,title,summary,url,published_at "
                f"FROM story_sources WHERE story_id IN ({placeholders}) ORDER BY id",
                story_ids,
            )]
        else:
            source_rows = []
        scans = [dict(row) for row in db.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 12")]
        totals = {row["verdict"]: row["count"] for row in db.execute("SELECT verdict,COUNT(*) AS count FROM stories GROUP BY verdict")}
        last_scan = db.execute("SELECT finished_at FROM scans ORDER BY id DESC LIMIT 1").fetchone()
    public_settings = {key: settings[key] for key in ("scope", "interval_minutes", "ai_base_url", "ai_model")}
    sources_by_story: dict[int, list[dict[str, Any]]] = {}
    for source in source_rows:
        sources_by_story.setdefault(source["story_id"], []).append(source)
    for story in stories:
        story["sources"] = sources_by_story.get(story["id"], [])
    public_settings.update({
        "ai_configured": bool(
            settings.get("ai_base_url") and settings.get("ai_model")
            and ("openrouter.ai" not in settings.get("ai_base_url", "") or settings.get("ai_api_key"))
        ),
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
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/state":
            return self._json(200, dashboard_state())
        if path == "/api/health":
            return self._json(200, {"ok": True})
        target = WEB_DIR / ("index.html" if path == "/" else path.lstrip("/"))
        try:
            target = target.resolve()
            if not target.is_relative_to(WEB_DIR.resolve()) or not target.is_file():
                raise FileNotFoundError
            data = target.read_bytes()
            content_type = "text/html; charset=utf-8" if target.suffix == ".html" else "text/css; charset=utf-8" if target.suffix == ".css" else "text/javascript; charset=utf-8" if target.suffix == ".js" else "application/octet-stream"
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


def main() -> None:
    init_db()
    threading.Thread(target=scheduler, daemon=True, name="hot-monitor-scheduler").start()
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
