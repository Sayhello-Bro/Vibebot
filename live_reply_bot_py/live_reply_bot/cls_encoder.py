"""Extract actual CLS hidden states, independently of the example embedder."""

from pathlib import Path

from .cls_cache import EncoderSpec


DEFAULT_CLS_MODEL = "BAAI/bge-small-zh-v1.5"
DEFAULT_CLS_REVISION = "7999e1d3359715c523056ef9478215996d62a620"


class CLSEncoder:
    def __init__(
        self,
        model_id=DEFAULT_CLS_MODEL,
        revision=None,
        cache_dir=None,
        device="cpu",
        local_files_only=False,
    ):
        if revision is None:
            if model_id != DEFAULT_CLS_MODEL:
                raise ValueError("An explicit revision is required for a custom CLS model")
            revision = DEFAULT_CLS_REVISION
        try:
            import torch
            from transformers import AutoModel, AutoTokenizer
        except ImportError as exc:
            raise RuntimeError(
                "CLS memory requires torch and transformers; install requirements-cls.txt"
            ) from exc

        self._torch = torch
        model_cache = Path(cache_dir) if cache_dir else (
            Path(__file__).resolve().parents[1] / ".cache-cls" / "models"
        )
        kwargs = dict(
            revision=revision,
            cache_dir=str(model_cache),
            local_files_only=local_files_only,
            trust_remote_code=False,
        )
        self.tokenizer = AutoTokenizer.from_pretrained(model_id, **kwargs)
        self.model = AutoModel.from_pretrained(model_id, **kwargs).to(device)
        self.model.eval()
        self.model.requires_grad_(False)
        if self.tokenizer.cls_token_id is None:
            raise ValueError("This tokenizer has no CLS token")
        self.device = device
        self.max_tokens = min(
            self.model.config.max_position_embeddings,
            self.tokenizer.model_max_length,
        )
        resolved_revision = getattr(self.model.config, "_commit_hash", None) or revision
        self.spec = EncoderSpec(
            model_id=model_id,
            revision=resolved_revision,
            dimension=self.model.config.hidden_size,
        )

    def encode(self, text):
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Cannot encode an empty sentence")
        inputs = self.tokenizer(text, return_tensors="pt", truncation=False)
        if inputs["input_ids"].shape[1] > self.max_tokens:
            raise ValueError(
                f"Sentence exceeds {self.max_tokens} tokens; split the input explicitly"
            )
        if inputs["input_ids"][0, 0].item() != self.tokenizer.cls_token_id:
            raise ValueError("First token is not CLS; refusing to mislabel the embedding")
        inputs = {name: value.to(self.device) for name, value in inputs.items()}
        with self._torch.no_grad():
            output = self.model(**inputs)
            cls = output.last_hidden_state[0, 0, :]
        # Store the raw last-layer CLS, not pooler_output or mean pooling.
        return cls.detach().float().cpu().tolist()
