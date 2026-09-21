import threading
import queue
import json
import datetime
import time
import re
import os
import sys
import io
import argparse
import traceback
import warnings

warnings.filterwarnings("ignore", category=FutureWarning, module="google.api_core.python_version_support")

from pathlib import Path
from collections import Counter
from google.auth import api_key
from google.cloud import speech

from context_loader import load_speech_contexts
from Facebook_stream_input import get_stream_info, start_streaming
from audio_queue_stream import iter_audio_chunks

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


def get_app_dirs():
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS), Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent, Path(__file__).resolve().parent


RESOURCE_DIR, OUTPUT_DIR = get_app_dirs()

def write_crash_log(error):
    log_path = OUTPUT_DIR / "stt_error.log"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write("\n" + "=" * 80 + "\n")
        f.write(datetime.datetime.now().isoformat() + "\n")
        f.write(str(error) + "\n")
        f.write(traceback.format_exc() + "\n")
    print(f"STT failed. Error log: {log_path}", flush=True)

# =======================
# 商品模式
# =======================
PRODUCT_MODE = os.environ.get("STT_PRODUCT_MODE", "jewelry")

# =======================
# 載入Speech Context
# =======================
CONTEXTS, SPEECH_CONTEXT_LIST, INTENT_RULES = load_speech_contexts(PRODUCT_MODE)

# =======================
# 多直播設定
# =======================
DEFAULT_LIVE_URL = os.environ.get(
    "STT_STREAM_URL",
    "https://www.facebook.com/100063847871771/videos/1103597748670838?locale=zh_TW",
)

parser = argparse.ArgumentParser()
parser.add_argument("--url", default=DEFAULT_LIVE_URL)
parser.add_argument("--output", default=os.environ.get("STT_OUTPUT_JSONL", str(OUTPUT_DIR / "Text.jsonl")))
parser.add_argument("--stream-id", default="live_1")
parser.add_argument("--chrome-profile", default=os.environ.get("STT_CHROME_PROFILE", "Default"))
parser.add_argument("--probe", action="store_true", help="Print broadcaster metadata as JSON and exit")
ARGS, _ = parser.parse_known_args()

# =======================
# Google STT Config
# =======================
TARGET_FS = 16000
STREAMING_LIMIT = max(5, int(os.environ.get("STT_STREAMING_LIMIT_SECONDS", "280")))

API_KEY_FILE = RESOURCE_DIR / "stt_api_key.txt"
SERVICE_JSON = RESOURCE_DIR / "service_account.json"

api_key_value = os.environ.get("STT_GOOGLE_API_KEY", "").strip()
if not api_key_value and API_KEY_FILE.exists():
    api_key_value = API_KEY_FILE.read_text(encoding="utf-8").strip()

if api_key_value:
    client = speech.SpeechClient(credentials=api_key.Credentials(api_key_value))
else:
    if SERVICE_JSON.exists():
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(SERVICE_JSON)
    client = speech.SpeechClient()

def make_streaming_config():
    """Create a fresh config for every Google streaming session.

    Reusing the same protobuf config after the 280-second boundary can leave
    the next gRPC stream without its required first configuration message.
    """
    recognition_config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
        sample_rate_hertz=TARGET_FS,
        language_code="zh-TW",
        enable_automatic_punctuation=True,
        speech_contexts=[
            speech.SpeechContext(phrases=list(context.phrases), boost=context.boost)
            for context in SPEECH_CONTEXT_LIST
        ],
    )
    return speech.StreamingRecognitionConfig(
        config=recognition_config,
        interim_results=True,
    )

# =======================
# Intent Detection
# =======================
def detect_intents(text: str):

    scores = {}

    for intent, keywords in INTENT_RULES.items():
        scores[intent] = sum(1 for k in keywords if k in text)

    scores = {k: v for k, v in scores.items() if v > 0}

    if not scores:
        return "CHAT", []

    primary = max(scores, key=scores.get)
    secondary = [k for k in scores if k != primary]

    return primary, secondary

# =======================
# Entity 抽取
# =======================
def extract_entities(text: str, contexts: dict):

    entities = {
        "trade_action": [],
        "color": [],
        "material": [],
        "size": [],
        "style": []
    }

    if "base_context" in contexts:
        entities["trade_action"] = [
            p for p in contexts["base_context"].phrases if p in text
        ]

    if "color_context" in contexts:
        entities["color"] = [
            p for p in contexts["color_context"].phrases if p in text
        ]

    if "fabric_context" in contexts:
        entities["material"] = [
            p for p in contexts["fabric_context"].phrases if p in text
        ]

    if "size_context" in contexts:
        entities["size"] = [
            p for p in contexts["size_context"].phrases if p in text
        ]

    if "style_context" in contexts:
        entities["style"] = [
            p for p in contexts["style_context"].phrases if p in text
        ]

    return entities

# =======================
# Request Generator
# =======================
def request_generator(audio_queue, first_chunk):
    """Close an idle Google stream instead of waiting for an audio timeout."""
    for data in iter_audio_chunks(audio_queue, first_chunk, STREAMING_LIMIT):
        yield speech.StreamingRecognizeRequest(audio_content=data)
