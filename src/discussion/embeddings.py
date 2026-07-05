"""Pluggable embedding providers for comment RAG indexing (SPECIFICATION §6).

Mirrors CSAI's provider approach. The default is an offline, deterministic **hash**
embedder (no network, stable across runs — ideal for dev/tests). Real deployments
point ``DISC_EMBEDDING_*`` at an OpenAI-compatible endpoint (Ollama's ``/v1`` or
OpenAI). All embedders produce fixed-width vectors of ``DISC_EMBEDDING_DIMENSION``
(which must match the schema's ``vector(N)`` column).
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import urllib.request
from typing import List, Sequence

log = logging.getLogger("discussion.embeddings")

_OLLAMA_DEFAULT = "http://localhost:11434/v1"


class HashEmbedder:
    """Deterministic offline embedder: hashes text into a normalized vector. Not
    semantic, but stable and dependency-free — the default, and what tests use."""
    provider = "hash"

    def __init__(self, dimension: int):
        self.dimension = int(dimension)

    def _vec(self, text: str) -> List[float]:
        v = [0.0] * self.dimension
        if not text:
            return v
        data = b""
        i = 0
        while len(data) < self.dimension * 4:
            data += hashlib.sha256(f"{i}:{text}".encode("utf-8")).digest()
            i += 1
        for j in range(self.dimension):
            v[j] = int.from_bytes(data[j * 4:j * 4 + 4], "big") / 2**32 - 0.5
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> List[float]:
        return self._vec(text)


class HTTPEmbedder:
    """OpenAI-compatible embeddings endpoint (Ollama ``/v1`` or OpenAI)."""
    def __init__(self, base_url: str, model: str, api_key: str, dimension: int, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.dimension = int(dimension)
        self.timeout = timeout
        self.provider = "http"

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        if not texts:
            return []
        payload = json.dumps({"model": self.model, "input": list(texts)}).encode("utf-8")
        req = urllib.request.Request(self.base_url + "/embeddings", data=payload, method="POST")
        req.add_header("Content-Type", "application/json")
        if self.api_key:
            req.add_header("Authorization", "Bearer " + self.api_key)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return [row["embedding"] for row in data["data"]]

    def embed_query(self, text: str) -> List[float]:
        return self.embed([text])[0]


def build_embedder(config):
    provider = (config.embedding_provider or "hash").lower()
    dim = config.embedding_dimension
    if provider in ("hash", "", "local", "fake", "offline"):
        return HashEmbedder(dim)
    base_url = config.embedding_base_url or (_OLLAMA_DEFAULT if provider == "ollama" else "")
    if not base_url:
        log.warning("embedding provider %s has no base_url — falling back to hash", provider)
        return HashEmbedder(dim)
    return HTTPEmbedder(base_url, config.embedding_model, config.embedding_api_key, dim)
