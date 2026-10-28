import asyncio
import json
import os
import shutil
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from chunking import chunk_text
from document_processing import extract_text
from embeddings import embed_text, embed_texts
from rerank import rerank, warmup as warmup_reranker
from vector_store import (
    delete_by_filename,
    ensure_collection,
    hybrid_search,
    list_documents,
    store_chunks,
)

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
SUPPORTED_EXTENSIONS = {".txt", ".md", ".pdf", ".docx", ".xlsx", ".csv"}
RERANK_POOL = 20  # at most this many passing passages go through the reranker

# Defaults for every tunable knob. The frontend sends its own values with each chat
# request; anything missing or invalid falls back to these.
DEFAULT_SETTINGS = {
    "dense_threshold": 0.45,   # cosine similarity at or above this is always relevant
    "dense_floor": 0.30,       # a keyword match also needs at least this cosine similarity...
    "keyword_rank_limit": 5,   # ...and must rank in the top N of the keyword results
    "top_k": 3,                # passages sent to the model
    "use_keyword_search": True,
    "use_reranker": False,
    "rerank_min_score": None,  # optional cutoff in the reranker's own units (None = off)
    "history_limit": 10,       # chat messages sent to the model (0 = all of them)
}


def read_settings(raw) -> dict:
    """Merge client-sent settings over the defaults, clamping everything to sane ranges."""
    s = dict(DEFAULT_SETTINGS)
    if not isinstance(raw, dict):
        return s

    def num(key, lo, hi, cast):
        try:
            s[key] = min(max(cast(raw[key]), lo), hi)
        except (KeyError, TypeError, ValueError):
            pass

    num("dense_threshold", 0.0, 1.0, float)
    num("dense_floor", 0.0, 1.0, float)
    num("keyword_rank_limit", 1, 20, int)
    num("top_k", 1, 10, int)
    num("history_limit", 0, 100, int)
    s["use_keyword_search"] = bool(raw.get("use_keyword_search", s["use_keyword_search"]))
    s["use_reranker"] = bool(raw.get("use_reranker", s["use_reranker"]))
    try:
        v = raw.get("rerank_min_score")
        s["rerank_min_score"] = None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        s["rerank_min_score"] = None
    return s


def is_relevant(c: dict, s: dict) -> bool:
    """The relevance gate. Compares COSINE similarity, the number shown in Sources."""
    dense = c["dense"] or 0.0
    if dense >= s["dense_threshold"]:
        return True
    return (
        c["keyword_rank"] is not None
        and c["keyword_rank"] <= s["keyword_rank_limit"]
        and dense >= s["dense_floor"]
    )


def trim_history(messages: list[dict], limit: int) -> list[dict]:
    """Keep only the last `limit` messages (0 = keep everything). The conversation
    should start with a user message, so drop a leading assistant message if the
    cut landed on one."""
    if limit <= 0 or len(messages) <= limit:
        return messages
    trimmed = messages[-limit:]
    while len(trimmed) > 1 and trimmed[0].get("role") != "user":
        trimmed = trimmed[1:]
    return trimmed


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_collection()
    yield


app = FastAPI(title="Ollama RAG Chat Backend", lifespan=lifespan)

# Allows the Vue frontend (different port) to call this API from the browser
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health():
    return {"status": "Ok-Backend is running"}


