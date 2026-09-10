from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from typing import Any

from .database import Database

TOKEN_PATTERN = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)


def tokenize(text: str) -> list[str]:
    words: list[str] = []
    for token in TOKEN_PATTERN.findall(text.lower()):
        if re.fullmatch(r"[\u4e00-\u9fff]+", token):
            words.extend(token[index : index + 2] for index in range(max(1, len(token) - 1)))
        else:
            words.append(token)
    return words


def embedding(text: str, dimensions: int = 256) -> list[float]:
    vector = [0.0] * dimensions
    for token, count in Counter(tokenize(text)).items():
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        vector[index] += float(count) * (1 if digest[4] % 2 else -1)
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


class HybridRetriever:
    """Offline hybrid retriever: token overlap plus deterministic vector similarity."""

    def __init__(self, database: Database):
        self.database = database

    def search(self, query: str, kind: str | None = None, limit: int = 4) -> list[dict[str, Any]]:
        sql = "SELECT id,kind,title,tags,content FROM documents"
        params: tuple[Any, ...] = ()
        if kind:
            sql += " WHERE kind=?"
            params = (kind,)
        documents = self.database.query(sql, params)
        query_tokens = set(tokenize(query))
        query_vector = embedding(query)
        ranked: list[dict[str, Any]] = []
        for document in documents:
            text = f"{document['title']} {document['tags']} {document['content']}"
            tokens = set(tokenize(text))
            lexical = len(query_tokens & tokens) / max(1, len(query_tokens))
            dense = sum(a * b for a, b in zip(query_vector, embedding(text), strict=True))
            score = round(0.65 * lexical + 0.35 * max(0.0, dense), 4)
            ranked.append({**document, "score": score})
        ranked.sort(key=lambda item: item["score"], reverse=True)
        return ranked[: max(1, min(limit, 10))]
