import json
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional


class OllamaClient:
    def __init__(
        self,
        base_url: str = "http://localhost:11434/api",
        timeout_seconds: int = 60,
        think: Optional[bool] = False,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        # Short live-chat replies do not need Qwen's long thinking trace.
        # None preserves the server/model default; False actually disables it.
        self.think = think

    def _post(self, path: str, payload: dict) -> dict:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Ollama request failed: {exc.code} {body}") from exc

    def embed(
        self,
        model: str,
        input_text: str = None,
        truncate: bool = True,
        **kwargs,
    ) -> List[List[float]]:
        if input_text is None and "input" in kwargs:
            input_text = kwargs.pop("input")
        payload = {"model": model, "input": input_text, "truncate": truncate}
        result = self._post("/embed", payload)
        return result.get("embeddings", [])

    def chat(
        self,
        model: str,
        system: Optional[str],
        messages: List[dict],
        format: str = "json",
        options: Optional[dict] = None,
        keep_alive: str = "5m",
    ) -> Dict[str, Any]:
        final_messages = list(messages)
        if system:
            final_messages = [{"role": "system", "content": system}] + final_messages

        payload = {
            "model": model,
            "messages": final_messages,
            "format": format,
            "stream": False,
            "keep_alive": keep_alive,
        }
        if self.think is not None:
            payload["think"] = self.think
        if options:
            payload["options"] = options

        result = self._post("/chat", payload)
        return {"content": result.get("message", {}).get("content", ""), "raw": result}
