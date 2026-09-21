import json
import tempfile
import unittest
from pathlib import Path
from urllib.request import Request, urlopen

from profile_cards import (
    account_id_for_profile,
    allowed_avatar_url,
    load_card_state,
    next_profile_to_show,
    normalize_display_name,
    ProfileBridgeHandler,
    save_card_state,
    start_profile_bridge,
)


class ProfileCardTests(unittest.TestCase):
    def test_saved_cards_keep_stable_account_ids_and_deletions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "robot_cards.json"
            save_card_state(path, ["Default", "Profile 2"], {"Profile 2": "王小明"}, 3)
            state = load_card_state(path, ["Default", "Profile 1", "Profile 2"])
            self.assertEqual(state["profiles"], ["Default", "Profile 2"])
            self.assertEqual(state["names"]["Profile 2"], "王小明")
            self.assertEqual(account_id_for_profile("Profile 2"), "fb_account_003")
            self.assertEqual(state["next_profile_number"], 3)
            save_card_state(path, [], {}, 3)
            self.assertEqual(load_card_state(path, ["Default"])["profiles"], [])

    def test_closed_cards_return_before_new_accounts_in_original_order(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "robot_cards.json"
            save_card_state(
                path, ["Profile 3", "Default", "Profile 1"],
                {"Profile 2": "王小明"}, 4,
            )
            state = load_card_state(path, ["Default"])
            self.assertEqual(state["profiles"], ["Default", "Profile 1", "Profile 3"])
            self.assertEqual(state["names"]["Profile 2"], "王小明")
            self.assertEqual(next_profile_to_show(state["profiles"], state["next_profile_number"]), (2, True))
            restored = [*state["profiles"], "Profile 2"]
            save_card_state(path, restored, state["names"], state["next_profile_number"])
            reloaded = load_card_state(path, ["Default"])
            self.assertEqual(reloaded["profiles"], ["Default", "Profile 1", "Profile 2", "Profile 3"])
            self.assertEqual(next_profile_to_show(reloaded["profiles"], reloaded["next_profile_number"]), (4, False))
            self.assertEqual(next_profile_to_show([], reloaded["next_profile_number"]), (0, True))

    def test_identity_validation_rejects_generic_names_and_non_facebook_images(self):
        self.assertEqual(normalize_display_name(" Facebook "), "")
        self.assertEqual(normalize_display_name("  王   小明 "), "王 小明")
        self.assertTrue(allowed_avatar_url("https://scontent.xx.fbcdn.net/photo.jpg"))
        self.assertFalse(allowed_avatar_url("http://127.0.0.1/private"))
        self.assertFalse(allowed_avatar_url("https://fbcdn.net.evil.example/photo.jpg"))

    def test_local_bridge_accepts_only_facebook_origin(self):
        received = []
        server = start_profile_bridge(lambda *args: received.append(args) or True, port=0)
        try:
            address = f"http://127.0.0.1:{server.server_address[1]}/account_profile"
            payload = json.dumps({
                "account_id": "fb_account_003",
                "display_name": "王小明",
                "avatar_url": "https://scontent.xx.fbcdn.net/photo.jpg",
            }).encode("utf-8")
            request = Request(address, data=payload, headers={
                "Origin": "https://www.facebook.com", "Content-Type": "application/json",
            })
            with urlopen(request, timeout=2) as response:
                self.assertEqual(response.status, 200)
            self.assertEqual(received, [(
                "fb_account_003", "王小明", "https://scontent.xx.fbcdn.net/photo.jpg"
            )])
            other_origin = object.__new__(ProfileBridgeHandler)
            other_origin.headers = {"Origin": "https://other.example"}
            self.assertFalse(other_origin._allowed_origin())
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