# =======================
# Text Cleanup
# =======================
def remove_repeated_phrases(text: str) -> str:
    """Collapse adjacent repeated phrases while preserving one occurrence."""
    pattern = re.compile(
        r"(?P<phrase>.{2,20}?)(?P<separator>[，。！？、；：,.!? ]*)(?P=phrase)"
    )

    previous = None
    while text != previous:
        previous = text
        text = pattern.sub(r"\g<phrase>", text)

    return text


def clean_text(text: str):

    text = re.sub(r"\s+", " ", text)
    text = re.sub(r'(.{1,10}？)\1{2,}', r'\1', text)
    text = re.sub(r'(.)\1{3,}', r'\1', text)
    text = remove_repeated_phrases(text)
    return text.strip()


def merge_overlapping_text(existing: str, incoming: str) -> str:
    """Merge final transcripts without duplicating their shared boundary."""
    if not existing:
        return incoming
    if not incoming:
        return existing

    if incoming in existing:
        return existing
    if existing in incoming:
        return incoming

    existing_normalized = normalize_for_compare(existing)
    incoming_normalized = normalize_for_compare(incoming)

    for overlap_length in range(
        min(len(existing_normalized), len(incoming_normalized)), 1, -1
    ):
        if existing_normalized[-overlap_length:] == incoming_normalized[:overlap_length]:
            return existing + incoming[overlap_length:]

    return f"{existing} {incoming}"


def append_final_text(sentence_buffer: list[str], text: str) -> None:
    """Append a final result after removing repeated or overlapping content."""
    if not text:
        return

    if not sentence_buffer:
        sentence_buffer.append(text)
        return

    sentence_buffer[-1] = merge_overlapping_text(sentence_buffer[-1], text)
    sentence_buffer[-1] = clean_text(sentence_buffer[-1])
# =======================
# Similar Sentence Check
# =======================
def is_similar(text1: str, text2: str):
    if not text1 or not text2:
        return False
    
    if text1 == text2:
        return True
    
    if text1 in text2 or text2 in text1:
        return True 
    
    return False


def normalize_for_compare(text: str) -> str:
    return re.sub(r"\s+", "", text.strip())


def save_payload(log_fp, stream_id, text, confidence_scores, live_title=None, live_uploader=None):
    current_sentence = clean_text(text)
    if len(normalize_for_compare(current_sentence)) < 6:
        return False

    intent, secondary = detect_intents(current_sentence)
    entities = extract_entities(current_sentence, CONTEXTS)
    avg_confidence = 0.0

    if confidence_scores:
        avg_confidence = sum(confidence_scores) / len(confidence_scores)

    payload = {
        "time": datetime.datetime.now().isoformat(),
        "stream_id": stream_id,
        "live_title": live_title,
        "live_uploader": live_uploader,
        "raw_text": current_sentence,
        "intent": intent,
        "secondary_intents": secondary,
        "confidence": round(avg_confidence, 3),
        "entities": entities
    }

    log_fp.write(json.dumps(payload, ensure_ascii=False) + "\n")
    log_fp.flush()
    print(f"\n[{stream_id}] saved: {current_sentence}", flush=True)
    return True
# =======================
# STT Pipeline
# =======================
def run_stt_pipeline(stream_id, url, output_file):
    try:
        _run_stt_pipeline(stream_id, url, output_file)
    except Exception as error:
        write_crash_log(error)
        # Do not leave an idle worker process alive after its only STT thread dies.
        os._exit(1)


