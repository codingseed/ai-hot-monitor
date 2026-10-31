import tempfile
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def test_parse_twitter_search_preserves_post_and_author_provenance(self):
        stories = app.parse_twitter_search({"tweets": [{
            "id": "12345",
            "url": "https://x.com/example/status/12345",
            "text": "A new model was announced",
            "createdAt": "2026-10-31T10:00:00Z",
            "author": {"userName": "example", "id": "67890"},
        }]})
        self.assertEqual(len(stories), 1)
        self.assertEqual(stories[0]["platform"], "twitterapi.io")
        self.assertEqual(stories[0]["external_id"], "12345")
        self.assertEqual(stories[0]["publisher"], "example")
        self.assertEqual(stories[0]["url"], "https://x.com/example/status/12345")

    def test_fetch_feed_uses_global_news_locale(self):
        with patch("app.urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = b"<rss><channel/></rss>"
            app.fetch_feed("AI coding", "global")
        request = urlopen.call_args.args[0]
        params = app.urllib.parse.parse_qs(app.urllib.parse.urlparse(request.full_url).query)
        self.assertEqual(params["hl"], ["en-US"])
        self.assertEqual(params["gl"], ["US"])
        self.assertEqual(params["ceid"], ["US:en"])

    def test_fetch_twitter_search_uses_documented_auth_and_latest_query(self):
        payload = {"tweets": [{"id": "1", "text": "Launch", "author": {"userName": "maker"}}]}
        with patch("app.urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = json.dumps(payload).encode()
            stories = app.fetch_twitter_search("new model", "test-key")
        request = urlopen.call_args.args[0]
        params = app.urllib.parse.parse_qs(app.urllib.parse.urlparse(request.full_url).query)
        self.assertEqual(request.get_header("X-api-key"), "test-key")
        self.assertEqual(params["queryType"], ["Latest"])
        self.assertEqual(stories[0]["publisher"], "maker")

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
        self.assertEqual(result["verdict"], "uncertain")


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
                (story_id,platform,external_id,region,language,publisher,title,url,discovered_at)
                VALUES(?,?,?,?,?,?,?,?,?)""", (
                story_id, "Google News", "source-one", "global", "en", "Reuters", "A headline",
                "https://reuters.com/story", "2026-10-31T00:00:00Z",
            ))
        story = app.dashboard_state()["stories"][0]
        self.assertEqual(story["sources"][0]["region"], "global")
        self.assertEqual(story["sources"][0]["language"], "en")

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
            patch.object(app, "fetch_twitter_search", return_value=[]),
            patch.object(app, "assess_story", return_value={"verdict": "credible", "confidence": 0.95, "rationale": "多方报道"}),
            patch.object(app, "assess_story", return_value={
                "verdict": "uncertain", "confidence": 0.45, "relevance_score": 92, "urgency": "high",
                "summary_zh": "模型发布消息", "rationale": "需继续核验",
            }),
            patch.object(app, "send_notifications", return_value=True) as notify,
        ):
            app.run_scan()

        with app.connect_db() as db:
            story = db.execute("SELECT * FROM stories").fetchone()
            sources = db.execute("SELECT * FROM story_sources WHERE story_id=?", (story["id"],)).fetchall()
        self.assertEqual(story["verdict"], "uncertain")
        self.assertEqual(story["urgency"], "high")
        self.assertEqual(story["relevance_score"], 92)
        self.assertEqual(story["corroborations"], 2)
        self.assertEqual(story["notified"], 1)
        self.assertEqual(len(sources), 2)
        self.assertEqual({source["region"] for source in sources}, {"china", "global"})
        notify.assert_called_once()

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
