# FB Live Auto Comment System
Real-time Speech Recognition and Automatic Facebook Live Interaction Assistant

![Python](https://img.shields.io/badge/Python-3.10+-blue)
![Chrome Extension](https://img.shields.io/badge/Chrome-Extension-green)
![License](https://img.shields.io/badge/License-MIT-green)

## Overview

This project implements a real-time automatic interaction assistant for Facebook Live streaming.

The system listens to livestream audio, converts speech into structured semantic text, generates responses using a local LLM server, and automatically posts replies through a Chrome Extension.

Pipeline:
```
Speech → STT → JSONL → LLM Server → API → Chrome Extension → Auto Comment
```
---

# Features

## Real-Time Speech Recognition (Streaming STT)

Supports:

- Google Streaming Speech-to-Text
- Stereo Mix / VB-Cable audio capture
- Interim and Final transcript detection
- Stable sentence segmentation
- Continuous speech monitoring

---

## Semantic Processing Module

Includes:

- Homophone correction
- Misrecognition detection
- Intent detection
- Entity extraction
- Context-aware sentence stabilization

---

## Local LLM Reply Server

Responsibilities:

- Reads latest speech JSONL records
- Extracts semantic meaning
- Generates contextual reply
- Provides REST API interface

API endpoint:


http://127.0.0.1:5000/latest_reply


Example response:


{
"source_text": "...",
"reply": "..."
}


---

## Chrome Extension Auto Comment Module

Extension automatically:

1. Polls reply API periodically
2. Detects Facebook Live comment input box
3. Inserts generated reply
4. Submits comment automatically

---

## One-Click System Execution

Run:


FB_Live_Auto_Comment.exe


Automatically launches:

- STT worker
- LLM reply server
- Chrome livestream page

---

# System Architecture

```
User launches exe
↓
Launcher starts modules
↓
Streaming STT captures audio
↓
Sentence stabilization
↓
Semantic extraction
↓
JSONL structured output
↓
LLM server reads latest sentence
↓
Reply generation
↓
REST API response
↓
Chrome Extension polling
↓
Automatic comment submission
```

---

# Project Structure


```
TEST1/
├── g/
│ ├── WASAPI_test.py
│ ├── speech_contexts/
│ └── service_account.json
│
├── release/
│ ├── FB_Live_Auto_Comment.exe
│ ├── stt_worker.exe
│ ├── llm_server.exe
│ ├── test_llm.py
│ ├── stt_annotated_output.jsonl
│ └── live_transcript.txt
│
├── fb-live-comment-extension/
│ ├── manifest.json
│ ├── content.js
│ └── background.js
│
└── launcher.py
```

---

# Requirements

Python version:


Python 3.10+


Install dependencies:

```
pip install google-cloud-speech
pip install sounddevice
pip install numpy
pip install flask
```

---

# Running the System

## Option 1 (Recommended)

Run executable:


FB_Live_Auto_Comment.exe


This automatically starts:

- STT worker
- LLM server
- Chrome livestream page

---

## Option 2 (Developer Mode)

Start STT module:


python WASAPI_test.py


Start LLM reply server:


python test_llm.py


Load Chrome Extension manually:


Load unpacked extension


---

# API Interface

## Get Latest Generated Reply

Request:


GET http://127.0.0.1:5000/latest_reply


Response format:


{
"source_text": "...",
"reply": "..."
}


---

# Speech Processing Pipeline

```
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
JSONL structured output
```

---

# Output Files

## live_transcript.txt

Stores latest real-time speech transcript

Example:


Help me place three XL orders


---

## stt_annotated_output.jsonl

Stores structured semantic speech results

Example:


{
"resolved_text": "Help me place three XL orders",
"intent": "PRODUCT_TRADE_ACTION"
}


---

# Chrome Extension Workflow

```
Open livestream page
↓
Poll API periodically
↓
Receive reply text
↓
Insert into comment box
↓
Submit automatically
```

---

# Full System Workflow

```
User launches exe
↓
System captures livestream audio
↓
Speech converted to text
↓
Semantic analysis applied
↓
LLM generates reply
↓
Extension posts comment automatically
```

---

# Module Responsibilities

This project contains four core modules:

### Speech Recognition Module

Handles real-time audio capture and speech-to-text conversion.

### LLM Reply Server Module

Processes semantic text and generates automated responses.

### Chrome Extension Module

Fetches generated replies and posts comments automatically.

### Launcher Module

Integrates and starts the entire pipeline with one click.

---

# Future Improvements

Potential upgrades:

- Apply knowledge distillation techniques to transfer knowledge from large language models to smaller models, enabling more natural and realistic conversational responses while reducing computational cost.
- Reduce the frequency of model invocation as much as possible to improve system efficiency and response speed.
- Explore alternative audio capture methods, such as directly connecting to livestream data streams, integrating third-party streaming tools, or developing dedicated streaming-processing modules, to support multiple bots monitoring multiple livestream channels simultaneously.
- Further refine the API design to ensure it can be triggered reliably for every new input event, and improve the overall system interface and visual presentation for better usability and user experience.

---

