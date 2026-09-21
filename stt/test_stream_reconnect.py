import unittest
import queue
from unittest.mock import patch

import Facebook_stream_input as stream_input
from audio_queue_stream import iter_audio_chunks


class StreamReconnectTests(unittest.TestCase):
    def test_audio_stream_closes_quickly_when_live_audio_stalls(self):
        chunks = queue.Queue()
        self.assertEqual(
            list(iter_audio_chunks(chunks, b"first", 30, idle_timeout=0.05, poll_interval=0.01)),
            [b"first"],
        )

    def test_audio_stream_keeps_new_chunks(self):
        chunks = queue.Queue()
        chunks.put(b"second")
        self.assertEqual(
            list(iter_audio_chunks(chunks, b"first", 30, idle_timeout=0.05, poll_interval=0.01)),
            [b"first", b"second"],
        )

    def test_unavailable_chrome_cookies_are_not_retried_every_refresh(self):
        stream_input._cookieless_until.clear()
        with patch.object(
            stream_input,
            "_extract_stream_info",
            side_effect=[RuntimeError("cookie unavailable"), {"stream_url": "audio"}, {"stream_url": "audio"}],
        ) as extract:
            self.assertEqual(stream_input.get_stream_info("live", "Default")["stream_url"], "audio")
            self.assertEqual(stream_input.get_stream_info("live", "Default")["stream_url"], "audio")
            self.assertEqual(extract.call_args_list[0].args, ("live", "Default"))
            self.assertEqual(extract.call_args_list[1].args, ("live", None))
            self.assertEqual(extract.call_args_list[2].args, ("live", None))
        stream_input._cookieless_until.clear()

    def test_ffmpeg_does_not_retry_an_expired_404_url(self):
        with patch.object(stream_input.subprocess, "Popen") as popen:
            stream_input._start_ffmpeg("https://example.com/audio")
        command = popen.call_args.args[0]
        self.assertEqual(command[command.index("-reconnect_on_http_error") + 1], "5xx")

    def test_hls_audio_is_preferred_over_signed_dash_fragments(self):
        info = {
            "url": "https://example.com/live.mpd",
            "formats": [
                {"url": "https://example.com/live.mpd", "protocol": "http_dash_segments", "acodec": "aac"},
                {"url": "https://example.com/live.m3u8", "protocol": "m3u8_native", "acodec": "aac", "vcodec": "none", "abr": 128},
            ],
        }
        with patch.object(stream_input.yt_dlp, "YoutubeDL") as downloader:
            downloader.return_value.__enter__.return_value.extract_info.return_value = info
            result = stream_input._extract_stream_info("https://example.com/live")
        self.assertEqual(result["stream_url"], "https://example.com/live.m3u8")


if __name__ == "__main__":
    unittest.main()
