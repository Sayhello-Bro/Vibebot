import tempfile
import unittest
from pathlib import Path

from favorite_streamers import (
    facebook_profile_url,
    load_favorites,
    resolve_profile_url,
    save_favorites,
    upsert_favorite,
)


class FavoriteStreamerTests(unittest.TestCase):
    def test_homepage_is_taken_from_broadcaster_not_watch_page(self):
        self.assertEqual(
            resolve_profile_url(
                "https://www.facebook.com/watch/live/?v=123",
                "https://www.facebook.com/people/直播主/61585501362449/",
            ),
            "https://www.facebook.com/people/直播主/61585501362449",
        )
        self.assertEqual(
            facebook_profile_url("https://www.facebook.com/creator123/videos/123?fbclid=tracking"),
            "https://www.facebook.com/creator123",
        )
        self.assertEqual(facebook_profile_url("https://www.facebook.com/watch/live/?v=123"), "")
        self.assertEqual(
            resolve_profile_url("https://www.facebook.com/watch/live/?v=123", "", "https://www.facebook.com/100046914134771"),
            "https://www.facebook.com/100046914134771",
        )
        self.assertEqual(facebook_profile_url("https://www.facebook.com.evil.test/creator"), "")

    def test_recent_three_deduplicate_and_survive_restart(self):
        entries = []
        for number in range(1, 5):
            entries = upsert_favorite(entries, f"主播 {number}", f"https://www.facebook.com/creator{number}", number)
        self.assertEqual([item["name"] for item in entries], ["主播 4", "主播 3", "主播 2"])
        entries = upsert_favorite(entries, "主播 2 更新", "https://www.facebook.com/creator2", 5)
        self.assertEqual([item["name"] for item in entries], ["主播 2 更新", "主播 4", "主播 3"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "favorite_streamers.json"
            save_favorites(path, entries)
            self.assertEqual(load_favorites(path), entries)


if __name__ == "__main__":
    unittest.main()
