#!/usr/bin/env python3
"""Самотесты без сети: чистые функции watcher.py.

    python3 tests/selftest.py

Проверяет то, что ломается тихо: очистку текста, разбиение сообщений,
конвертацию HTML в markdown, разбор JSON ответа переводчика и логику
«перевод реально состоялся».
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import watcher as w  # noqa: E402


class TestCleanText(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(w.clean_text(None), "")
        self.assertEqual(w.clean_text(""), "")

    def test_entities_and_markup(self):
        self.assertEqual(w.clean_text("a &amp; b"), "a & b")
        self.assertEqual(w.clean_text("<b>hi</b>"), "hi")

    def test_markdown_characters_are_neutralised(self):
        # пайп и звёздочки ломали бы таблицы и выделение в дайджесте
        self.assertEqual(w.clean_text("a|b"), "a/b")
        self.assertEqual(w.clean_text("a*b"), "ab")
        self.assertEqual(w.clean_text("a_b"), "a b")

    def test_truncation(self):
        out = w.clean_text("x" * 500, 50)
        self.assertLessEqual(len(out), 50)
        self.assertTrue(out.endswith("…"))

    def test_unescapes_html_which_breaks_telegram_parse_mode(self):
        # &amp; -> & , иначе Telegram отклонит сообщение в HTML-режиме
        self.assertEqual(w.clean_text("a &amp; b"), "a & b")
        self.assertNotIn("&amp;", w.clean_text("a &amp; b"))


class TestEsc(unittest.TestCase):
    def test_escapes_angle_brackets(self):
        self.assertEqual(w.esc("<b>"), "&lt;b&gt;")

    def test_none_is_empty(self):
        self.assertEqual(w.esc(None), "")


class TestNormRepo(unittest.TestCase):
    def test_extracts_owner_repo(self):
        self.assertEqual(w.norm_repo("https://github.com/Owner/Repo"), "owner/repo")

    def test_strips_git_suffix(self):
        self.assertEqual(w.norm_repo("https://github.com/Owner/Repo.git"), "owner/repo")

    def test_ignores_special_paths(self):
        self.assertIsNone(w.norm_repo("https://github.com/sponsors/Owner"))

    def test_none(self):
        self.assertIsNone(w.norm_repo(None))


class TestIsTranslated(unittest.TestCase):
    def test_russian_output_accepted(self):
        self.assertTrue(w.is_translated("OpenCode plugin", "Плагин OpenCode"))

    def test_unchanged_english_rejected(self):
        # именно этот случай ловится, чтобы не кэшировать оригинал
        self.assertFalse(w.is_translated("OpenCode plugin", "OpenCode plugin"))

    def test_empty_rejected(self):
        self.assertFalse(w.is_translated("OpenCode plugin", ""))

    def test_source_without_latin_always_ok(self):
        self.assertTrue(w.is_translated("⭐⭐⭐", "⭐⭐⭐"))


class TestCacheKey(unittest.TestCase):
    def test_stable(self):
        a = w.translation_cache_key("text", "ru", 150)
        b = w.translation_cache_key("text", "ru", 150)
        self.assertEqual(a, b)

    def test_changes_with_params(self):
        base = w.translation_cache_key("text", "ru", 150)
        self.assertNotEqual(base, w.translation_cache_key("text", "en", 150))
        self.assertNotEqual(base, w.translation_cache_key("text", "ru", 200))
        self.assertNotEqual(base, w.translation_cache_key("other", "ru", 150))


class TestParseJsonList(unittest.TestCase):
    def test_plain_array(self):
        self.assertEqual(w.parse_json_list('[{"n":1,"ru":"а"}]'), [{"n": 1, "ru": "а"}])

    def test_fenced_block(self):
        got = w.parse_json_list('```json\n[{"n":1,"ru":"а"}]\n```')
        self.assertEqual(got, [{"n": 1, "ru": "а"}])

    def test_text_around_array(self):
        got = w.parse_json_list('Готово:\n[{"n":1,"ru":"а"}]\nСпасибо!')
        self.assertEqual(got, [{"n": 1, "ru": "а"}])

    def test_object_with_list_value(self):
        got = w.parse_json_list('{"translations":[{"n":1,"ru":"а"}]}')
        self.assertEqual(got, [{"n": 1, "ru": "а"}])

    def test_garbage_returns_empty(self):
        self.assertEqual(w.parse_json_list("не json вовсе"), [])


class TestHtmlToMd(unittest.TestCase):
    def test_links(self):
        self.assertEqual(
            w.html_to_md('<a href="https://x.dev/a">proj</a>'),
            "proj (https://x.dev/a)",
        )

    def test_no_tags_left(self):
        out = w.html_to_md("<b>Жирно</b> и <i>курсив</i> <code>код</code>")
        self.assertNotIn("<", out)
        self.assertIn("**Жирно**", out)
        self.assertIn("_курсив_", out)
        self.assertIn("`код`", out)

    def test_entities(self):
        self.assertIn("&", w.html_to_md("a &amp; b"))


class TestSplitMessage(unittest.TestCase):
    def test_short_message_is_one_chunk(self):
        self.assertEqual(len(w.split_message("привет")), 1)

    def test_long_message_is_split(self):
        blocks = "\n".join(f"• плагин {i} " + "x" * 300 for i in range(60))
        chunks = w.split_message(blocks)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 3900)

    def test_no_content_lost(self):
        blocks = "\n".join(f"строка {i}" for i in range(2000))
        joined = "\n".join(w.split_message(blocks))
        self.assertEqual(joined, blocks)


class TestClassify(unittest.TestCase):
    def test_known_kinds(self):
        self.assertIn("Плагин", w.classify("plugin"))
        self.assertIn("Тема", w.classify("theme"))

    def test_unknown_defaults_to_plugin(self):
        self.assertIn("Плагин", w.classify("что-то новое"))
        self.assertIn("Плагин", w.classify(None))


class TestDatabase(unittest.TestCase):
    """Схема создаётся с нуля и переживает повторный запуск."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "t.db")
        self.conn = w.db_connect(self.path)
        # формат реально выдают source_github() и source_npm()
        self.items = [
            {
                "key": "gh:owner/tool",
                "title": "tool",
                "kind": "plugin",
                "repo": "owner/tool",
                "url": "https://github.com/owner/tool",
                "description": "A tool for OpenCode",
                "stars": 7,
                "sources": "github",
            },
            {
                "key": "npm:tool",
                "title": "tool",
                "kind": "plugin",
                "npm": "tool",
                "repo": "owner/tool",
                "url": "https://www.npmjs.com/package/tool",
                "description": "A tool for OpenCode",
                "version": "1.2.3",
                "sources": "npm",
            },
        ]

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_tables_created(self):
        names = {
            row[0]
            for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for table in ("items", "translations", "runs", "meta"):
            self.assertIn(table, names)

    def test_store_is_idempotent(self):
        w.store_items(self.conn, self.items)
        again = w.store_items(self.conn, self.items)
        self.assertEqual(again[0], 0)  # ничего нового
        self.assertEqual(again[1], 0)  # и обновлений
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0], 2)

    def test_merge_deduplicates_github_and_npm(self):
        merged = w.merge_items(self.items)
        self.assertEqual(len(merged), 1, "репозиторий и npm-пакет должны склеиться")
        self.assertEqual(merged[0]["version"], "1.2.3", "версия должна пережить склейку")

    def test_merge_keeps_unrelated_items(self):
        other = dict(self.items[1], key="npm:another", npm="another", repo="owner/other")
        self.assertEqual(len(w.merge_items(self.items + [other])), 2)

    def test_version_bump_is_counted(self):
        w.store_items(self.conn, self.items)
        bumped = [dict(self.items[1], version="2.0.0")]
        new_count, upd_count = w.store_items(self.conn, bumped)
        self.assertEqual((new_count, upd_count), (0, 1))
        self.assertEqual(
            self.conn.execute("SELECT version FROM items WHERE key='npm:tool'").fetchone()[0],
            "2.0.0",
        )

    def test_stars_and_version_changes_both_counted(self):
        w.store_items(self.conn, self.items)
        bumped = [dict(self.items[0], stars=10), dict(self.items[1], version="2.0.0")]
        new_count, upd_count = w.store_items(self.conn, bumped)
        self.assertEqual((new_count, upd_count), (0, 2))

    def test_translation_cache_roundtrip(self):
        key = w.translation_cache_key("OpenCode plugin", "ru", 150)
        self.conn.execute(
            "INSERT OR REPLACE INTO translations(k, text, model, created) VALUES(?,?,?,?)",
            (key, "Плагин OpenCode", "test", w.iso(w.now_utc())),
        )
        self.conn.commit()
        got = self.conn.execute("SELECT text FROM translations WHERE k=?", (key,)).fetchone()
        self.assertEqual(got[0], "Плагин OpenCode")


