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

from pathlib import Path
from collections import Counter
from google.cloud import speech

from Facebook_stream_input import start_streaming

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
PRODUCT_MODE = "clothing"
CONTEXT_DIR = RESOURCE_DIR / "speech_contexts" / PRODUCT_MODE

# =======================
# 載入 Speech Context
# =======================
def load_speech_context(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    return speech.SpeechContext(
        phrases=data["phrases"],
        boost=data.get("boost", 10.0)
    )

CONTEXTS = {}

for file in CONTEXT_DIR.glob("*.json"):
    CONTEXTS[file.stem] = load_speech_context(file)

SPEECH_CONTEXT_LIST = list(CONTEXTS.values())

# =======================
# Intent Rules
# =======================
INTENT_RULES = {
    "PRODUCT_TRADE_ACTION": CONTEXTS.get("base_context", speech.SpeechContext()).phrases,
    "PRODUCT_COLOR_DESC": CONTEXTS.get("color_context", speech.SpeechContext()).phrases,
    "PRODUCT_MATERIAL": CONTEXTS.get("fabric_context", speech.SpeechContext()).phrases,
    "PRODUCT_SIZE_SPEC": CONTEXTS.get("size_context", speech.SpeechContext()).phrases,
    "PRODUCT_STYLE_DESC": CONTEXTS.get("style_context", speech.SpeechContext()).phrases,
}

# =======================
# 多直播設定
# =======================
DEFAULT_LIVE_URL = os.environ.get(
    "STT_STREAM_URL",
    "https://www.facebook.com/shinekoreafashion/videos/2514776075612845?locale=zh_TW"
)

parser = argparse.ArgumentParser()
parser.add_argument("--url", default=DEFAULT_LIVE_URL)
parser.add_argument("--output", default=os.environ.get("STT_OUTPUT_JSONL", str(OUTPUT_DIR / "Text.jsonl")))
parser.add_argument("--stream-id", default="live_1")
parser.add_argument("--chrome-profile", default=os.environ.get("STT_CHROME_PROFILE", "Default"))
ARGS, _ = parser.parse_known_args()

# =======================
# Google STT Config
# =======================
TARGET_FS = 16000
STREAMING_LIMIT = 280

SERVICE_JSON = RESOURCE_DIR / "service_account.json"
if SERVICE_JSON.exists():
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(SERVICE_JSON)

client = speech.SpeechClient()

config = speech.RecognitionConfig(
    encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
    sample_rate_hertz=TARGET_FS,
    language_code="zh-TW",
    enable_automatic_punctuation=True,
    speech_contexts=SPEECH_CONTEXT_LIST
)

streaming_config = speech.StreamingRecognitionConfig(
    config=config,
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
def request_generator(audio_queue, start_time):

    while True:

        if time.time() - start_time > STREAMING_LIMIT:
            print("⏱ Streaming restart")
            return

        try:
            data = audio_queue.get(timeout=2)

            if data is None:
                continue
            
            yield speech.StreamingRecognizeRequest(
                audio_content=data
            )

        except queue.Empty:
            continue
# =======================
# Text Cleanup
# =======================
def clean_text(text: str):

    text = re.sub(r"\s+", " ", text)
    text = re.sub(r'(.{1,10}？)\1{2,}', r'\1', text)
    text = re.sub(r'(.)\1{3,}', r'\1', text)
    return text.strip()
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


def save_payload(log_fp, stream_id, text, confidence_scores):
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
    
    MAX_SENTENCE_SEC = 10
    SILENCE_GAP_SEC = 2.2
    INTERIM_STABLE_SEC = 2.5

    print(f"🚀 Starting {stream_id}")

    audio_queue = start_streaming(stream_id, url, ARGS.chrome_profile)

    log_fp = open(output_file, "a", encoding="utf-8")

    sentence_buffer = []
    last_final_text = ""
    confidence_scores = []   
    last_final_time = time.time()

    sentence_start_time = None
    last_interim_text = ""
    last_interim_change_time = time.time()
    last_saved_text = ""
    
    while True:

        start_time = time.time()

        requests = request_generator(audio_queue, start_time)

        responses = client.streaming_recognize(
            streaming_config,
            requests
        )

        try:

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
                                
                            sentence_buffer.append(text)
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

                        if (
                            last_interim_text
                            and not sentence_buffer
                            and (now - last_interim_change_time) >= INTERIM_STABLE_SEC
                            and normalize_for_compare(last_interim_text) != normalize_for_compare(last_saved_text)
                        ):
                            interim_scores = [confidence] if confidence else []
                            if save_payload(log_fp, stream_id, last_interim_text, interim_scores):
                                last_saved_text = last_interim_text
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

        except Exception as e:

            print(f"⚠️ [{stream_id}] restart: {e}")

            time.sleep(2)

# =======================
# 啟動所有直播
# =======================
threads = []

Path(ARGS.output).resolve().parent.mkdir(parents=True, exist_ok=True)

for stream_id, url in {ARGS.stream_id: ARGS.url}.items():

    t = threading.Thread(
        target=run_stt_pipeline,
        args=(stream_id, url, str(Path(ARGS.output).resolve())),
        daemon=True
    )

    t.start()

    threads.append(t)

# =======================
# 主執行緒保持運行
# =======================
while True:
    time.sleep(1)
