"""Retrieval-Augmented Generation for claim extraction (LangChain-backed).

Problem this replaces: the Claim Extractor used to feed the LLM the first
MAX_SOURCE_CHARS_FOR_LLM characters of a source document and discard
whatever came after - if a filing discusses revenue on page 1 and risk
factors further down, the risk factors were silently dropped before the
model ever saw them.

This module chunks a source's text (LangChain's
RecursiveCharacterTextSplitter), embeds each chunk with a small local
sentence-transformer model (no API key, no network call, no shared rate
limit with the LLM calls), indexes them in an ephemeral per-source FAISS
vector store, and retrieves the chunks most semantically relevant to what
the research plan actually asked about - relevance-ranked selection
instead of a fixed-prefix cut.

Scope, deliberately narrow: the index is built fresh per source, per
extraction call, and discarded immediately after. It is not a persistent
corpus - each research run concerns a different company and period, so
there is no shared index to reuse across runs. The job here is only:
pick the best chunks OF THIS ONE DOCUMENT for THIS ONE extraction call.

Evidence integrity is completely unaffected by any of this: retrieved
chunks are exact, contiguous substrings of source.document_text
(add_start_index=True is what lets us trust that), so the Claim
Extractor's existing verbatim-quote validation (make_evidence) against
the full original text is unchanged. RAG changes what the model is shown;
it never changes how a claim's evidence is checked afterwards.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("financial_research_agent.rag")

# Below this length a source's whole text already fits comfortably in an
# extraction call - chunking and embedding it would spend real CPU time
# (model load, vector computation) for zero benefit, since retrieval would
# just hand back everything anyway.
RAG_CHUNK_THRESHOLD = 1500

CHUNK_SIZE = 600
CHUNK_OVERLAP = 80
TOP_K_CHUNKS = 6

_embeddings = None  # constructed once per process, not once per call


def _get_embeddings():
    """Loads the local embedding model once and reuses it thereafter.
    Deferred import + lazy init so the (fairly heavy) sentence-transformers
    / torch stack is only ever loaded by a process that actually exercises
    this path - the deterministic mock extractor, and every test that
    doesn't touch RAG, never pay that startup cost."""
    global _embeddings
    if _embeddings is None:
        from langchain_huggingface import HuggingFaceEmbeddings

        _embeddings = HuggingFaceEmbeddings(model_name="sentence-transformers/all-MiniLM-L6-v2")
    return _embeddings


def retrieve_relevant_text(document_text: str, query: str, max_chars: int) -> str:
    """Returns the portion of document_text most relevant to `query`,
    within a max_chars budget. Falls back to a plain prefix (the previous
    behaviour) for short documents, or if the RAG stack fails to load for
    any reason - a missing/broken dependency degrades extraction quality
    slightly rather than breaking the pipeline."""
    if len(document_text) <= max(RAG_CHUNK_THRESHOLD, max_chars):
        return document_text[:max_chars]

    try:
        return _rag_select(document_text, query, max_chars)
    except Exception as exc:  # noqa: BLE001
        logger.warning("RAG chunk retrieval failed (%s); falling back to prefix truncation", exc)
        return document_text[:max_chars]


def _rag_select(document_text: str, query: str, max_chars: int) -> str:
    # langchain-community emits a maintenance-mode deprecation warning as of
    # this writing; its FAISS integration is still the correct, stable API
    # for this use case (there is no drop-in standalone replacement - the
    # early-stage `langchain-faiss` package on PyPI has a different,
    # unstable interface). Not a functional concern - noted here so it
    # reads as a deliberate call, not an oversight.
    from langchain_community.vectorstores import FAISS
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        add_start_index=True,  # records each chunk's offset in the original
        # text, which is what lets us reassemble retrieved chunks back into
        # document order below and trust they're verbatim substrings.
    )
    docs = splitter.create_documents([document_text])
    if not docs:
        return document_text[:max_chars]

    store = FAISS.from_documents(docs, _get_embeddings())
    k = min(TOP_K_CHUNKS, len(docs))
    hits = store.similarity_search(query, k=k)  # best-match-first order

    # Two separate decisions here, deliberately kept in this order:
    #   1. WHICH chunks make the cut - decided by similarity rank, so the
    #      most relevant content is never crowded out of the budget by a
    #      less-relevant chunk that merely sits earlier in the document.
    #   2. WHAT ORDER to present the chosen chunks in - re-sorted into
    #      original document order only after selection, so an LLM reads
    #      one coherent excerpt rather than a similarity-ranked jumble.
    # (An earlier version selected AND ordered by document position in one
    # sort, which let long irrelevant sections ahead of a highly relevant
    # one silently exhaust the character budget first - caught by
    # tests/test_rag_indexer.py::test_risk_query_retrieves_the_risk_factors_section.)
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
