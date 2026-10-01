from __future__ import annotations

import logging

logger = logging.getLogger("financial_research_agent.rag")

RAG_CHUNK_THRESHOLD = 1500

CHUNK_SIZE = 600
CHUNK_OVERLAP = 80
TOP_K_CHUNKS = 6

_embeddings = None  # constructed once per process, not once per call


def _get_embeddings():

    global _embeddings
    if _embeddings is None:
        from langchain_huggingface import HuggingFaceEmbeddings

        _embeddings = HuggingFaceEmbeddings(model_name="sentence-transformers/all-MiniLM-L6-v2")
    return _embeddings


def retrieve_relevant_text(document_text: str, query: str, max_chars: int) -> str:

    if len(document_text) <= max(RAG_CHUNK_THRESHOLD, max_chars):
        return document_text[:max_chars]

    try:
        return _rag_select(document_text, query, max_chars)
    except Exception as exc:  # noqa: BLE001
        logger.warning("RAG chunk retrieval failed (%s); falling back to prefix truncation", exc)
        return document_text[:max_chars]


def _rag_select(document_text: str, query: str, max_chars: int) -> str:

    from langchain_community.vectorstores import FAISS
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        add_start_index=True,
    )
    docs = splitter.create_documents([document_text])
    if not docs:
        return document_text[:max_chars]

    store = FAISS.from_documents(docs, _get_embeddings())
    k = min(TOP_K_CHUNKS, len(docs))
    hits = store.similarity_search(query, k=k)  # best-match-first order

    selected_docs = []
    used = 0
    for doc in hits:
        text = doc.page_content
        if used + len(text) > max_chars:
            remaining = max_chars - used
            if remaining <= 0:
                break
            text = text[:remaining]
        selected_docs.append((doc.metadata.get("start_index", 0), text))
        used += len(text)
        if used >= max_chars:
            break

    selected_docs.sort(key=lambda item: item[0])
    pieces = [text for _start, text in selected_docs]
    selected = "\n[...]\n".join(pieces)
    logger.info(
        "RAG: %d chunks from %d-char document -> retrieved %d/%d chunks (%d chars) for query=%r",
        len(docs), len(document_text), len(pieces), k, len(selected), query[:60],
    )
    return selected
