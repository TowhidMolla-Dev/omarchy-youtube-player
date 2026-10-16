import importlib.util
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("player", Path(__file__).parents[1] / "player.py")
player = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(player)

# Pin config that would otherwise be read from the ambient environment so the
# search tests stay deterministic regardless of the user's OMARCHY_YOUTUBE_* vars.
player.MAX_RESULTS = 8


class PlayerHelpersTest(unittest.TestCase):
    def test_accepts_youtube_video_and_short_url(self):
        self.assertTrue(player.valid_video_url("https://www.youtube.com/watch?v=abc123"))
        self.assertTrue(player.valid_video_url("https://youtu.be/abc123"))

    def test_rejects_non_youtube_url(self):
        self.assertFalse(player.valid_video_url("https://example.com/video"))
        self.assertFalse(player.valid_video_url("javascript:alert(1)"))

    def test_formats_durations(self):
        self.assertEqual(player.format_duration(65), "1:05")
        self.assertEqual(player.format_duration(3661), "1:01:01")
        self.assertEqual(player.format_duration(None), "")

    def test_state_uses_private_file_and_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            original = player.STATE_PATH
            try:
                player.STATE_PATH = Path(directory) / "state" / "state.json"
                player.write_state({"url": "https://youtube.com/watch?v=abc", "title": "video"})
                self.assertEqual(player.read_state()["title"], "video")
                self.assertEqual(stat.S_IMODE(player.STATE_PATH.stat().st_mode), 0o600)
                self.assertEqual(list(player.STATE_PATH.parent.glob(".state-*")), [])
                player.STATE_PATH.unlink()
                player.STATE_PATH.write_bytes(b"x" * (player.MAX_STATE_BYTES + 1))
                self.assertEqual(player.read_state(), {})
                player.STATE_PATH.unlink()
                target = Path(directory) / "attacker-target"
                target.write_text("{}", encoding="utf-8")
                player.STATE_PATH.symlink_to(target)
                self.assertEqual(player.read_state(), {})
            finally:
                player.STATE_PATH = original

    def test_bounded_process_rejects_oversized_output(self):
        command = [sys.executable, "-c", "import sys; sys.stdout.write('x' * 128)"]
        with self.assertRaises(player.ResponseLimitError):
            player._run_bounded(command, timeout=2, max_bytes=32)

    def test_search_caps_results_and_fields_before_qml(self):
        payload = {"entries": [{
            "id": "abcdefghijk",
            "ie_key": "Youtube",
            "title": "x" * 500,
            "channel": "c" * 500,
            "duration": 65,
            "thumbnail": "https://example.com/" + "x" * 1000,
        } for _ in range(20)]}
        with patch.object(player, "_run_bounded_text", return_value=(0, json.dumps(payload), "")):
            result = player.search("test", "en")
        self.assertEqual(len(result["results"]), 8)
        self.assertLessEqual(len(result["results"][0]["title"]), 180)
        self.assertLessEqual(len(result["results"][0]["channel"]), 100)
        self.assertLessEqual(len(result["results"][0]["thumbnail"]), 512)

    def test_search_skips_channels_and_playlists(self):
        payload = {"entries": [
            {"id": "UCSJ4gkVC6NrvII8umztf0Ow", "ie_key": "YoutubeTab", "title": "A channel"},
            {"id": "PLsomeplaylistid000000000000", "ie_key": "YoutubePlaylist", "title": "A playlist"},
            {"id": "dQw4w9WgXcQ", "ie_key": "Youtube", "title": "A video", "channel": "someone"},
        ]}
        with patch.object(player, "_run_bounded_text", return_value=(0, json.dumps(payload), "")):
            result = player.search("test", "en")
        self.assertEqual([item["id"] for item in result["results"]], ["dQw4w9WgXcQ"])

    def test_state_persists_queue_and_clamps_index(self):
        with tempfile.TemporaryDirectory() as directory:
            original = player.STATE_PATH
            try:
                player.STATE_PATH = Path(directory) / "state" / "state.json"
                queue = [
                    {"url": "https://www.youtube.com/watch?v=aaaaaaaaaaa", "title": "one"},
                    {"url": "https://www.youtube.com/watch?v=bbbbbbbbbbb", "title": "two"},
                ]
                player.write_state({"queue": queue, "queueIndex": 99})
                state = player.read_state()
                self.assertEqual(len(state["queue"]), 2)
                self.assertEqual(state["queueIndex"], 1)
            finally:
                player.STATE_PATH = original


if __name__ == "__main__":
    unittest.main()
