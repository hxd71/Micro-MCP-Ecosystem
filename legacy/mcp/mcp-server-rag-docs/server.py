from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from mcp.server.fastmcp import FastMCP

try:
    from sklearn.feature_extraction.text import TfidfVectorizer
except ImportError:
    TfidfVectorizer = None  # type: ignore[assignment]

try:
    from langchain_text_splitters import RecursiveCharacterTextSplitter
except ImportError:
    RecursiveCharacterTextSplitter = None  # type: ignore[assignment]

try:
    import faiss
except ImportError:
    faiss = None  # type: ignore[assignment]

mcp = FastMCP("mcp-server-rag-docs")

BASE_DIR = Path(__file__).resolve().parent
DOCS_DIR = BASE_DIR / "docs"


@dataclass
class ChunkRecord:
    source: str
    text: str


KB_CHUNKS: list[ChunkRecord] = []
KB_INDEX: Any | None = None
KB_MATRIX: np.ndarray | None = None
KB_VECTORIZER: Any | None = None
KB_VOCAB: dict[str, int] = {}


def _read_docs(root: Path) -> list[tuple[str, str]]:
    supported_ext = {".md", ".txt", ".log"}
    docs: list[tuple[str, str]] = []

    if not root.exists():
        return docs

    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in supported_ext:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = path.read_text(encoding="gbk", errors="replace")
        docs.append((str(path.relative_to(BASE_DIR)).replace("\\", "/"), content))

    return docs


def _split_text(text: str) -> list[str]:
    if RecursiveCharacterTextSplitter is not None:
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=700,
            chunk_overlap=120,
            separators=["\n\n", "\n", "。", ". ", " ", ""],
        )
        return splitter.split_text(text)

    chunk_size = 700
    chunk_overlap = 120
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunks.append(text[start:end])
        if end == len(text):
            break
        start = max(0, end - chunk_overlap)
    return chunks


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]", text.lower())


def _build_simple_matrix(texts: list[str]) -> np.ndarray:
    global KB_VOCAB

    doc_tokens = [_tokenize(text) for text in texts]
    term_counts = Counter(token for tokens in doc_tokens for token in tokens)
    most_common = term_counts.most_common(4096)
    KB_VOCAB = {token: index for index, (token, _) in enumerate(most_common)}

    dense = np.zeros((len(texts), max(len(KB_VOCAB), 1)), dtype=np.float32)
    for row, tokens in enumerate(doc_tokens):
        counts = Counter(tokens)
        for token, count in counts.items():
            col = KB_VOCAB.get(token)
            if col is not None:
                dense[row, col] = float(count)
    return dense


def _vectorize_query(query: str) -> np.ndarray:
    if KB_VECTORIZER is not None:
        return KB_VECTORIZER.transform([query]).astype(np.float32).toarray()

    vector = np.zeros((1, max(len(KB_VOCAB), 1)), dtype=np.float32)
    counts = Counter(_tokenize(query))
    for token, count in counts.items():
        col = KB_VOCAB.get(token)
        if col is not None:
            vector[0, col] = float(count)
    return vector


def _build_knowledge_base() -> tuple[int, int]:
    global KB_CHUNKS, KB_INDEX, KB_MATRIX, KB_VECTORIZER

    docs = _read_docs(DOCS_DIR)
    chunk_records: list[ChunkRecord] = []
    for source, content in docs:
        for chunk in _split_text(content):
            normalized = chunk.strip()
            if not normalized:
                continue
            chunk_records.append(ChunkRecord(source=source, text=normalized))

    KB_CHUNKS = chunk_records
    if not KB_CHUNKS:
        KB_VECTORIZER = None
        KB_INDEX = None
        KB_MATRIX = None
        return len(docs), 0

    texts = [record.text for record in KB_CHUNKS]
    vectorizer = None
    if TfidfVectorizer is not None:
        vectorizer = TfidfVectorizer(
            max_features=4096,
            ngram_range=(1, 2),
            lowercase=True,
        )
        matrix = vectorizer.fit_transform(texts).astype(np.float32)
        dense = matrix.toarray()
    else:
        dense = _build_simple_matrix(texts)

    norms = np.linalg.norm(dense, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    dense = dense / norms

    index = None
    if faiss is not None:
        index = faiss.IndexFlatIP(dense.shape[1])
        index.add(dense)

    KB_VECTORIZER = vectorizer
    KB_INDEX = index
    KB_MATRIX = dense
    return len(docs), len(KB_CHUNKS)


def _ensure_kb() -> None:
    if KB_MATRIX is None or not KB_CHUNKS:
        _build_knowledge_base()


def _search_dense(vector: np.ndarray, top_k: int) -> tuple[np.ndarray, np.ndarray]:
    if KB_INDEX is not None:
        return KB_INDEX.search(vector, top_k)

    if KB_MATRIX is None:
        return np.array([[]], dtype=np.float32), np.array([[]], dtype=np.int64)

    scores = (KB_MATRIX @ vector[0]).astype(np.float32)
    order = np.argsort(scores)[::-1][:top_k]
    return scores[order][None, :], order.astype(np.int64)[None, :]


@mcp.tool()
async def search_knowledge_base(query: str) -> str:
    """Search local docs knowledge base and return top 3 relevant chunks.

    Args:
        query: User query text.

    Returns:
        The 3 most relevant text chunks with source paths.
    """
    _ensure_kb()

    if KB_MATRIX is None or not KB_CHUNKS:
        return "Knowledge base is empty. Please put .md/.txt/.log files into mcp-server-rag-docs/docs."

    query_text = query.strip()
    if not query_text:
        return "Query is empty."

    vector = _vectorize_query(query_text)
    norm = np.linalg.norm(vector, axis=1, keepdims=True)
    norm[norm == 0] = 1.0
    vector = vector / norm

    top_k = min(3, len(KB_CHUNKS))
    scores, indices = _search_dense(vector, top_k)

    lines: list[str] = []
    for rank, (score, idx) in enumerate(zip(scores[0], indices[0]), start=1):
        if idx < 0:
            continue
        record = KB_CHUNKS[int(idx)]
        lines.append(
            f"[{rank}] source={record.source} score={score:.4f}\n{record.text}"
        )

    if not lines:
        return "No relevant content found."

    return "\n\n---\n\n".join(lines)


@mcp.tool()
async def refresh_knowledge_base() -> str:
    """Rebuild in-memory index from docs folder."""
    file_count, chunk_count = _build_knowledge_base()
    return f"Knowledge base refreshed. files={file_count}, chunks={chunk_count}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run mcp-server-rag-docs")
    parser.add_argument(
        "--transport",
        choices=["stdio"],
        default="stdio",
        help="Transport mode. This server is intended for stdio in mcp-core-hub.",
    )
    _ = parser.parse_args()

    file_count, chunk_count = _build_knowledge_base()
    print(
        f"[mcp-server-rag-docs] loaded files={file_count}, chunks={chunk_count}, backend={'faiss' if faiss is not None else 'numpy'}, vectorizer={'sklearn' if TfidfVectorizer is not None else 'simple'}",
        file=sys.stderr,
    )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
