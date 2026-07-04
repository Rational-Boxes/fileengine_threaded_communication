"""Markdown → plaintext projection for `comments.body_text` (SPECIFICATION §4a).

Comments are stored as constrained Markdown; a stripped plaintext projection is
what feeds full-text search and (M3) embeddings, so matches are on words rather
than backticks or asterisks. This is a lightweight, dependency-free stripper —
good enough for indexing, not a full renderer (rendering is the SPA's job via
`marked` + `dompurify`).
"""
from __future__ import annotations

import re

_FENCE = re.compile(r"```.*?```", re.DOTALL)          # fenced code blocks
_INLINE_CODE = re.compile(r"`([^`]*)`")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")            # drop images entirely
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")           # [text](url) -> text
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_BLOCKQUOTE = re.compile(r"^\s{0,3}>\s?", re.MULTILINE)
_LIST_MARKER = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+", re.MULTILINE)
_EMPHASIS = re.compile(r"(\*\*|__|\*|_|~~)")            # bold/italic/strike markers
_WS = re.compile(r"[ \t]+")
_BLANK = re.compile(r"\n{3,}")


def to_plaintext(md: str) -> str:
    """Strip Markdown formatting to readable plaintext for indexing."""
    if not md:
        return ""
    t = _FENCE.sub(" ", md)
    t = _IMAGE.sub(" ", t)
    t = _LINK.sub(r"\1", t)
    t = _INLINE_CODE.sub(r"\1", t)
    t = _HEADING.sub("", t)
    t = _BLOCKQUOTE.sub("", t)
    t = _LIST_MARKER.sub("", t)
    t = _EMPHASIS.sub("", t)
    t = _WS.sub(" ", t)
    t = _BLANK.sub("\n\n", t)
    return t.strip()
