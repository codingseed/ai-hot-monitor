---
name: ai-hot-monitor-standalone
description: Self-contained AI hotspot monitor for Leo. Search Chinese and international sources directly, verify evidence, and produce an AI hotspot report without requiring the local AI 热探 dashboard, database, or OpenRouter. Use when Leo asks for current AI/model/product developments, a topic digest, or monitoring a keyword. TwitterAPI.io is optional.
---

# AI Hot Monitor Standalone

This skill is intentionally self-contained. It does not depend on the local `AI 热探` server, SQLite database, or OpenRouter. Claude performs relevance, authenticity, urgency, and summary analysis using the frameworks in `references/`.

## When to use

- Leo asks what is new or trending in AI, models, coding tools, products, or a named company.
- Leo asks to monitor a keyword or topic and produce a digest.
- The local dashboard is unavailable, or a one-off report is requested.
- A recurring manual scan is needed without starting the full local service.

## Operating principles

1. Search in both Chinese and English when the topic is bilingual.
2. Prefer primary sources and dated reporting.
3. Never present an unverified claim as confirmed.
4. Multiple aggregators repeating one article are not independent evidence.
5. Twitter/X is optional and only runs when `TWITTERAPI_API_KEY` is available.
6. Do not request or echo API keys in chat. Ask Leo to set environment variables locally.

## Scripts

Run from the skill directory or with absolute paths.

```bash
python scripts/search_web.py --query "AI coding agent" --limit 10 --output web.json
python scripts/search_china.py --query "AI 编程" --limit 10 --output china.json
python scripts/search_twitter.py --query "AI coding" --limit 10 --output twitter.json
python scripts/generate_report.py --inputs web.json china.json twitter.json --output report.md
```

If `--output` is omitted, each script writes JSON to stdout.

## Workflow

1. Normalize the user topic into one Chinese query and one English query when useful.
2. Run `search_web.py` for international sources.
3. Run `search_china.py` for Chinese-platform sources.
4. Run `search_twitter.py` only if `TWITTERAPI_API_KEY` is set; otherwise state that X coverage is skipped.
5. Combine outputs with `generate_report.py`.
6. Read `references/source-policy.md`, `references/relevance-rubric.md`, and `references/verification-rubric.md`.
7. For each candidate, assess:
   - relevance score 0-100
   - urgency: urgent/high/medium/low
   - authenticity: credible/suspicious/uncertain/unverified
   - Chinese summary
   - translation when useful
   - rationale and evidence gaps
8. Produce the final report using `references/report-template.md`.
9. Clearly mark partial coverage, failed sources, and unverified items.

## Relevance and thresholds

- 50-60 is the recommended decision band for balancing precision and recall.
- Below 55 should not be included in the default filtered result unless it is unverified but potentially useful.
- Do not inflate scores because a story merely mentions AI, models, or technology.

## Safety and privacy

- Do not bypass robots.txt, CAPTCHA, login walls, or access controls.
- Do not send notifications or modify external systems unless Leo explicitly asks.
- Bing RSS is for personal non-commercial aggregation only.
- DuckDuckGo `/html` is disallowed and must not be requested.
- Never expose API keys, database contents, or private workspace details.