def _run_stt_pipeline(stream_id, url, output_file):
    MAX_SENTENCE_SEC = 5
    SILENCE_GAP_SEC = 2.2
    INTERIM_STABLE_SEC = 2.5

    print(f"🚀 Starting {stream_id}")

    audio_queue, stream_info = start_streaming(stream_id, url, ARGS.chrome_profile)
    live_title = stream_info.get("title") or stream_info.get("fulltitle") or ""
    live_uploader = stream_info.get("uploader") or stream_info.get("channel")
    live_uploader_id = stream_info.get("uploader_id") or stream_info.get("channel_id")
    live_uploader_url = stream_info.get("uploader_url") or stream_info.get("channel_url")

    log_fp = open(output_file, "a", encoding="utf-8")
    # Publish stream identity immediately; transcript records may arrive much later.
    # The LLM ignores this record because it intentionally has no raw_text field.
    log_fp.write(json.dumps({
        "time": datetime.datetime.now().isoformat(),
        "event": "stream_metadata",
        "stream_id": stream_id,
        "live_title": live_title,
        "live_uploader": live_uploader,
        "live_uploader_id": live_uploader_id,
        "live_uploader_url": live_uploader_url,
    }, ensure_ascii=False) + "\n")
    log_fp.flush()

    sentence_buffer = []
    last_final_text = ""
    confidence_scores = []   
    last_final_time = time.time()

    sentence_start_time = None
    last_interim_text = ""
    last_interim_change_time = time.time()
    last_saved_text = ""
    last_wait_log = 0.0
    
    while True:

        try:
            first_chunk = audio_queue.get(timeout=2)
        except queue.Empty:
            if time.monotonic() - last_wait_log >= 15:
                print(f"⏳ [{stream_id}] waiting for live audio...", flush=True)
                last_wait_log = time.monotonic()
            continue
        if not first_chunk:
            continue

        requests = request_generator(audio_queue, first_chunk)

        try:
            # A brand-new configuration message must be the first message of
            # every restarted stream.  Keep creation and iteration inside the
            # retry block so transient Google errors never kill the worker.
            responses = client.streaming_recognize(
                config=make_streaming_config(),
                requests=requests,
            )

            for response in responses:

                for result in response.results:

                    alt = result.alternatives[0]

                    text = alt.transcript.strip()
                    text = clean_text(text)
                    
                    confidence = getattr(alt, "confidence", 0.0)

                    now = time.time()

                    if not text:
                        continue

                    # FINAL RESULT
                    if result.is_final:

                        if is_similar(text, last_final_text):
                            continue
                        
                        last_final_text = text
                        
                        duplicate = False
                        
                        for old in sentence_buffer:
                            if is_similar(text, old):
                                duplicate = True
                                break
                        
                        if not duplicate:
                            
                            if not sentence_buffer:
                                sentence_start_time = now
                                
                            append_final_text(sentence_buffer, text)
                            confidence_scores.append(confidence)
                            last_final_time = now
                            last_interim_text = ""
                            last_interim_change_time = now

                            print(f"\n[{stream_id}] 📝 {text}")

                    # INTERIM
                    else:
                        print(f"[{stream_id}] ⏳ {text}", end="\r")

                        if text != last_interim_text:
                            last_interim_text = text
                            last_interim_change_time = now

                    # FLUSH
                    if (
                        sentence_buffer 
                        and sentence_start_time is not None
                        and (now - sentence_start_time >= MAX_SENTENCE_SEC or now - last_final_time > SILENCE_GAP_SEC)
                    ):

                        current_sentence = " ".join(sentence_buffer)
                        current_sentence = clean_text(current_sentence)
                        
                        if len(normalize_for_compare(current_sentence)) < 6:
                            
                            sentence_buffer.clear()
                            confidence_scores.clear()
                            sentence_start_time = None
                            continue
                        
                        intent, secondary = detect_intents(current_sentence)

                        entities = extract_entities(
                            current_sentence,
                            CONTEXTS
                        )

                        avg_confidence = 0.0
                        
                        if confidence_scores:
                            avg_confidence = sum(confidence_scores) / len(confidence_scores)
                        
                        
                        payload = {
                            "time": datetime.datetime.now().isoformat(),
                            "stream_id": stream_id,
                            "live_title": live_title,
                            "live_uploader": live_uploader,
                            "raw_text": current_sentence,
                            "intent": intent,
                            "secondary_intents": secondary,
                            "confidence": round(avg_confidence, 3),
                            "entities": entities
                        }

                        log_fp.write(
                            json.dumps(
                                payload,
                                ensure_ascii=False
                            ) + "\n"
                        )

                        log_fp.flush()

                        print(
                            f"\n[{stream_id}] 💾 saved: {current_sentence}"
                        )

                        print(current_sentence)
                        last_saved_text = current_sentence
                        
                        sentence_buffer.clear()
                        confidence_scores.clear()
                        last_final_text = ""
                        sentence_start_time = None
                        last_final_time = now

            if sentence_buffer:
                save_payload(
                    log_fp, stream_id, " ".join(sentence_buffer),
                    confidence_scores, live_title, live_uploader,
                )
                sentence_buffer.clear()
                confidence_scores.clear()
                sentence_start_time = None
                last_final_text = ""

        except Exception as e:

            if sentence_buffer:
                save_payload(
                    log_fp, stream_id, " ".join(sentence_buffer),
                    confidence_scores, live_title, live_uploader,
                )
                sentence_buffer.clear()
                confidence_scores.clear()
                sentence_start_time = None
                last_final_text = ""

            print(f"⚠️ [{stream_id}] restart: {e}")

            time.sleep(1)
            continue

# =======================
# 啟動所有直播
# =======================
def main():
    if ARGS.probe:
        info = get_stream_info(ARGS.url, ARGS.chrome_profile)
        print(json.dumps({
            "live_title": info.get("title"),
            "live_uploader": info.get("uploader") or info.get("channel"),
            "live_uploader_id": info.get("uploader_id") or info.get("channel_id"),
            "live_uploader_url": info.get("uploader_url") or info.get("channel_url"),
        }, ensure_ascii=True), flush=True)
        return
    threads = []
    Path(ARGS.output).resolve().parent.mkdir(parents=True, exist_ok=True)
    for stream_id, url in {ARGS.stream_id: ARGS.url}.items():
        thread = threading.Thread(
            target=run_stt_pipeline,
            args=(stream_id, url, str(Path(ARGS.output).resolve())),
            daemon=True,
        )
        thread.start()
        threads.append(thread)

    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
