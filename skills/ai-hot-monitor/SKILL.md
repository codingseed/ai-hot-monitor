---
name: ai-hot-monitor
description: Monitor AI, software, product launches, and user-defined topics across Chinese and international sources; verify evidence before reporting; optionally read the local 热探 dashboard.
---

# AI Hot Monitor

Use this skill when Leo asks for current developments, breaking AI/model releases, monitoring a saved topic, or a recurring digest. Search in Chinese and English where possible. Prefer fresh primary sources and dated reporting. Never present an unverified claim as confirmed.

## Operating Modes

1. **Local dashboard available**: use the local API at `http://127.0.0.1:8765`.
2. **No local dashboard**: use available live web-search/browser tools. Search the exact topic and a broader category in Chinese and English. If live tools are unavailable, explain that current monitoring cannot be performed rather than inventing news.

## Local Dashboard Workflow

- Check `GET /api/health` before using the dashboard.
- Read `GET /api/state` for saved scope, keywords, last scan, current stories, source URLs, region/language metadata, and verification state. The API does not return credential values.
- To add a user-requested keyword, send `POST /api/keywords` with JSON `{"phrase":"..."}`. To remove one, send `DELETE /api/keywords/{id}`.
- Start a scan with `POST /api/scan` and an empty JSON object. The scan runs asynchronously. Poll `GET /api/state` at a reasonable interval until `scan_state.running` is false.
- Report items with title, publisher/account, original URL, publication time, region/language, urgency, relevance score, Chinese summary, verdict, rationale, and independent source count. Authenticity verdict (credible/suspicious/uncertain/unverified) is an independent label, not the main grouping.
- A story is only eligible for `credible` when the model supports it and the application finds an official domain/account or at least two independent publishers. Reposts and duplicate aggregator copies are not independent.
- Browser WebSocket notifications and SMTP/Telegram email are delivered by the dashboard for `urgent` and `high` urgency stories only, regardless of authenticity verdict. Suspicious items are explicitly marked in the message. Do not trigger `POST /api/notifications/test-email` unless Leo explicitly asks to test email delivery.
- Never ask Leo to paste API keys or SMTP passwords into chat. Direct him to enter them in the local settings page.

Example read-only request:

```python
import json
from urllib.request import urlopen

with urlopen("http://127.0.0.1:8765/api/state", timeout=5) as response:
    state = json.load(response)
print(state["settings"]["scope"], state["keywords"], state["last_scan"])
```

## Live Search Workflow

- Search exact phrases and broader categories in both Chinese and English; include recency terms such as `today`, `latest`, or the current date.
- For a model release or company announcement, check the organization's newsroom, release notes, model documentation, or official account. Seek independent reporting for corroboration.
- Open source pages when possible. Confirm the date, who made the claim, whether the page is original or reposted, and whether the content supports the headline.
- Treat anonymous posts, unsourced screenshots, lookalike domains, copied releases, and unsupported benchmark claims as suspicious or unverified. Multiple aggregators repeating one article are not independent evidence.
- Summarize with links and timestamps. Separate confirmed facts from inference; state when evidence is thin or sources conflict.

## Safety and Privacy

- Do not send notifications or make external changes unless the dashboard is configured to do so or the user explicitly requests them.
- Never expose local API keys, TwitterAPI.io keys, SMTP passwords, database contents, or private workspace details.
- The HTTP and WebSocket servers bind to loopback by default. Do not expose the unauthenticated local API to a public network.
- A language model can help assess evidence but cannot independently prove authenticity. Prefer primary sources, preserve source links, and maintain uncertainty.
