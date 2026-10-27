import os
import uuid
from datetime import datetime, timezone

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    HasIdCondition,
    MatchAny,
    MatchValue,
    Modifier,
    PointStruct,
    SparseVector,
    SparseVectorParams,
    VectorParams,
)

from sparse import doc_sparse, query_sparse

# New name: the old "documents" collection has a different vector layout.
COLLECTION_NAME = "documents_hybrid"
DENSE = "dense"
SPARSE = "sparse"
VECTOR_SIZE = 1024  # must match bge-m3's output size
CANDIDATES = 20     # how many hits each search contributes before merging
RRF_K = 60          # standard reciprocal-rank-fusion constant
UPSERT_BATCH = 64

client = QdrantClient(
    host=os.environ.get("QDRANT_HOST", "localhost"),
    port=int(os.environ.get("QDRANT_PORT", "6333")),
)


def ensure_collection():
    """Create the collection once if missing. Never recreates it (that would wipe every document)."""
    if not client.collection_exists(COLLECTION_NAME):
        client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config={DENSE: VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE)},
            sparse_vectors_config={SPARSE: SparseVectorParams(modifier=Modifier.IDF)},
        )


def store_chunks(filename: str, chunks: list[str], vectors: list[list[float]], file_size: int = 0):
    """One point per chunk: a dense (meaning) vector, a sparse (keyword) vector, and the text."""
    uploaded_at = datetime.now(timezone.utc).isoformat()
    points = []
    for chunk, dense in zip(chunks, vectors):
        vector = {DENSE: dense}
        indices, values = doc_sparse(chunk)
        if indices:
            vector[SPARSE] = SparseVector(indices=indices, values=values)
        points.append(
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
        )
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
    """Per-file details, aggregated by scrolling payloads only (no vectors)."""
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
                    "uploaded_at": payload.get("uploaded_at"),
                },
            )
            doc["chunks"] += 1
        if offset is None:
            break
    return sorted(docs.values(), key=lambda d: d["filename"].lower())

    
def hybrid_search(
    query_vector: list[float],
    query_text: str,
    filenames: list[str] | None = None,
    candidates: int = CANDIDATES,
    use_keyword: bool = True,
) -> list[dict]:    
    """Run a semantic search AND a keyword search, then merge them with
    reciprocal-rank fusion.

    I merge here instead of using Qdrant's built-in fusion on purpose: fused
    scores are rank-based numbers on a different scale than cosine similarity,
    which would break the relevance threshold that makes "thanks!" skip
    retrieval. Every result keeps its real cosine score in `dense`."""
    query_filter = None
    if filenames:
        query_filter = Filter(must=[FieldCondition(key="filename", match=MatchAny(any=filenames))])

    dense_hits = client.query_points(
        collection_name=COLLECTION_NAME,
        query=query_vector,
        using=DENSE,
        query_filter=query_filter,
        limit=candidates,
    ).points

    sparse_hits = []
    indices, values = query_sparse(query_text)
    if indices:  # a query made only of stopwords has no keywords to search for
        sparse_hits = client.query_points(
            collection_name=COLLECTION_NAME,
            query=SparseVector(indices=indices, values=values),
            using=SPARSE,
            query_filter=query_filter,
            limit=candidates,
        ).points

    found: dict = {}

    def entry(p):
        return found.setdefault(
            p.id,
            {
                "id": p.id,
                "filename": p.payload["filename"],
                "text": p.payload["text"],
                "dense": None,
                "dense_rank": None,
                "keyword_rank": None,
            },
        )

    for rank, p in enumerate(dense_hits, start=1):
        e = entry(p)
        e["dense"], e["dense_rank"] = p.score, rank
    for rank, p in enumerate(sparse_hits, start=1):
        entry(p)["keyword_rank"] = rank

    # A chunk found only by keywords has no cosine score yet; fetch it, so the
    # relevance gate always compares real similarities.
    missing = [pid for pid, e in found.items() if e["dense"] is None]
    if missing:
        extra = client.query_points(
            collection_name=COLLECTION_NAME,
            query=query_vector,
            using=DENSE,
            query_filter=Filter(must=[HasIdCondition(has_id=missing)]),
            limit=len(missing),
        ).points
        for p in extra:
            found[p.id]["dense"] = p.score

    for e in found.values():
        e["rrf"] = sum(
            1.0 / (RRF_K + r) for r in (e["dense_rank"], e["keyword_rank"]) if r is not None
        )
    return sorted(found.values(), key=lambda e: e["rrf"], reverse=True)