class TestMcpRelevance(unittest.TestCase):
    """Правила отбора MCP-серверов под OpenCode: точность важнее полноты."""

    def test_key_regex_accepts_slugs(self):
        for text in ("owner/opencode-mcp", "opencode_mcp_manager", "mcp-opencode", "opencode.mcp"):
            self.assertTrue(w.MCP_KEY_RE.search(text), text)

    def test_key_regex_rejects_plain_opencode(self):
        for text in ("owner/opencode-plugin", "opencode-sdk-dotnet", "claude-code"):
            self.assertFalse(w.MCP_KEY_RE.search(text), text)

    def test_desc_regex_accepts_purpose_phrases(self):
        for text in (
            "MCP server for OpenCode AI — 70 tools",
            "An OpenCode MCP server for the browser",
            "Zero-dependency MCP server to drive opencode",
            "MCP para OpenCode",
        ):
            self.assertTrue(w.MCP_DESC_RE.search(text), text)

    def test_desc_regex_rejects_incidental_mentions(self):
        # «поддерживает OpenCode среди других клиентов» — не наш сервер
        for text in (
            "Unofficial typed .NET SDK for the opencode HTTP API (preview) with MCP support",
            "Run Claude Code, Codex, Cursor and OpenCode from one tmux workspace",
        ):
            self.assertFalse(w.MCP_DESC_RE.search(text), text)


