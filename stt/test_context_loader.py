from context_loader import load_speech_contexts


if __name__ == "__main__":
    try:
        contexts, speech_context_list, intent_rules = load_speech_contexts("clothing")

        print("CONTEXTS keys:")
        for key in contexts.keys():
            print(f"- {key}")

        print("\nSPEECH_CONTEXT_LIST phrases:")
        for idx, speech_context in enumerate(speech_context_list, start=1):
            print(f"[{idx}] {speech_context.phrases}")

        print("\nINTENT_RULES:")
        for intent, phrases in intent_rules.items():
            print(f"- {intent}: {phrases}")

    except Exception as exc:
        print(f"[ERROR] Failed to load speech contexts from MongoDB: {exc}")
        raise
