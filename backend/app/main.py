# We need to import the required libraries first

import os
import shutil
import tempfile
from pathlib import Path
from fastapi import UploadFile, File, HTTPException

from document_processing import extract_text
from chunking import chunk_text
from embeddings import embed_text
from vector_store import ensure_collection, store_chunks, delete_by_filename, list_filenames

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import httpx
import uvicorn

# additional libraries for the chat streaming endpoint

from fastapi import Request
from fastapi.responses import StreamingResponse
import json
import os

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")

# OLLAMA_BASE_URL = "http://localhost:11434"  # we'll make this configurable later for Docker

app = FastAPI(title="Ollama Chat Backend")

SUPPORTED_EXTENSIONS = {".txt", ".md", ".pdf", ".docx", ".xlsx", ".csv"}

@app.lifespan("startup")
async def startup():
    ensure_collection()

# Allows our Vue frontend (different port) to call this API from the browser
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/api/health")
async def health():
    return {"status": "Ok-Backend is running"}

# @app.get("/api/luitzdaryl")
# async def luitzdaryl():
#     return{"Luitz Daryl": "QUE MIRAS BOBO???"}

@app.get("/api/models")
async def list_models():
    """Ask Ollama which models are installed, return just their names."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
        resp.raise_for_status()
        data = resp.json()
    return [m["name"] for m in data.get("models", [])]

# This is the main chat endpoint that streams responses from Ollama to the frontend

@app.post("/api/chat")
async def chat(request: Request):
    body = await request.json()
    model = body.get("model")
    messages = body.get("messages", [])

    async def event_stream():
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "POST",
                f"{OLLAMA_BASE_URL}/api/chat",
                json={"model": model, "messages": messages, "stream": True},
            ) as response:
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    chunk = json.loads(line)
                    content = chunk.get("message", {}).get("content", "")
                    if content:
                        yield content

    return StreamingResponse(event_stream(), media_type="text/plain")


@app.post("/api/documents/upload")
async def upload_document(file: UploadFile = File(...)):
    suffix = Path(file.filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {suffix}")

    # extract_text expects a real file path, not raw bytes — write to a
    # temp file first, always clean it up afterward regardless of outcome
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = tmp.name

    try:
        text = extract_text(tmp_path)
        chunks = chunk_text(text)
        if not chunks:
            raise HTTPException(status_code=400, detail="No extractable text found in file")

        vectors = [embed_text(c) for c in chunks]
        store_chunks(filename=file.filename, chunks=chunks, vectors=vectors)
    finally:
        os.unlink(tmp_path)

    return {"filename": file.filename, "chunks_stored": len(chunks)}


@app.get("/api/documents")
async def list_documents():
    return {"documents": list_filenames()}


@app.delete("/api/documents/{filename}")
async def delete_document(filename: str):
    delete_by_filename(filename)
    return {"deleted": filename}

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)