class TestSections(unittest.TestCase):
    """Плагины и MCP — разные дайджесты, оба по популярности."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = w.db_connect(os.path.join(self.tmp.name, "t.db"))
        self.cfg = {
            "digest_title": "🆕 Плагины",
            "filters": {"min_stars": 0, "max_new": 40, "window_days": 8, "star_jump": 10},
            "mcp": {
                "enabled": True,
                "digest_title": "🔌 MCP",
                "filters": {"min_stars": 0, "max_new": 40, "window_days": 8, "star_jump": 10},
            },
            "translate": {"enabled": False},
        }
        rows = [
            {"key": "gh:a/plugin-low", "title": "plugin-low", "kind": "plugin", "repo": "a/plugin-low",
             "stars": 5, "description": "low stars plugin", "sources": "github"},
            {"key": "gh:a/plugin-top", "title": "plugin-top", "kind": "plugin", "repo": "a/plugin-top",
             "stars": 500, "description": "top plugin", "sources": "github"},
            {"key": "gh:a/mcp-huge", "title": "mcp-huge", "kind": "mcp", "repo": "a/mcp-huge",
             "stars": 9999, "description": "huge mcp", "sources": "github:mcp"},
            {"key": "gh:a/mcp-small", "title": "mcp-small", "kind": "mcp", "repo": "a/mcp-small",
             "stars": 1, "description": "small mcp", "sources": "github:mcp"},
            # старая запись без явного kind — должна считаться плагином
            {"key": "gh:a/legacy", "title": "legacy", "repo": "a/legacy", "kind": None,
             "stars": 42, "description": "legacy row", "sources": "github"},
        ]
        w.store_items(self.conn, rows)
        for row in self.conn.execute("SELECT key FROM items").fetchall():
            self.conn.execute("UPDATE items SET announced=0 WHERE key=?", (row[0],))
        self.conn.execute("UPDATE meta SET v=? WHERE k='last_digest_at'", ("2000-01-01T00:00:00+00:00",))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_plugin_digest_excludes_mcp(self):
        text, keys = w.build_digest(self.conn, self.cfg, do_translate=False, section="plugins")
        self.assertNotIn("mcp-huge", text)
        self.assertNotIn("mcp-small", text)
        self.assertEqual(set(keys), {"gh:a/plugin-low", "gh:a/plugin-top", "gh:a/legacy"})

    def test_mcp_digest_excludes_plugins(self):
        text, keys = w.build_digest(self.conn, self.cfg, do_translate=False, section="mcp")
        self.assertNotIn("plugin-top", text)
        self.assertEqual(set(keys), {"gh:a/mcp-huge", "gh:a/mcp-small"})

    def test_sections_do_not_share_keys(self):
        _, pkeys = w.build_digest(self.conn, self.cfg, do_translate=False, section="plugins")
        _, mkeys = w.build_digest(self.conn, self.cfg, do_translate=False, section="mcp")
        self.assertEqual(set(pkeys) & set(mkeys), set())

    def test_sorted_by_popularity(self):
        text, _ = w.build_digest(self.conn, self.cfg, do_translate=False, section="plugins")
        # по популярности: 500 → 42 → 5
        order = [text.index("plugin-top"), text.index("legacy"), text.index("plugin-low")]
        self.assertEqual(order, sorted(order), "плагины должны идти по звёздам: 500 → 42 → 5")

        mtext, _ = w.build_digest(self.conn, self.cfg, do_translate=False, section="mcp")
        self.assertLess(mtext.index("mcp-huge"), mtext.index("mcp-small"))

    def test_legacy_row_without_kind_goes_to_plugin_digest(self):
        _, pkeys = w.build_digest(self.conn, self.cfg, do_translate=False, section="plugins")
        self.assertIn("gh:a/legacy", pkeys)

    def test_titles_use_own_digest_title(self):
        ptext, _ = w.build_digest(self.conn, self.cfg, do_translate=False, section="plugins")
        mtext, _ = w.build_digest(self.conn, self.cfg, do_translate=False, section="mcp")
        self.assertIn("🆕 Плагины", ptext)
        self.assertIn("🔌 MCP", mtext)

    def test_mcp_has_no_npm_snippet(self):
        # «⚙️ Установка в opencode.json» с именами пакетов для MCP бессмысленно
        _, mkeys = w.build_digest(self.conn, self.cfg, do_translate=False, section="mcp")
        self.assertTrue(mkeys)
        npm_row = {
            "key": "npm:opencode-mcp", "title": "opencode-mcp", "kind": "mcp", "npm": "opencode-mcp",
            "stars": 0, "version": "1.0.0", "description": "MCP server for OpenCode", "sources": "npm:mcp",
        }
        w.store_items(self.conn, [npm_row])
        self.conn.execute("UPDATE items SET announced=0 WHERE key='npm:opencode-mcp'")
        self.conn.commit()
        mtext, _ = w.build_digest(self.conn, self.cfg, do_translate=False, section="mcp")
        self.assertNotIn("Установка в opencode.json", mtext)

    def test_empty_section_says_nothing_found(self):
        empty_cfg = json.loads(json.dumps(self.cfg))
        for section in ("plugins", "mcp"):
            self.conn.execute("UPDATE items SET announced=1")
            self.conn.commit()
            text, keys = w.build_digest(self.conn, empty_cfg, do_translate=False, section=section)
            self.assertEqual(keys, [])
            self.assertIn("не найдено", text)


class TestMergeDoesNotMixKinds(unittest.TestCase):
    def test_mcp_and_plugin_with_same_name_stay_separate(self):
        plugin = {"key": "npm:shared", "title": "shared", "npm": "shared", "kind": "plugin", "repo": "o/shared"}
        mcp = {"key": "gh:o/shared-mcp", "title": "shared-mcp", "repo": "o/shared-mcp", "kind": "mcp"}
        merged = w.merge_items([plugin, mcp])
        self.assertEqual(len(merged), 2, "плагин и MCP не должны склеиваться")

    def test_same_kind_still_merges(self):
        plugin = {"key": "npm:tool", "title": "tool", "npm": "tool", "kind": "plugin", "repo": "o/tool"}
        repo = {"key": "gh:o/tool", "title": "tool", "repo": "o/tool", "kind": "plugin"}
        self.assertEqual(len(w.merge_items([plugin, repo])), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
