import io
import tempfile
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import mock_open, patch

import app


class FeedTests(unittest.TestCase):
    def test_clean_html_removes_markup_and_collapses_whitespace(self):
        self.assertEqual(app.clean_html("<b>New&nbsp; model</b><br> update"), "New model update")

    def test_parse_feed_extracts_google_news_item(self):
        payload = b"""<?xml version='1.0' encoding='UTF-8'?>
        <rss><channel><item>
          <title>New model announced</title>
          <link>https://news.example/story</link>
          <description>&lt;b&gt;Company&lt;/b&gt; announced a model.</description>
          <source url='https://example.com'>Example News</source>
          <pubDate>Sat, 31 Oct 2026 10:00:00 GMT</pubDate>
        </item></channel></rss>"""
        stories = app.parse_feed(payload)
        self.assertEqual(len(stories), 1)
        self.assertEqual(stories[0]["source"], "Example News")
        self.assertEqual(stories[0]["summary"], "Company announced a model.")
        self.assertEqual(stories[0]["url"], "https://news.example/story")

    def test_parse_feed_uses_engine_name_and_result_domain_when_publisher_is_missing(self):
        payload = b"<rss><channel><item><title>AI launch</title><link>https://example.com/launch</link></item></channel></rss>"
        story = app.parse_feed(payload, "Bing RSS")[0]
        self.assertEqual(story["platform"], "Bing RSS")
        self.assertEqual(story["publisher"], "example.com")
        self.assertEqual(story["source_url"], "https://example.com/launch")

    def test_parse_duckduckgo_results_unwrap_redirect_and_preserve_publisher(self):
        payload = """<div class="result">
          <a class="result__a" href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fnews">New model</a>
          <a class="result__snippet">Company announced a model.</a>
        </div>"""
        stories = app.parse_duckduckgo_search(payload, "global")
        self.assertEqual(len(stories), 1)
        self.assertEqual(stories[0]["url"], "https://example.com/news")
        self.assertEqual(stories[0]["publisher"], "example.com")
        self.assertEqual(stories[0]["summary"], "Company announced a model.")

    def test_parse_hacker_news_story_preserves_item_and_original_url(self):
        stories = app.parse_hacker_news_search({"hits": [{
            "objectID": "123", "title": "A new AI tool", "url": "https://example.com/tool",
            "author": "alice", "created_at": "2026-11-01T10:00:00Z",
        }]})
        self.assertEqual(len(stories), 1)
        self.assertEqual(stories[0]["platform"], "Hacker News")
        self.assertEqual(stories[0]["external_id"], "123")
        self.assertEqual(stories[0]["url"], "https://example.com/tool")

    def test_parse_twitter_search_preserves_post_and_author_provenance(self):
        stories = app.parse_twitter_search({"tweets": [{
            "id": "12345",
            "url": "https://x.com/example/status/12345",
            "text": "A new model was announced",
            "createdAt": "2026-10-31T10:00:00Z",
            "likeCount": 50,
            "retweetCount": 20,
            "replyCount": 3,
            "viewCount": 2000,
            "author": {"userName": "example", "id": "67890", "followers": 500, "isBlueVerified": True},
        }]})
        self.assertEqual(len(stories), 1)
        self.assertEqual(stories[0]["platform"], "twitterapi.io")
        self.assertEqual(stories[0]["external_id"], "12345")
        self.assertEqual(stories[0]["publisher"], "example")
        self.assertEqual(stories[0]["url"], "https://x.com/example/status/12345")
        self.assertEqual(stories[0]["like_count"], 50)
        self.assertEqual(stories[0]["author_followers"], 500)
        self.assertTrue(stories[0]["author_verified"])

    def test_twitter_quality_filter_rejects_missing_or_below_threshold_metrics(self):
        base = {
            "id": "1", "text": "A new model was announced", "likeCount": 10,
            "retweetCount": 5, "viewCount": 500,
            "author": {"userName": "example", "followers": 100},
        }
        for field in ("likeCount", "retweetCount", "viewCount"):
            with self.subTest(field=field):
                item = dict(base)
                item.pop(field)
                candidate = app.parse_twitter_search({"tweets": [item]})[0]
                self.assertFalse(app.twitter_source_is_eligible(candidate))
        below_threshold = {**base, "likeCount": 9}
        candidate = app.parse_twitter_search({"tweets": [below_threshold]})[0]
        self.assertFalse(app.twitter_source_is_eligible(candidate))
        missing_followers = {**base, "author": {"userName": "example"}}
        candidate = app.parse_twitter_search({"tweets": [missing_followers]})[0]
        self.assertFalse(app.twitter_source_is_eligible(candidate))
        eligible = app.parse_twitter_search({"tweets": [base]})[0]
        self.assertTrue(app.twitter_source_is_eligible(eligible))

    def test_twitter_quality_filter_rejects_replies_quotes_and_retweets(self):
        base = {
            "id": "1", "text": "A new model was announced", "likeCount": 50,
            "retweetCount": 20, "viewCount": 2000,
            "author": {"userName": "example", "followers": 500},
        }
        for flags in ({"isReply": True}, {"inReplyToId": "parent"},
                      {"quoted_tweet": {"id": "quoted"}}, {"retweeted_tweet": {"id": "original"}}):
            with self.subTest(flags=flags):
                candidate = app.parse_twitter_search({"tweets": [{**base, **flags}]})[0]
                self.assertFalse(app.twitter_source_is_eligible(candidate))

    def test_fetch_feed_uses_global_news_locale(self):
        with patch("app.urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = b"<rss><channel/></rss>"
            app.fetch_feed("AI coding", "global")
        request = urlopen.call_args.args[0]
        params = app.urllib.parse.parse_qs(app.urllib.parse.urlparse(request.full_url).query)
        self.assertEqual(params["hl"], ["en-US"])
        self.assertEqual(params["gl"], ["US"])
        self.assertEqual(params["ceid"], ["US:en"])

    def test_fetch_bing_search_requests_rss(self):
        with patch("app.urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = b"<rss><channel/></rss>"
            app.fetch_bing_search("AI coding")
        request = urlopen.call_args.args[0]
        params = app.urllib.parse.parse_qs(app.urllib.parse.urlparse(request.full_url).query)
        self.assertEqual(params["format"], ["rss"])

    def test_fetch_hacker_news_uses_story_search(self):
        with patch("app.urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = json.dumps({"hits": []}).encode()
            app.fetch_hacker_news_search("AI coding")
        request = urlopen.call_args.args[0]
        params = app.urllib.parse.parse_qs(app.urllib.parse.urlparse(request.full_url).query)
        self.assertEqual(params["tags"], ["story"])
        self.assertEqual(params["query"], ["AI coding"])

    def test_fetch_twitter_search_uses_top_latest_and_two_cursor_pages(self):
        payloads = [
            {"tweets": [{"id": f"{page}-{query_type}", "text": "Launch"}],
             "has_next_page": True, "next_cursor": f"cursor-{page}-{query_type}"}
            for query_type in ("Top", "Latest") for page in (1, 2)
        ]
        payloads[1]["has_next_page"] = False
        payloads[3]["has_next_page"] = False
        with patch("app.time.sleep"), patch("app.urllib.request.urlopen") as urlopen:
            urlopen.side_effect = [mock_open(read_data=json.dumps(payload).encode())() for payload in payloads]
            stories = app.fetch_twitter_search("new model", "test-key")
        requests = [call.args[0] for call in urlopen.call_args_list]
        params = [app.urllib.parse.parse_qs(app.urllib.parse.urlparse(request.full_url).query, keep_blank_values=True)
                  for request in requests]
        self.assertEqual([item["queryType"][0] for item in params], ["Top", "Top", "Latest", "Latest"])
        self.assertEqual([item["cursor"][0] for item in params], ["", "cursor-1-Top", "", "cursor-1-Latest"])
        self.assertTrue(all(request.get_header("X-api-key") == "test-key" for request in requests))
        self.assertEqual(len(stories), 4)

    def test_parse_explicit_x_account_identifiers_without_guessing_topics(self):
        self.assertEqual(app.parse_account_identifier("@OpenAI"), ("x", "username", "OpenAI"))
        self.assertEqual(app.parse_account_identifier("https://x.com/OpenAI"), ("x", "username", "OpenAI"))
        self.assertEqual(app.parse_account_identifier("x:123456789"), ("x", "user_id", "123456789"))
        self.assertEqual(app.parse_account_identifier("https://space.bilibili.com/12345678"),
                         ("bilibili", "user_id", "12345678"))
        self.assertEqual(app.parse_account_identifier("https://weibo.com/u/123456"),
                         ("weibo", "user_id", "123456"))
        self.assertIsNone(app.parse_account_identifier("OpenAI announces a new model"))

    def test_fetch_twitter_account_uses_profile_and_original_tweets_endpoints(self):
        payloads = [
            {"userName": "OpenAI", "id": "123456789", "followers": 1000},
            {"tweets": [{"id": "1", "text": "Launch"}], "has_next_page": False, "next_cursor": ""},
        ]
        with patch("app.time.sleep"), patch("app.urllib.request.urlopen") as urlopen:
            urlopen.side_effect = [mock_open(read_data=json.dumps(payload).encode())() for payload in payloads]
            stories = app.fetch_twitter_account("username", "OpenAI", "test-key")
        requests = [call.args[0] for call in urlopen.call_args_list]
        self.assertEqual(app.urllib.parse.urlparse(requests[0].full_url).path, "/twitter/user/info")
        self.assertEqual(app.urllib.parse.urlparse(requests[1].full_url).path, "/twitter/user/last_tweets")
        params = app.urllib.parse.parse_qs(app.urllib.parse.urlparse(requests[1].full_url).query)
        self.assertEqual(params["userId"], ["123456789"])
        self.assertEqual(params["includeReplies"], ["false"])
        self.assertEqual(stories[0]["external_id"], "1")
        self.assertEqual(stories[0]["author_followers"], 1000)

    def test_normalize_title_deduplicates_punctuation_and_case(self):
        self.assertEqual(app.normalize_title("OpenAI: New Model!"), app.normalize_title("openai new model"))

    def test_similarity_detects_related_headlines(self):
        self.assertGreaterEqual(app.title_similarity("OpenAI launches new coding model", "OpenAI launches coding model today"), 0.34)

    def test_cluster_stories_keeps_distinct_sources_for_related_headlines(self):
        clusters = app.cluster_stories([
            {"title": "OpenAI launches a new coding model", "summary": "", "sources": [
                {"platform": "Google News", "external_id": "a", "url": "https://a.example"},
            ]},
            {"title": "OpenAI launches new coding model today", "summary": "A longer report", "sources": [
                {"platform": "twitterapi.io", "external_id": "b", "url": "https://x.com/a/status/b"},
            ]},
        ])
        self.assertEqual(len(clusters), 1)
        self.assertEqual(len(clusters[0]["sources"]), 2)

    def test_single_unofficial_publisher_cannot_be_marked_credible(self):
        result = app.apply_verification_gate(
            {"verdict": "credible", "confidence": 0.99, "rationale": "模型判断可信"},
            [{"platform": "Google News", "publisher": "Unknown News", "source_url": "https://unknown.example"}],
        )
        self.assertEqual(result["verdict"], "uncertain")
        self.assertEqual(result["corroborations"], 1)

    def test_independent_publishers_can_pass_verification_gate(self):
        result = app.apply_verification_gate(
            {"verdict": "credible", "confidence": 0.9, "rationale": "多方报道"},
            [
                {"platform": "Google News", "publisher": "Reuters", "source_url": "https://reuters.com/world/story"},
                {"platform": "Google News", "publisher": "AP", "source_url": "https://apnews.com/article/story"},
            ],
        )
        self.assertEqual(result["verdict"], "credible")
        self.assertEqual(result["corroborations"], 2)

    def test_two_unknown_domains_cannot_pass_verification_gate(self):
        result = app.apply_verification_gate(
            {"verdict": "credible", "confidence": 0.99, "rationale": "模型判断可信"},
            [
                {"platform": "Google News", "publisher_domain": "spam-a.example"},
                {"platform": "Google News", "publisher_domain": "spam-b.example"},
            ],
        )
        self.assertEqual(result["verdict"], "uncertain")
        self.assertEqual(result["corroborations"], 1)

    def test_official_source_uses_stored_publisher_domain(self):
        result = app.apply_verification_gate(
            {"verdict": "credible", "confidence": 0.9, "rationale": "官方公告"},
            [{"platform": "Google News", "publisher": "OpenAI", "publisher_domain": "openai.com",
              "url": "https://news.google.com/rss/articles/example"}],
        )
        self.assertEqual(result["verdict"], "credible")
        self.assertEqual(result["corroborations"], 1)

    def test_missing_model_never_claims_ai_verification(self):
        result = app.assess_story(
            {"title": "A claim", "summary": "", "source": "Unknown", "url": "https://example.com"},
            1,
            {"ai_base_url": "", "ai_model": ""},
        )
        self.assertEqual(result["verdict"], "unverified")
        self.assertEqual(result["confidence"], 0)

    def test_assess_story_requests_strict_openrouter_json_schema(self):
        response_body = {"choices": [{"message": {"content": json.dumps({
            "verdict": "uncertain", "confidence": 0.4, "rationale": "证据不足", "evidence_gaps": ["缺少官方公告"],
            "relevance_score": 72, "urgency": "medium", "summary_zh": "新模型更新",
            "translation": "A new model update", "translation_language": "en",
        })}}]}
        with patch("app.urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = json.dumps(response_body).encode()
            result = app.assess_story(
                {"title": "Claim", "summary": "", "source": "Outlet", "url": "https://example.com",
                 "sources": [{"publisher": "Outlet", "url": "https://example.com/story"}]},
                1,
                {"ai_base_url": "https://openrouter.ai/api/v1", "ai_model": "deepseek/deepseek-v4.1-flash", "ai_api_key": "test-key"},
            )
        request = urlopen.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
        self.assertEqual(body["response_format"]["type"], "json_schema")
        self.assertTrue(body["response_format"]["json_schema"]["strict"])
        schema = body["response_format"]["json_schema"]["schema"]
        self.assertEqual(schema["properties"]["urgency"]["enum"], ["urgent", "high", "medium", "low"])
        self.assertEqual(schema["properties"]["relevance_score"]["maximum"], 100)
        self.assertIn("summary_zh", schema["required"])
        self.assertIn("assistant", [message["role"] for message in body["messages"]])
        self.assertIn("低于35", " ".join(message["content"] for message in body["messages"]))
        self.assertEqual(result["verdict"], "uncertain")
        self.assertEqual(result["relevance_score"], 72)
        self.assertEqual(result["urgency"], "medium")
        self.assertEqual(result["summary_zh"], "新模型更新")
        self.assertEqual(result["translation"], "A new model update")

    def test_translation_target_uses_source_language_and_script(self):
        for language in ("ja", "ko", "zh"):
            with self.subTest(language=language):
                self.assertEqual(app.translation_target_for_story({
                    "title": "A post", "sources": [{"language": language}],
                }), "zh")
        for language in ("es", "fr"):
            with self.subTest(language=language):
                self.assertEqual(app.translation_target_for_story({
                    "title": "Un anuncio", "sources": [{"language": language}],
                }), "en")
        self.assertEqual(app.translation_target_for_story({"title": "新しいモデル", "sources": []}), "zh")


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temporary_directory.name) / "test.db"
        self.db_path_patch = patch.object(app, "DB_PATH", self.db_path)
        self.db_path_patch.start()
        self.environment_patch = patch.dict("os.environ", {name: "" for name in app.ENV_SETTINGS.values()}, clear=False)
        self.environment_patch.start()
        app.init_db()

    def tearDown(self):
        self.environment_patch.stop()
        self.db_path_patch.stop()
        self.temporary_directory.cleanup()

    def test_interval_is_clamped_and_secret_is_stored(self):
        app.save_settings({"interval_minutes": "1", "telegram_bot_token": "secret"})
        settings = app.get_settings()
        self.assertEqual(settings["interval_minutes"], "5")
        self.assertEqual(settings["telegram_bot_token"], "secret")

    def test_stories_schema_includes_urgency_relevance_and_chinese_summary(self):
        with app.connect_db() as db:
            columns = {row["name"] for row in db.execute("PRAGMA table_info(stories)")}
        self.assertTrue({"urgency", "relevance_score", "summary_zh"} <= columns)
        self.assertTrue({"translation", "translation_language"} <= columns)
        with app.connect_db() as db:
            source_columns = {row["name"] for row in db.execute("PRAGMA table_info(story_sources)")}
        self.assertTrue({"like_count", "retweet_count", "reply_count", "view_count", "author_followers",
                         "author_verified", "content_type"} <= source_columns)

    def test_dashboard_state_never_returns_saved_credentials(self):
        secrets = ("private-openrouter", "private-twitter", "private-smtp")
        app.save_settings({
            "ai_api_key": secrets[0],
            "twitter_api_key": secrets[1],
            "smtp_password": secrets[2],
            "smtp_host": "smtp.example.com",
            "smtp_sender": "sender@example.com",
            "smtp_recipient": "reader@example.com",
        })
        payload = json.dumps(app.dashboard_state())
        for secret in secrets:
            self.assertNotIn(secret, payload)
        self.assertTrue(app.dashboard_state()["settings"]["smtp_configured"])

    def test_dashboard_state_returns_persisted_story_sources(self):
        with app.connect_db() as db:
            db.execute("INSERT INTO stories(fingerprint,title,url,discovered_at) VALUES(?,?,?,?)",
                       ("story-one", "A headline", "https://example.com", "2026-10-31T00:00:00Z"))
            story_id = db.execute("SELECT id FROM stories WHERE fingerprint='story-one'").fetchone()["id"]
            db.execute("""INSERT INTO story_sources
                (story_id,platform,external_id,region,language,publisher,title,url,discovered_at,like_count,retweet_count,
                 reply_count,view_count,author_followers,author_verified,content_type)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                story_id, "Google News", "source-one", "global", "en", "Reuters", "A headline",
                "https://reuters.com/story", "2026-10-31T00:00:00Z", 50, 20, 3, 2000, 500, 1, "original",
            ))
        story = app.dashboard_state()["stories"][0]
        self.assertEqual(story["sources"][0]["region"], "global")
        self.assertEqual(story["sources"][0]["language"], "en")
        self.assertEqual(story["sources"][0]["like_count"], 50)

    def test_dashboard_state_can_return_more_than_eighty_stories(self):
        with app.connect_db() as db:
            db.executemany(
                "INSERT INTO stories(fingerprint,title,url,discovered_at) VALUES(?,?,?,?)",
                [(f"story-{index}", f"Headline {index}", "https://example.com", f"2026-10-{index % 28 + 1:02d}T00:00:00Z")
                 for index in range(125)],
            )
        state = app.dashboard_state(limit=120)
        self.assertEqual(len(state["stories"]), 120)
        self.assertEqual(state["total_story_count"], 125)

    def test_dashboard_state_marks_ai_failures_as_unavailable_not_zero_relevance(self):
        with app.connect_db() as db:
            db.execute("INSERT INTO stories(fingerprint,title,url,discovered_at,rationale) VALUES(?,?,?,?,?)",
                       ("ai-error-story", "A headline", "https://example.com", "2026-10-31T00:00:00Z",
                        "AI 核验不可用：HTTP Error 429: Too Many Requests"))
        story = next(item for item in app.dashboard_state()["stories"] if item["fingerprint"] == "ai-error-story")
        self.assertEqual(story["relevance_score"], 0)
        self.assertTrue(story["relevance_unavailable"])

    def test_dashboard_state_shows_ineligible_historical_x_story_with_warning(self):
        with app.connect_db() as db:
            db.execute("INSERT INTO stories(fingerprint,title,url,source,discovered_at) VALUES(?,?,?,?,?)",
                       ("old-x-story", "Old reply", "https://x.com/example/status/1", "X @example", "2026-10-31T00:00:00Z"))
            story_id = db.execute("SELECT id FROM stories WHERE fingerprint='old-x-story'").fetchone()["id"]
            db.execute("""INSERT INTO story_sources
                (story_id,platform,external_id,publisher,title,url,discovered_at,like_count,retweet_count,
                 view_count,author_followers,content_type)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (
                story_id, "twitterapi.io", "old-x-post", "example", "Old reply",
                "https://x.com/example/status/1", "2026-10-31T00:00:00Z", 100, 50, 5000, 1000, "reply",
            ))
        story = next(item for item in app.dashboard_state()["stories"] if item["id"] == story_id)
        self.assertTrue(story["source_quality_warning"])
        self.assertEqual(len(story["sources"]), 1)
        self.assertEqual(story["sources"][0]["like_count"], 100)
        self.assertFalse(story["sources"][0]["quality_eligible"])
        self.assertEqual(story["sources"][0]["quality_status"], "ineligible")

    def test_dashboard_state_marks_missing_historical_x_metrics_as_unknown(self):
        with app.connect_db() as db:
            db.execute("INSERT INTO stories(fingerprint,title,url,source,discovered_at) VALUES(?,?,?,?,?)",
                       ("old-x-missing-metrics", "Old X post", "https://x.com/example/status/5",
                        "X @example", "2026-10-31T00:00:00Z"))
            story_id = db.execute("SELECT id FROM stories WHERE fingerprint='old-x-missing-metrics'").fetchone()["id"]
            db.execute("""INSERT INTO story_sources
                (story_id,platform,external_id,publisher,title,url,discovered_at)
                VALUES(?,?,?,?,?,?,?)""", (
                story_id, "legacy", "legacy:missing-metrics", "X @example", "Old X post",
                "https://x.com/example/status/5", "2026-10-31T00:00:00Z",
            ))
        story = next(item for item in app.dashboard_state()["stories"]
                     if item["fingerprint"] == "old-x-missing-metrics")
        self.assertEqual(story["sources"][0]["quality_status"], "unknown")
        self.assertFalse(story["source_quality_warning"])

    def test_dashboard_state_hides_legacy_source_when_same_x_source_is_present(self):
        with app.connect_db() as db:
            db.execute("INSERT INTO stories(fingerprint,title,url,source,discovered_at) VALUES(?,?,?,?,?)",
                       ("duplicate-x-story", "A headline", "https://x.com/example/status/6", "X @example",
                        "2026-10-31T00:00:00Z"))
            story_id = db.execute("SELECT id FROM stories WHERE fingerprint='duplicate-x-story'").fetchone()["id"]
            db.executemany("""INSERT INTO story_sources
                (story_id,platform,external_id,publisher,title,url,discovered_at)
                VALUES(?,?,?,?,?,?,?)""", [
                (story_id, "twitterapi.io", "post-6", "example", "A headline",
                 "https://x.com/example/status/6", "2026-10-31T00:00:00Z"),
                (story_id, "legacy", "legacy:6", "X @example", "A headline",
                 "https://x.com/example/status/6", "2026-10-31T00:00:00Z"),
            ])
        story = next(item for item in app.dashboard_state()["stories"] if item["id"] == story_id)
        self.assertEqual(len(story["sources"]), 1)
        self.assertEqual(story["sources"][0]["platform"], "twitterapi.io")

    def test_existing_source_backfills_interaction_counts_without_losing_saved_values(self):
        with app.connect_db() as db:
            db.execute("INSERT INTO stories(fingerprint,title,url,discovered_at) VALUES(?,?,?,?)",
                       ("metrics-story", "A headline", "https://x.com/example/status/1", "2026-10-31T00:00:00Z"))
            story_id = db.execute("SELECT id FROM stories WHERE fingerprint='metrics-story'").fetchone()["id"]
            db.execute("""INSERT INTO story_sources
                (story_id,platform,external_id,publisher,publisher_domain,title,url,discovered_at,content_type)
                VALUES(?,?,?,?,?,?,?,?,?)""", (
                story_id, "twitterapi.io", "post-1", "example", "x.com", "A headline",
                "https://x.com/example/status/1", "2026-10-31T00:00:00Z", "original",
            ))
            changed = app.save_story_source(db, story_id, {
                "platform": "twitterapi.io", "external_id": "post-1", "publisher": "example",
                "title": "A headline", "url": "https://x.com/example/status/1", "content_type": "original",
                "like_count": 123, "retweet_count": 45, "reply_count": 6, "view_count": 7890,
                "author_followers": 321,
            }, "2026-11-01T00:00:00Z")
            saved = db.execute("SELECT * FROM story_sources WHERE external_id='post-1'").fetchone()
        self.assertFalse(changed)
        self.assertEqual(saved["like_count"], 123)
        self.assertEqual(saved["retweet_count"], 45)
        self.assertEqual(saved["view_count"], 7890)

    def test_scan_reuses_assessment_when_existing_sources_do_not_change(self):
        app.save_settings({"twitter_api_key": "test-twitter-key"})
        with app.connect_db() as db:
            db.execute("INSERT INTO keywords(phrase,created_at) VALUES(?,?)",
                       ("AI coding", "2026-10-31T00:00:00Z"))
            db.execute("INSERT INTO stories(fingerprint,title,url,source,discovered_at) VALUES(?,?,?,?,?)",
                       (app.hashlib.sha256(app.normalize_title("A coding update").encode()).hexdigest(),
                        "A coding update", "https://x.com/example/status/1", "X @example", "2026-10-31T00:00:00Z"))
            story_id = db.execute("SELECT id FROM stories").fetchone()["id"]
            source = {
                "platform": "twitterapi.io", "external_id": "post-1", "publisher": "example",
                "publisher_domain": "x.com", "title": "A coding update", "summary": "Details",
                "url": "https://x.com/example/status/1", "published_at": "2026-10-31T00:00:00Z",
                "region": "global", "language": "en", "like_count": 123, "retweet_count": 45,
                "reply_count": 6, "view_count": 7890, "author_followers": 321,
                "author_verified": False, "content_type": "original",
            }
            app.save_story_source(db, story_id, source, "2026-10-31T00:00:00Z")
        with (
            patch.object(app, "translate_query_to_english", return_value=""),
            patch.object(app, "fetch_feed", return_value=[]),
            patch.object(app, "fetch_bing_search", return_value=[]),
            patch.object(app, "fetch_duckduckgo_search", return_value=[]),
            patch.object(app, "fetch_hacker_news_search", return_value=[]),
            patch.object(app, "fetch_twitter_search", return_value=[source]),
            patch.object(app, "assess_story") as assess,
            patch.object(app.time, "sleep"),
        ):
            app.run_scan()
        assess.assert_not_called()

    def test_keywords_persist_enabled_state_and_only_enabled_keywords_are_scanned(self):
        with app.connect_db() as db:
            db.execute("INSERT INTO keywords(phrase,created_at,enabled) VALUES(?,?,?)",
                       ("active topic", "2026-10-31T00:00:00Z", 1))
            db.execute("INSERT INTO keywords(phrase,created_at,enabled) VALUES(?,?,?)",
                       ("paused topic", "2026-10-31T00:00:00Z", 0))
        state = app.dashboard_state()
        self.assertEqual([item["phrase"] for item in state["keywords"] if item["enabled"]], ["active topic"])
        self.assertEqual(app.get_enabled_keywords(), ["active topic"])

    def test_dashboard_state_replaces_ineligible_x_primary_with_valid_source(self):
        with app.connect_db() as db:
            db.execute("INSERT INTO stories(fingerprint,title,url,source,discovered_at) VALUES(?,?,?,?,?)",
                       ("mixed-story", "Quoted X post", "https://x.com/example/status/2", "X @example",
                        "2026-10-31T00:00:00Z"))
            story_id = db.execute("SELECT id FROM stories WHERE fingerprint='mixed-story'").fetchone()["id"]
            db.execute("""INSERT INTO story_sources
                (story_id,platform,external_id,publisher,title,summary,url,discovered_at,like_count,retweet_count,
                 view_count,author_followers,content_type)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                story_id, "twitterapi.io", "old-x-quote", "example", "Quoted X post", "X summary",
                "https://x.com/example/status/2", "2026-10-31T00:00:00Z", 100, 50, 5000, 1000, "quote",
            ))
            db.execute("""INSERT INTO story_sources
                (story_id,platform,external_id,publisher,title,summary,url,discovered_at)
                VALUES(?,?,?,?,?,?,?,?)""", (
                story_id, "Google News", "source-2", "Reuters", "Original report", "Verified report",
                "https://reuters.com/story", "2026-10-31T00:00:00Z",
            ))
        story = next(item for item in app.dashboard_state()["stories"] if item["id"] == story_id)
        self.assertEqual(story["title"], "Original report")
        self.assertEqual(story["url"], "https://reuters.com/story")
        self.assertEqual([source["publisher"] for source in story["sources"]], ["example", "Reuters"])

    def test_nonempty_environment_key_overrides_saved_key_and_empty_keeps_saved(self):
        app.save_settings({"ai_api_key": "saved-key"})
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "environment-key"}, clear=False):
            self.assertEqual(app.get_settings()["ai_api_key"], "environment-key")
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": ""}, clear=False):
            self.assertEqual(app.get_settings()["ai_api_key"], "saved-key")

    def test_default_openrouter_without_key_is_not_marked_configured(self):
        self.assertEqual(app.get_settings()["ai_model"], "deepseek/deepseek-v4.1-flash")
        self.assertFalse(app.dashboard_state()["settings"]["ai_configured"])

    def test_start_scan_runs_worker_and_releases_lock(self):
        def immediate_thread(**kwargs):
            class InlineThread:
                def start(self):
                    kwargs["target"]()
            return InlineThread()

        with patch.object(app.threading, "Thread", side_effect=immediate_thread), patch.object(app, "run_scan") as run_scan:
            self.assertTrue(app.start_scan())
        run_scan.assert_called_once()
        self.assertTrue(app.SCAN_LOCK.acquire(blocking=False))
        app.SCAN_LOCK.release()

    def test_query_translation_uses_cached_english_term(self):
        with app.connect_db() as db:
            db.execute("INSERT INTO query_translations(original,english,updated_at) VALUES(?,?,?)",
                       ("新模型", "new AI model", "2026-10-31T00:00:00+00:00"))
        with patch("app.urllib.request.urlopen") as urlopen:
            result = app.translate_query_to_english("新模型", {})
        self.assertEqual(result, "new AI model")
        urlopen.assert_not_called()

    def test_scan_aggregates_regional_sources_before_notifying(self):
        app.save_settings({"twitter_api_key": "test-twitter-key", "ai_api_key": "test-openrouter-key"})
        with app.connect_db() as db:
            db.execute("INSERT INTO keywords(phrase,created_at) VALUES(?,?)",
                       ("AI 编程", "2026-10-31T00:00:00Z"))
        china_story = {
            "title": "OpenAI announces a new coding model", "summary": "Chinese report",
            "url": "https://news.google.com/china-story", "source": "Outlet A", "publisher": "Outlet A",
            "source_url": "https://reuters.com/story", "published_at": "2026-10-31T01:00:00Z",
            "platform": "Google News", "region": "china",
        }
        global_story = {
            "title": "OpenAI announces new coding model today", "summary": "Global report",
            "url": "https://news.google.com/global-story", "source": "AP", "publisher": "AP",
            "source_url": "https://apnews.com/story", "published_at": "2026-10-31T02:00:00Z",
            "platform": "Google News", "region": "global",
        }
        with (
            patch.object(app, "translate_query_to_english", return_value="new coding model"),
            patch.object(app, "fetch_feed", side_effect=[[china_story], [global_story]]),
            patch.object(app, "fetch_bing_search", side_effect=OSError("Bing temporarily unavailable")),
            patch.object(app, "fetch_duckduckgo_search", return_value=[]),
            patch.object(app, "fetch_hacker_news_search", return_value=[]),
            patch.object(app, "fetch_twitter_search", return_value=[]),
            patch.object(app, "assess_story", return_value={
                "verdict": "credible", "confidence": 0.95, "relevance_score": 92, "urgency": "high",
                "summary_zh": "模型发布消息", "rationale": "多方报道",
            }),
            patch.object(app, "send_notifications", return_value=True) as notify,
            patch.object(app.time, "sleep"),
        ):
            scan_output = io.StringIO()
            with redirect_stdout(scan_output):
                app.run_scan()

        with app.connect_db() as db:
            story = db.execute("SELECT * FROM stories").fetchone()
            sources = db.execute("SELECT * FROM story_sources WHERE story_id=?", (story["id"],)).fetchall()
        self.assertEqual(story["verdict"], "credible")
        self.assertEqual(story["urgency"], "high")
        self.assertEqual(story["relevance_score"], 92)
        self.assertEqual(story["corroborations"], 2)
        self.assertEqual(story["notified"], 1)
        self.assertEqual(len(sources), 2)
        self.assertEqual({source["region"] for source in sources}, {"china", "global"})
        self.assertIn('Google News RSS search for "AI 编程" (china): found 1 results', scan_output.getvalue())
        self.assertIn("[results] 2 raw -> 1 title clusters", scan_output.getvalue())
        self.assertIn("[complete] Total: 2 raw -> 1 unique -> 1 new", scan_output.getvalue())
        self.assertIn("[new hotspot] [high] OpenAI announces a new coding model", scan_output.getvalue())
        with app.connect_db() as db:
            scan = db.execute("SELECT status FROM scans ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(scan["status"], "partial")
        notify.assert_called_once()

    def test_scan_does_not_notify_for_ineligible_x_candidate(self):
        app.save_settings({"twitter_api_key": "test-twitter-key", "scope": "AI coding"})
        with app.connect_db() as db:
            db.execute("INSERT INTO keywords(phrase,created_at) VALUES(?,?)",
                       ("AI coding", "2026-10-31T00:00:00Z"))
        rejected_source = {
            "title": "A reply claiming a major launch", "summary": "", "url": "https://x.com/example/status/3",
            "source": "X @example", "publisher": "example", "source_url": "https://x.com/example/status/3",
            "published_at": "2026-10-31T01:00:00Z", "platform": "twitterapi.io", "external_id": "3",
            "like_count": 100, "retweet_count": 50, "view_count": 5000, "author_followers": 1000,
            "content_type": "reply",
        }
        with (
            patch.object(app, "translate_query_to_english", return_value=""),
            patch.object(app, "fetch_feed", return_value=[]),
            patch.object(app, "fetch_bing_search", return_value=[]),
            patch.object(app, "fetch_duckduckgo_search", return_value=[]),
            patch.object(app, "fetch_hacker_news_search", return_value=[]),
            patch.object(app, "fetch_twitter_search", return_value=[rejected_source]),
            patch.object(app, "assess_story", return_value={
                "verdict": "credible", "confidence": 0.99, "relevance_score": 95, "urgency": "urgent",
                "summary_zh": "发布消息", "rationale": "模型判断可信",
            }),
            patch.object(app, "send_notifications", return_value=True) as notify,
            patch.object(app.time, "sleep"),
        ):
            app.run_scan()
        with app.connect_db() as db:
            story = db.execute("SELECT verdict,notified FROM stories").fetchone()
        self.assertEqual(story["verdict"], "uncertain")
        self.assertEqual(story["notified"], 0)
        notify.assert_not_called()

    def test_scan_routes_explicit_x_handle_to_account_lookup(self):
        app.save_settings({"twitter_api_key": "test-twitter-key", "scope": "@OpenAI"})
        with app.connect_db() as db:
            db.execute("INSERT INTO keywords(phrase,created_at) VALUES(?,?)",
                       ("@OpenAI", "2026-10-31T00:00:00Z"))
        account_source = {
            "title": "OpenAI release note", "summary": "", "url": "https://x.com/OpenAI/status/4",
            "source": "X @OpenAI", "publisher": "OpenAI", "source_url": "https://x.com/OpenAI/status/4",
            "published_at": "2026-10-31T01:00:00Z", "platform": "twitterapi.io", "external_id": "4",
            "like_count": 100, "retweet_count": 20, "view_count": 1000, "author_followers": 1000,
            "content_type": "original",
        }
        with (
            patch.object(app, "translate_query_to_english", return_value=""),
            patch.object(app, "fetch_feed", return_value=[]),
            patch.object(app, "fetch_bing_search", return_value=[]),
            patch.object(app, "fetch_duckduckgo_search", return_value=[]),
            patch.object(app, "fetch_hacker_news_search", return_value=[]),
            patch.object(app, "fetch_twitter_search") as topic_search,
            patch.object(app, "fetch_twitter_account", return_value=[account_source]) as account_lookup,
            patch.object(app, "assess_story", return_value={"verdict": "uncertain", "urgency": "medium"}),
            patch.object(app.time, "sleep"),
        ):
            app.run_scan()
        account_lookup.assert_called_once_with("username", "OpenAI", "test-twitter-key")
        topic_search.assert_not_called()

    def test_existing_source_table_is_migrated_idempotently(self):
        with app.connect_db() as db:
            db.execute("DROP TABLE story_sources")
            db.execute("""CREATE TABLE story_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                story_id INTEGER NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
                platform TEXT NOT NULL, external_id TEXT NOT NULL,
                region TEXT NOT NULL DEFAULT '', language TEXT NOT NULL DEFAULT '',
                publisher TEXT NOT NULL DEFAULT '', publisher_domain TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '', url TEXT NOT NULL,
                published_at TEXT NOT NULL DEFAULT '', discovered_at TEXT NOT NULL,
                UNIQUE(platform, external_id)
            )""")
        app.init_db()
        app.init_db()
        with app.connect_db() as db:
            columns = {row["name"] for row in db.execute("PRAGMA table_info(story_sources)")}
        self.assertTrue({"like_count", "retweet_count", "reply_count", "view_count", "author_followers",
                         "author_verified", "content_type"} <= columns)

    def test_unverified_high_priority_story_does_not_send_notifications(self):
        with patch.object(app, "publish_websocket") as websocket, patch.object(app, "send_email") as email:
            sent = app.send_notifications(
                {"title": "Unverified claim", "urgency": "high", "verdict": "uncertain"}, [], {},
            )
        self.assertFalse(sent)
        websocket.assert_not_called()
        email.assert_not_called()

    def test_send_email_uses_starttls_and_message(self):
        settings = {
            "smtp_host": "smtp.example.com", "smtp_port": "587", "smtp_starttls": "1",
            "smtp_sender": "sender@example.com", "smtp_recipient": "reader@example.com",
        }
        with patch("app.smtplib.SMTP") as smtp:
            server = smtp.return_value.__enter__.return_value
            sent = app.send_email("Subject", "Body", settings)
        self.assertTrue(sent)
        server.starttls.assert_called_once()
        message = server.send_message.call_args.args[0]
        self.assertEqual(message["To"], "reader@example.com")
        self.assertEqual(message.get_content().strip(), "Body")


if __name__ == "__main__":
    unittest.main()