@app.get("/api/models")
async def list_models():
    """Ask Ollama which models are installed, return just their names."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
        resp.raise_for_status()
        data = resp.json()
    return [m["name"] for m in data.get("models", [])]


@app.post("/api/rerank/warmup")
def rerank_warmup():
    """Load (and on first use, download) the reranker. Called when the toggle is switched on."""
    return warmup_reranker()


# ───────────────────────── RAG helpers ─────────────────────────

DEFAULT_INSTRUCTION = (
    "Answer the question using ONLY the context below. "
    "If the context doesn't contain the answer, say you don't know — "
    "do not make up information."
)


def build_rag_prompt(question: str, retrieved_chunks: list[dict], instruction: str | None = None) -> str:
    if not retrieved_chunks:
        return question

    context = "\n\n".join(
        f"[Source {i + 1}: {c['filename']}]\n{c['text']}" for i, c in enumerate(retrieved_chunks)
    )
    instruction = (instruction or DEFAULT_INSTRUCTION).strip() or DEFAULT_INSTRUCTION
    return f"{instruction}\n\nContext:\n{context}\n\nQuestion: {question}"


async def rewrite_followup(model: str, history: list[dict], question: str) -> str:
    """Turn a context-dependent follow-up ("and the second one?") into a standalone
    search query. On any failure we fall back to the original question, so this
    can never break chat."""
    transcript = "\n".join(
        f"{'User' if m.get('role') == 'user' else 'Assistant'}: {str(m.get('content', ''))[:400]}"
        for m in history[-6:]
    )
    prompt = (
        "Rewrite the user's latest message as ONE standalone search query that makes sense "
        "without the conversation. Replace pronouns and references (\"it\", \"the second one\", "
        "\"that\") with the specific things they refer to. If the message is already "
        "self-contained, repeat it unchanged. Output only the query: no quotes, no explanation.\n\n"
        f"Conversation:\n{transcript}\n\n"
        f"Latest message: {question}\n\n"
        "Standalone query:"
    )
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{OLLAMA_BASE_URL}/api/generate",
                json={
                    "model": model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": 0, "num_predict": 100},
                },
            )
            resp.raise_for_status()
            text = resp.json().get("response", "").strip()
    except Exception:
        return question

    if not text:  # e.g. a reasoning model that spent its token budget "thinking"
        return question
    query = text.splitlines()[0].strip().strip("\"'")
    return query if 0 < len(query) <= 300 else question


# ───────────────────────── Chat ─────────────────────────

@app.post("/api/chat")
async def chat(request: Request):
    body = await request.json()
    model = body.get("model")
    messages = body.get("messages") or []
    prompt_instruction = body.get("prompt_instruction")
    use_knowledge_base = body.get("use_knowledge_base", True)
    document_filter = body.get("document_filter")  # None = all documents, list = only these
    settings = read_settings(body.get("retrieval_settings"))

    if not model:
        raise HTTPException(status_code=400, detail="No model selected")
    if not messages:
        raise HTTPException(status_code=400, detail="No messages provided")
    if not isinstance(document_filter, list):
        document_filter = None

    messages = trim_history(messages, settings["history_limit"])
    last_user_message = messages[-1]["content"]
    search_query = last_user_message
    relevant_chunks: list[dict] = []
    rerank_status = None  # None = not requested, "applied", or "unavailable"

    # An empty filter list means "no documents selected": skip retrieval entirely
    if use_knowledge_base and document_filter != []:
        if len(messages) > 1:
            search_query = await rewrite_followup(model, messages[:-1], last_user_message)

        # These are blocking calls: run them in worker threads so they don't
        # freeze the event loop (and every other request with it)
        question_vector = await asyncio.to_thread(embed_text, search_query)
        candidates = await asyncio.to_thread(
            hybrid_search,
            question_vector,
            search_query,
            document_filter,
            use_keyword=settings["use_keyword_search"],
        )
        passing = [c for c in candidates if is_relevant(c, settings)]

        if settings["use_reranker"] and passing:
            pool = passing[:RERANK_POOL]
            scores = await asyncio.to_thread(rerank, search_query, [c["text"] for c in pool])
            if scores is None:
                rerank_status = "unavailable"
                relevant_chunks = passing[: settings["top_k"]]
            else:
                for c, sc in zip(pool, scores):
                    c["rerank"] = sc
                pool.sort(key=lambda c: c["rerank"], reverse=True)
                if settings["rerank_min_score"] is not None:
                    pool = [c for c in pool if c["rerank"] >= settings["rerank_min_score"]]
                relevant_chunks = pool[: settings["top_k"]]
                rerank_status = "applied"
        else:
            relevant_chunks = passing[: settings["top_k"]]

    augmented_messages = messages[:-1] + [
        {"role": "user", "content": build_rag_prompt(last_user_message, relevant_chunks, prompt_instruction)}
    ]

    meta = {
        "sources": [
            {
                "filename": c["filename"],
                "score": round(c["dense"] or 0.0, 4),
                "keyword_rank": c["keyword_rank"],
                "rerank": round(c["rerank"], 3) if c.get("rerank") is not None else None,
                "text": c["text"],
            }
            for c in relevant_chunks
        ],
        # only sent when the rewrite actually changed the question
        "search_query": search_query if search_query != last_user_message else None,
        "rerank_status": rerank_status,
    }

    async def event_stream():
        # First line: metadata. Everything after it is answer text.
        yield f"__META__{json.dumps(meta, ensure_ascii=False)}\n"

        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "POST",
                f"{OLLAMA_BASE_URL}/api/chat",
                json={"model": model, "messages": augmented_messages, "stream": True},
            ) as response:
                if response.status_code != 200:
                    detail = (await response.aread()).decode(errors="replace")[:200]
                    yield f"[Ollama error {response.status_code}: {detail}]"
                    return
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    chunk = json.loads(line)
                    if "error" in chunk:
                        yield f"[Ollama error: {chunk['error']}]"
                        return
                    content = chunk.get("message", {}).get("content", "")
                    if content:
                        yield content

    return StreamingResponse(event_stream(), media_type="text/plain")


# ───────────────────────── Documents ─────────────────────────

# Plain `def`, not `async def`: it does blocking work (file parsing, HTTP calls to
# Ollama), so FastAPI runs it in a thread pool instead of freezing the server.
@app.post("/api/documents/upload")
def upload_document(file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")
    suffix = Path(file.filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {suffix}")

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = tmp.name

    try:
        file_size = os.path.getsize(tmp_path)
        try:
            text = extract_text(tmp_path)
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"Could not read file: {e}")

        chunks = chunk_text(text)
        if not chunks:
            raise HTTPException(status_code=400, detail="No extractable text found in file")

        started = time.perf_counter()
        vectors = embed_texts(chunks)
        print(f"[upload] {file.filename}: embedded {len(chunks)} chunks in {time.perf_counter() - started:.1f}s")

        # Replace any previous copy only AFTER embedding succeeded, so a failed
        # re-upload can't wipe the version you already had.
        delete_by_filename(file.filename)
        store_chunks(filename=file.filename, chunks=chunks, vectors=vectors, file_size=file_size)
    finally:
        os.unlink(tmp_path)

    return {"filename": file.filename, "chunks_stored": len(chunks)}


@app.get("/api/documents")
def get_documents():
    return {"documents": list_documents()}


@app.delete("/api/documents/{filename}")
def delete_document(filename: str):
    delete_by_filename(filename)
    return {"deleted": filename}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)