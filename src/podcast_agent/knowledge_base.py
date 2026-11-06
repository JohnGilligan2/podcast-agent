"""A tiny local knowledge base with BM25 ranking.

This stands in for a search/retrieval API. It is intentionally dependency-free so the
ranking logic is easy to read; in production it would be replaced by a vector or hybrid
index (see docs/DEPLOYMENT.md) behind the same `search()` interface.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

TOKEN_RE = re.compile(r"[a-z0-9]+")
STOPWORDS = set(
    "a an and are as at be been but by can do does for from has have how in into is it its "
    "of on or that the their them they this to was were what when which who will with would "
    "you your our we i".split()
)


def tokenize(text: str) -> list[str]:
    return [t for t in TOKEN_RE.findall(text.lower()) if t not in STOPWORDS]


@dataclass
class KBDocument:
    id: str
    title: str
    source: str
    url: str | None
    as_of: str | None
    tags: list[str]
    content: str
    tier: str = "reference"   # reference (curated KB) | pinned (client-supplied fact)

    def searchable_text(self) -> str:
        # Title and tags are repeated so they weigh more than body text.
        return " ".join([self.title, self.title, " ".join(self.tags), " ".join(self.tags), self.content])


@dataclass
class SearchHit:
    doc: KBDocument
    score: float

    def as_dict(self) -> dict:
        return {
            "source_id": self.doc.id,
            "title": self.doc.title,
            "source": self.doc.source,
            "url": self.doc.url,
            "as_of": self.doc.as_of,
            "tier": self.doc.tier,
            "score": round(self.score, 2),
            "content": self.doc.content,
        }


class KnowledgeBase:
    def __init__(self, documents: list[KBDocument], k1: float = 1.5, b: float = 0.75):
        self.documents = documents
        self.by_id = {d.id: d for d in documents}
        self.k1, self.b = k1, b
        self._tf = [Counter(tokenize(d.searchable_text())) for d in documents]
        self._len = [sum(tf.values()) for tf in self._tf]
        self._avg_len = (sum(self._len) / len(self._len)) if self._len else 0.0
        df: Counter[str] = Counter()
        for tf in self._tf:
            df.update(tf.keys())
        n = len(documents)
        self._idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

    @classmethod
    def load(cls, path: str | Path) -> "KnowledgeBase":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        docs = [KBDocument(**{k: d[k] for k in KBDocument.__dataclass_fields__ if k in d})
                for d in data["documents"]]
        return cls(docs)

    def with_documents(self, extra: list[KBDocument]) -> "KnowledgeBase":
        """A new index that also covers `extra` (e.g. a client's pinned facts)."""
        return KnowledgeBase(self.documents + list(extra), self.k1, self.b) if extra else self

    def search(self, query: str, top_k: int = 4, min_score: float = 1.0) -> list[SearchHit]:
        q = tokenize(query)
        if not q:
            return []
        hits = []
        for doc, tf, length in zip(self.documents, self._tf, self._len):
            score = 0.0
            for term in q:
                f = tf.get(term, 0)
                if not f:
                    continue
                denom = f + self.k1 * (1 - self.b + self.b * length / self._avg_len)
                score += self._idf[term] * f * (self.k1 + 1) / denom
            if score >= min_score:
                hits.append(SearchHit(doc, score))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:top_k]
