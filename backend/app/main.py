import asyncio
import json
import os
import time
import shutil
import tempfile
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
from vector_store import (
    delete_by_filename,
    ensure_collection,
    hybrid_search,
    list_documents,
    store_chunks,
)

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
SUPPORTED_EXTENSIONS = {".txt", ".md", ".pdf", ".docx", ".xlsx", ".csv"}

# Relevance gate. All three compare against COSINE similarity, the number shown in Sources.
DENSE_THRESHOLD = 0.45   # this semantically close is always relevant
DENSE_FLOOR = 0.30       # a keyword match only counts if it is at least this close semantically
KEYWORD_RANK_LIMIT = 5   # ...and ranks in the top N of the keyword results
TOP_K = 5

def is_relevant(c: dict) -> bool:
    dense = c["dense"] or 0.0
    if dense >= DENSE_THRESHOLD:
        return True
    return (
        c["keyword_rank"] is not None
        and c["keyword_rank"] <= KEYWORD_RANK_LIMIT
        and dense >= DENSE_FLOOR
    )


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

    if not model:
        raise HTTPException(status_code=400, detail="No model selected")
    if not messages:
        raise HTTPException(status_code=400, detail="No messages provided")
    if not isinstance(document_filter, list):
        document_filter = None

    last_user_message = messages[-1]["content"]
    search_query = last_user_message
    relevant_chunks: list[dict] = []

    # An empty filter list means "no documents selected": skip retrieval entirely
    if use_knowledge_base and document_filter != []:
        if len(messages) > 1:
            search_query = await rewrite_followup(model, messages[:-1], last_user_message)

        # embed_text and search are blocking calls: run them in worker threads
        # so they don't freeze the event loop (and every other request with it)
        question_vector = await asyncio.to_thread(embed_text, search_query)
        candidates = await asyncio.to_thread(
            hybrid_search, question_vector, search_query, document_filter
        )
        relevant_chunks = [c for c in candidates if is_relevant(c)][:TOP_K]

    augmented_messages = messages[:-1] + [
        {"role": "user", "content": build_rag_prompt(last_user_message, relevant_chunks, prompt_instruction)}
    ]

    meta = {
               "sources": [
            {
                "filename": c["filename"],
                "score": round(c["dense"] or 0.0, 4),
                "keyword_rank": c["keyword_rank"],
                "text": c["text"],
            }
            for c in relevant_chunks
        ],
        # only sent when the rewrite actually changed the question
        "search_query": search_query if search_query != last_user_message else None,
    }

    async def event_stream():
        # First line: metadata (sources + what we searched for). Everything after it is answer text.
        yield f"{'__META__'}{json.dumps(meta, ensure_ascii=False)}\n"

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

        vectors = started = time.perf_counter()
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