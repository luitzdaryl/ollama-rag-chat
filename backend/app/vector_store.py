import os
import uuid
from datetime import datetime, timezone

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchAny,
    MatchValue,
    PointStruct,
    VectorParams,
)

COLLECTION_NAME = "documents"
VECTOR_SIZE = 1024  # must match bge-m3's output size

# Env vars with local defaults (same pattern as OLLAMA_BASE_URL),
# so this keeps working unchanged once we containerize the backend.
client = QdrantClient(
    host=os.environ.get("QDRANT_HOST", "localhost"),
    port=int(os.environ.get("QDRANT_PORT", "6333")),
)

UPSERT_BATCH = 64  # one giant request for a big document can exceed Qdrant's payload limit


def ensure_collection():
    """Create the collection once if missing. Never recreates it, since that would wipe every document."""
    if not client.collection_exists(COLLECTION_NAME):
        client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
        )


def store_chunks(filename: str, chunks: list[str], vectors: list[list[float]], file_size: int = 0):
    """One point per chunk: vector + the chunk text + file metadata (for the Documents tab)."""
    uploaded_at = datetime.now(timezone.utc).isoformat()
    points = [
        PointStruct(
            id=str(uuid.uuid4()),
            vector=vector,
            payload={
                "filename": filename,
                "text": chunk,
                "uploaded_at": uploaded_at,
                "file_size": file_size,
            },
        )
        for chunk, vector in zip(chunks, vectors)
    ]
    for i in range(0, len(points), UPSERT_BATCH):
        client.upsert(collection_name=COLLECTION_NAME, points=points[i : i + UPSERT_BATCH])


def delete_by_filename(filename: str):
    client.delete(
        collection_name=COLLECTION_NAME,
        points_selector=Filter(
            must=[FieldCondition(key="filename", match=MatchValue(value=filename))]
        ),
    )


def list_documents() -> list[dict]:
    """Aggregate per-file details by scrolling payloads only (no vectors, no chunk text)."""
    docs: dict[str, dict] = {}
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=COLLECTION_NAME,
            with_payload=["filename", "uploaded_at", "file_size"],
            with_vectors=False,
            limit=256,
            offset=offset,
        )
        for point in points:
            payload = point.payload or {}
            name = payload.get("filename")
            if not name:
                continue
            doc = docs.setdefault(
                name,
                {
                    "filename": name,
                    "chunks": 0,
                    "file_size": payload.get("file_size", 0),
                    "uploaded_at": payload.get("uploaded_at"),  # None for files uploaded before this change
                },
            )
            doc["chunks"] += 1
        if offset is None:
            break
    return sorted(docs.values(), key=lambda d: d["filename"].lower())


def search(query_vector: list[float], top_k: int = 5, filenames: list[str] | None = None):
    """Nearest chunks. If `filenames` is given, only those documents are searched."""
    query_filter = None
    if filenames:
        query_filter = Filter(
            must=[FieldCondition(key="filename", match=MatchAny(any=filenames))]
        )
    return client.query_points(
        collection_name=COLLECTION_NAME,
        query=query_vector,
        query_filter=query_filter,
        limit=top_k,
    ).points