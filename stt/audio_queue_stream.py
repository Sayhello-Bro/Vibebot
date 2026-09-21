"""Keep speech streaming aligned with live audio rather than idle network time."""

import queue
import time


def iter_audio_chunks(audio_queue, first_chunk, max_duration, idle_timeout=2.5, poll_interval=0.5):
    started_at = time.monotonic()
    last_audio_at = started_at
    yield first_chunk

    while time.monotonic() - started_at < max_duration:
        try:
            chunk = audio_queue.get(timeout=poll_interval)
        except queue.Empty:
            if time.monotonic() - last_audio_at >= idle_timeout:
                return
            continue
        if chunk:
            last_audio_at = time.monotonic()
            yield chunk
