FB Live Auto Comment System

Real-time Speech Recognition and Automatic Live Interaction Assistant






A real-time live streaming assistant system that integrates Streaming Speech-to-Text (STT), Local LLM response generation, and Chrome Extension automation to automatically reply to Facebook Live content.

Overview

This project implements an automated live interaction pipeline:

Speech → STT → JSONL → LLM → API → Chrome Extension → Auto Comment

The system listens to livestream audio, converts speech into structured semantic data, generates responses using a language model, and automatically posts comments on Facebook Live.

Features
Real-time Speech Recognition
Google Streaming Speech-to-Text
Stereo Mix / VB-Cable audio capture
Interim / Final sentence detection
Stable sentence detection mechanism
Semantic Processing
Homophone correction
Misrecognition detection
Intent detection
Entity extraction
Local LLM Server
Reads latest speech JSONL
Generates contextual reply
Provides REST API interface
http://127.0.0.1:5000/latest_reply
Chrome Extension Automation
Polls reply API
Detects Facebook Live comment box
Auto-fills reply text
Simulates Enter key submission
One-click Execution

Launcher .exe automatically starts:

STT worker
LLM server
Chrome livestream page
System Architecture
User launches exe
        ↓
Launcher starts modules
        ↓
Streaming STT
        ↓
Sentence stabilization
        ↓
Semantic extraction
        ↓
JSONL output
        ↓
LLM server reads latest sentence
        ↓
Reply generation
        ↓
API response
        ↓
Chrome extension polling
        ↓
Auto comment submission
Project Structure
TEST1/
├── g/
│   ├── WASAPI_test.py
│   ├── speech_contexts/
│   └── service_account.json
│
├── release/
│   ├── FB_Live_Auto_Comment.exe
│   ├── stt_worker.exe
│   ├── llm_server.exe
│   ├── test_llm.py
│   ├── stt_annotated_output.jsonl
│   └── live_transcript.txt
│
├── fb-live-comment-extension/
│   ├── manifest.json
│   ├── content.js
│   └── background.js
│
└── launcher.py
Installation
Requirements

Python 3.10+

Install dependencies:

pip install google-cloud-speech
pip install sounddevice
pip install numpy
pip install flask
Running the System
Option 1 — Run Executable (Recommended)

Double click:

FB_Live_Auto_Comment.exe

This launches:

STT worker
LLM server
Chrome livestream page
Option 2 — Run Manually (Developer Mode)

Start STT:

python WASAPI_test.py

Start LLM server:

python test_llm.py

Start Chrome Extension:

Load unpacked extension
API Interface
Get Latest Reply
GET http://127.0.0.1:5000/latest_reply

Response:

{
    "source_text": "...",
    "reply": "..."
}
Speech Processing Pipeline
Audio Input
    ↓
Chunk segmentation
    ↓
Streaming STT
    ↓
Sentence stabilization
    ↓
Homophone correction
    ↓
Intent detection
    ↓
Entity extraction
    ↓
JSONL output
Output Files
live_transcript.txt

Stores latest real-time speech recognition result

Example:

今天幫我下單三件XL
stt_annotated_output.jsonl

Stores structured semantic speech data

Example:

{
  "resolved_text": "今天幫我下單三件XL",
  "intent": "PRODUCT_TRADE_ACTION"
}
Chrome Extension Workflow
Load livestream page
        ↓
Poll API every few seconds
        ↓
Receive reply text
        ↓
Insert comment
        ↓
Submit automatically
System Workflow
User launches exe
        ↓
System captures livestream audio
        ↓
Speech converted into text
        ↓
Semantic analysis
        ↓
LLM generates reply
        ↓
Extension posts comment
Authors

Project Team Members

Speech Recognition Module
LLM Server Module
Chrome Extension Automation Module
System Integration Launcher Module

(可填你們名字)

Future Improvements
Multi-speaker detection
Context memory enhancement
Adaptive reply strategy
Multi-platform livestream support
GPT-based semantic enhancement
