import datetime as dt
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "builder"))
import build_catalog as bc  # noqa: E402


def fixture(name):
    with open(os.path.join(HERE, "fixtures", name), encoding="utf-8") as handle:
        return handle.read()


class ParsingTests(unittest.TestCase):
    def test_views(self):
        self.assertEqual(bc.parse_views("953k views"), 953000)
        self.assertEqual(bc.parse_views("1.2M views"), 1200000)
        self.assertEqual(bc.parse_views("812 views"), 812)
        self.assertEqual(bc.parse_views("1 view"), 1)
        self.assertIsNone(bc.parse_views(""))

    def test_duration(self):
        self.assertEqual(bc.parse_duration("PT25M14S"), 1514)
        self.assertEqual(bc.parse_duration("PT1H2M3S"), 3723)
        self.assertIsNone(bc.parse_duration("soon"))
        self.assertIsNone(bc.parse_duration(None))

    def test_cards_from_load_more(self):
        cards = bc.parse_cards(json.loads(fixture("listing_page.json"))["data"]["html"])
        self.assertEqual(len(cards), 6)
        card = cards[0]
        self.assertEqual(card["id"], 12337)
        self.assertEqual(card["title"], "Of Souls, Symbols, and Sacraments")
        self.assertEqual(card["speakers"], [{"name": "Jeffrey R. Holland", "slug": "jeffrey-r-holland"}])
        self.assertEqual(card["date"], "1988-01-12")
        self.assertEqual(card["views"], 451000)
        self.assertEqual(card["type"], "Devotional")
        self.assertEqual(card["topics"], ["Chastity", "Marriage", "Morality"])
        self.assertTrue(card["mp3"].endswith("Holland_Jeffrey_1988_01_12.mp3"))
        self.assertTrue(card["pdf"].endswith(".pdf"))
        self.assertTrue(card["hasVideo"])
        self.assertTrue(card["hasText"])
        self.assertEqual(card["slug"], "souls-symbols-sacraments")

    def test_old_card_without_video_or_pdf(self):
        cards = bc.parse_cards(json.loads(fixture("listing_old.json"))["data"]["html"])
        card = next(c for c in cards if c["slug"] == "may-deceived")
        self.assertEqual(card["id"], 5868)
        self.assertFalse(card["hasVideo"])
        self.assertIsNone(card["pdf"])
        self.assertEqual(card["views"], 2000)
        self.assertIn("won't be", card["excerpt"])  # entities decoded

    def test_talk_page_new(self):
        page = bc.parse_talk_page(fixture("talk_new.html"))
        self.assertEqual(page["id"], 32521)
        self.assertEqual(page["dur"], 1514)
        self.assertEqual(page["videos"], [{"id": "xuLNpFIz-2s", "kind": "full"},
                                          {"id": "eyK1mhZfXMY", "kind": "short"}])

    def test_talk_page_old(self):
        page = bc.parse_talk_page(fixture("talk_old.html"))
        self.assertEqual(page["dur"], 34 * 60 + 55)
        self.assertEqual(page["videos"], [])
        self.assertIn("apostasy", page["keywords"])

    def test_class_list(self):
        speakers, topics, kind = bc.slugs_from_class_list(
            ["post-1", "speech", "event_type-devotional", "speaker-joni-l-koch", "topic-faith", "topic-knowledge"])
        self.assertEqual((speakers, topics, kind), (["joni-l-koch"], ["faith", "knowledge"], "devotional"))


class GainTests(unittest.TestCase):
    today = dt.date(2026, 11, 30)

    def test_uses_newest_snapshot_old_enough(self):
        snapshots = {"2026-11-20": {"1": [100, 1000]}, "2026-11-23": {"1": [110, 1100]}, "2026-11-28": {"1": [150, 1500]}}
        self.assertEqual(bc.gains({"1": [160, 1700]}, snapshots, self.today, 7), {"1": 50 + 600})

    def test_no_snapshot_old_enough(self):
        self.assertEqual(bc.gains({"1": [160, None]}, {"2026-11-28": {"1": [150, None]}}, self.today, 7), {})

    def test_new_talk_and_missing_youtube(self):
        snapshots = {"2026-11-23": {"1": [110, None]}}
        result = bc.gains({"1": [160, 900], "2": [5, None]}, snapshots, self.today, 7)
        self.assertEqual(result, {"1": 50})  # YouTube had no earlier number; talk 2 is new

    def test_never_negative_and_not_stretched(self):
        self.assertEqual(bc.gains({"1": [90, None]}, {"2026-11-23": {"1": [110, None]}}, self.today, 7), {"1": 0})
        self.assertEqual(bc.gains({"1": [90, None]}, {"2026-10-01": {"1": [10, None]}}, self.today, 7), {})


class DetailFetchTests(unittest.TestCase):
    today = dt.date(2026, 11, 30)

    def test_rules(self):
        cached = {"modified": "m1", "fetched": "2026-11-29", "dur": 100, "videos": [{"id": "x", "kind": "full"}]}
        self.assertTrue(bc.needs_detail_fetch(None, "m1", self.today, "2020-01-01"))
        self.assertFalse(bc.needs_detail_fetch(cached, "m1", self.today, "2020-01-01"))
        self.assertTrue(bc.needs_detail_fetch(cached, "m2", self.today, "2020-01-01"))
        recent_incomplete = {"modified": "m1", "fetched": "2026-11-20", "dur": None, "videos": []}
        self.assertTrue(bc.needs_detail_fetch(recent_incomplete, "m1", self.today, "2026-11-10"))
        self.assertFalse(bc.needs_detail_fetch(recent_incomplete, "m1", self.today, "2019-11-10"))


class RecordTests(unittest.TestCase):
    def test_record(self):
        card = bc.parse_cards(json.loads(fixture("listing_page.json"))["data"]["html"])[0]
        detail = {"dur": 2000, "videos": [{"id": "FULLFULLFUL", "kind": "full"}, {"id": "CLIPCLIPCLI", "kind": "short"}]}
        youtube = {"FULLFULLFUL": {"views": 5000, "likes": 200, "comments": 9},
                   "CLIPCLIPCLI": {"views": 90000, "likes": 1, "comments": 0}}
        record = bc.build_record(card, {"id": 12337}, detail, youtube, {7: 12, 30: None, 90: None})
        self.assertEqual(record["yt"], "FULLFULLFUL")
        self.assertEqual(record["ytViews"], 5000)       # the clip's views are kept apart
        self.assertEqual(record["clipViews"], 90000)
        self.assertEqual(record["g7"], 12)
        self.assertNotIn("g30", record)
        self.assertEqual(record["speaker"], "Jeffrey R. Holland")
        self.assertEqual(record["dur"], 2000)


if __name__ == "__main__":
    unittest.main()
