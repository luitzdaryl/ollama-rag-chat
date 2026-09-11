from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams

client = QdrantClient(host="localhost", port=6333)

client.recreate_collection(
    collection_name="test",
    vectors_config=VectorParams(size=4, distance=Distance.COSINE),
)

print("Collections:", client.get_collections())