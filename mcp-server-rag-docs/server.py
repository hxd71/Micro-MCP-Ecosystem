from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import faiss
import numpy as np
from langchain_text_splitters import RecursiveCharacterTextSplitter
from mcp.server.fastmcp import FastMCP
from sklearn.feature_extraction.text import TfidfVectorizer

mcp = FastMCP("mcp-server-rag-docs")

BASE_DIR = Path(__file__).resolve().parent
DOCS_DIR = BASE_DIR / "docs"


@dataclass
class ChunkRecord:
    source: str
    text: str


KB_CHUNKS: list[ChunkRecord] = []
KB_INDEX: faiss.Index | None = None
KB_VECTORIZER: TfidfVectorizer | None = None


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


def _build_knowledge_base() -> tuple[int, int]:
    global KB_CHUNKS, KB_INDEX, KB_VECTORIZER

    docs = _read_docs(DOCS_DIR)
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=700,
        chunk_overlap=120,
        separators=["\n\n", "\n", "。", ". ", " ", ""],
    )

    chunk_records: list[ChunkRecord] = []
    for source, content in docs:
        for chunk in splitter.split_text(content):
            normalized = chunk.strip()
            if not normalized:
                continue
            chunk_records.append(ChunkRecord(source=source, text=normalized))

    KB_CHUNKS = chunk_records
    if not KB_CHUNKS:
        KB_VECTORIZER = None
        KB_INDEX = None
        return len(docs), 0

    vectorizer = TfidfVectorizer(
        max_features=4096,
        ngram_range=(1, 2),
        lowercase=True,
    )
    matrix = vectorizer.fit_transform([record.text for record in KB_CHUNKS]).astype(np.float32)
    dense = matrix.toarray()

    norms = np.linalg.norm(dense, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    dense = dense / norms

    index = faiss.IndexFlatIP(dense.shape[1])
    index.add(dense)

    KB_VECTORIZER = vectorizer
    KB_INDEX = index
    return len(docs), len(KB_CHUNKS)


def _ensure_kb() -> None:
    if KB_INDEX is None or KB_VECTORIZER is None or not KB_CHUNKS:
        _build_knowledge_base()


@mcp.tool()
async def search_knowledge_base(query: str) -> str:
    """Search local docs knowledge base and return top 3 relevant chunks.

    Args:
        query: User query text.

    Returns:
        The 3 most relevant text chunks with source paths.
    """
    _ensure_kb()

    if KB_INDEX is None or KB_VECTORIZER is None or not KB_CHUNKS:
        return "Knowledge base is empty. Please put .md/.txt/.log files into mcp-server-rag-docs/docs."

    query_text = query.strip()
    if not query_text:
        return "Query is empty."

    vector = KB_VECTORIZER.transform([query_text]).astype(np.float32).toarray()
    norm = np.linalg.norm(vector, axis=1, keepdims=True)
    norm[norm == 0] = 1.0
    vector = vector / norm

    top_k = min(3, len(KB_CHUNKS))
    scores, indices = KB_INDEX.search(vector, top_k)

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
        f"[mcp-server-rag-docs] loaded files={file_count}, chunks={chunk_count}",
        file=sys.stderr,
    )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
