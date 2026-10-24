"""
backend.rag.store
=================

Lightweight **local RAG** (retrieval-augmented generation) store for the
Document agent.

Design goals
------------
* Zero heavy dependencies: pure-Python TF-IDF retrieval — no embeddings
  server, no vector DB.  Perfect for a sandbox whose purpose is to test
  *orchestration*, not retrieval quality.
* Transparent: every retrieval returns the exact chunks + scores so they
  can be persisted in the run trace for debugging.
* Persistent enough: documents are saved under ``data/documents/`` as
  ``.txt`` plus a ``manifest.json``, so the corpus survives restarts.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple

from ..schemas import new_id


_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Chunking parameters (characters).  Small chunks → sharper retrieval.
CHUNK_SIZE = 500
CHUNK_OVERLAP = 80


def _tokenize(text: str) -> List[str]:
    """Lowercase word tokens; good enough for lexical matching."""
    return _TOKEN_RE.findall(text.lower())


def _split_chunks(text: str) -> List[str]:
    """
    Split text into ~CHUNK_SIZE character chunks with overlap.

    Paragraph boundaries are respected when possible so retrieved chunks
    stay readable when pasted into the trace / UI.
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: List[str] = []
    current = ""

    for para in paragraphs:
        # Long paragraphs are hard-split.
        while len(para) > CHUNK_SIZE:
            piece, para = para[:CHUNK_SIZE], para[CHUNK_SIZE - CHUNK_OVERLAP:]
            if current:
                chunks.append(current)
                current = ""
            chunks.append(piece.strip())
        if not current:
            current = para
        elif len(current) + len(para) + 2 <= CHUNK_SIZE:
            current = f"{current}\n\n{para}"
        else:
            chunks.append(current)
            # Overlap: keep the tail of the previous chunk for continuity.
            tail = current[-CHUNK_OVERLAP:]
            current = f"{tail}\n\n{para}" if tail else para

    if current:
        chunks.append(current)
    return chunks or [text[:CHUNK_SIZE]]


@dataclass
class Chunk:
    """One retrievable passage."""

    chunk_id: str
    doc_id: str
    doc_name: str
    text: str
    index: int


@dataclass
class RetrievedChunk:
    """A chunk plus its retrieval score (for traces)."""

    chunk: Chunk
    score: float

    def to_dict(self) -> dict:
        return {
            "doc_id": self.chunk.doc_id,
            "doc_name": self.chunk.doc_name,
            "chunk_index": self.chunk.index,
            "score": round(self.score, 4),
            "text": self.chunk.text,
        }


@dataclass
class DocumentStore:
    """
    In-memory corpus with TF-IDF retrieval.

    ``document_dir`` persists uploaded text as ``<doc_id>.txt`` with a
    ``manifest.json`` describing every document (name, size, chunk count).
    """

    document_dir: Path
    _chunks: List[Chunk] = field(default_factory=list)
    _doc_names: Dict[str, str] = field(default_factory=dict)  # doc_id → name
    _doc_freq: Counter = field(default_factory=Counter)
    _doc_count: int = 0

    def __post_init__(self) -> None:
        self.document_dir.mkdir(parents=True, exist_ok=True)
        self._load_from_disk()

    # ── persistence ─────────────────────────────────────────────
    def _manifest_path(self) -> Path:
        return self.document_dir / "manifest.json"

    def _load_from_disk(self) -> None:
        manifest = self._manifest_path()
        if not manifest.exists():
            return
        try:
            entries = json.loads(manifest.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        for entry in entries:
            path = self.document_dir / entry["file"]
            if path.exists():
                self._index_document(
                    entry["doc_id"],
                    entry["name"],
                    path.read_text(encoding="utf-8"),
                    persist=False,
                )

    def _write_manifest(self) -> None:
        entries = []
        for doc_id, name in self._doc_names.items():
            entries.append({"doc_id": doc_id, "name": name, "file": f"{doc_id}.txt"})
        self._manifest_path().write_text(
            json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    # ── indexing ────────────────────────────────────────────────
    def _index_document(self, doc_id: str, name: str, text: str, *, persist: bool) -> Dict[str, object]:
        chunks = _split_chunks(text)
        self._doc_names[doc_id] = name
        self._doc_count += 1

        for idx, chunk_text in enumerate(chunks):
            self._chunks.append(
                Chunk(
                    chunk_id=f"{doc_id}#{idx}",
                    doc_id=doc_id,
                    doc_name=name,
                    text=chunk_text,
                    index=idx,
                )
            )
            # Document frequency for IDF.
            unique_tokens = set(_tokenize(chunk_text))
            for token in unique_tokens:
                self._doc_freq[token] += 1

        if persist:
            (self.document_dir / f"{doc_id}.txt").write_text(text, encoding="utf-8")
            self._write_manifest()

        return {"doc_id": doc_id, "name": name, "chunks": len(chunks)}

    def add_document(self, name: str, text: str) -> Dict[str, object]:
        """Index a new document; returns metadata including chunk count."""
        doc_id = new_id("doc")
        return self._index_document(doc_id, name, text, persist=True)

    # ── retrieval ───────────────────────────────────────────────
    def _tf_idf(self, tokens: List[str]) -> Dict[str, float]:
        counts = Counter(tokens)
        total = len(tokens) or 1
        vector: Dict[str, float] = {}
        for token, count in counts.items():
            idf = math.log((1 + self._doc_count) / (1 + self._doc_freq.get(token, 0))) + 1.0
            vector[token] = (count / total) * idf
        return vector

    @staticmethod
    def _cosine(a: Dict[str, float], b: Dict[str, float]) -> float:
        if not a or not b:
            return 0.0
        common = set(a) & set(b)
        dot = sum(a[t] * b[t] for t in common)
        norm_a = math.sqrt(sum(v * v for v in a.values()))
        norm_b = math.sqrt(sum(v * v for v in b.values()))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    def retrieve(self, query: str, top_k: int = 4) -> List[RetrievedChunk]:
        """Return the top-k chunks most similar to ``query`` (TF-IDF cosine)."""
        if not self._chunks:
            return []
        query_vector = self._tf_idf(_tokenize(query))
        scored: List[RetrievedChunk] = []
        for chunk in self._chunks:
            chunk_vector = self._tf_idf(_tokenize(chunk.text))
            score = self._cosine(query_vector, chunk_vector)
            if score > 0:
                scored.append(RetrievedChunk(chunk=chunk, score=score))
        scored.sort(key=lambda r: r.score, reverse=True)
        return scored[:top_k]

    # ── introspection ───────────────────────────────────────────
    def list_documents(self) -> List[Dict[str, object]]:
        return [
            {"doc_id": doc_id, "name": name}
            for doc_id, name in self._doc_names.items()
        ]

    @property
    def document_count(self) -> int:
        return len(self._doc_names)

    @property
    def chunk_count(self) -> int:
        return len(self._chunks)
