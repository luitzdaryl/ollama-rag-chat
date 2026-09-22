from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct
import uuid

COLLECTION_NAME = "documents"
VECTOR_SIZE = 1024  # must match bge-m3's actual output size

client = QdrantClient(host="localhost", port=6333)


def ensure_collection():
    """Create the collection once, if it doesn't already exist. Never
    recreates — that would silently wipe every stored document."""
    if not client.collection_exists(COLLECTION_NAME):
        client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
        )


def store_chunks(filename: str, chunks: list[str], vectors: list[list[float]]):
    """One point per chunk: its vector, plus the original text and source
    filename as payload, so retrieval can return both the match AND its source."""
    points = [
        PointStruct(
            id=str(uuid.uuid4()),
            vector=vector,
            payload={"filename": filename, "text": chunk},
        )
        for chunk, vector in zip(chunks, vectors)
    ]
    client.upsert(collection_name=COLLECTION_NAME, points=points)


def delete_by_filename(filename: str):
    """Remove every chunk belonging to one file — needed for the 'remove
    file' feature the frontend will expose."""
    client.delete(
        collection_name=COLLECTION_NAME,
        points_selector={"filter": {"must": [{"key": "filename", "match": {"value": filename}}]}},
    )


def search(query_vector: list[float], top_k: int = 5):
    """Find the most similar stored chunks to a given query vector."""
    return client.query_points(
        collection_name=COLLECTION_NAME,
        query=query_vector,
        limit=top_k,
    ).points