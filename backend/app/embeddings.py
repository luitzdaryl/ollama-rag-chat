import os

import requests

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "bge-m3")
EMBED_BATCH_SIZE = int(os.environ.get("EMBED_BATCH_SIZE", "16"))


def embed_texts(texts: list[str], batch_size: int | None = None) -> list[list[float]]:
    """Embed many texts with one HTTP request per batch instead of one per text.
    Output order matches input order."""
    batch_size = batch_size or EMBED_BATCH_SIZE
    vectors: list[list[float]] = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        response = requests.post(
            f"{OLLAMA_BASE_URL}/api/embed",
            json={"model": EMBEDDING_MODEL, "input": batch},
            timeout=300,
        )
        response.raise_for_status()
        embeddings = response.json()["embeddings"]
        if len(embeddings) != len(batch):
            raise RuntimeError(f"Ollama returned {len(embeddings)} embeddings for {len(batch)} inputs")
        vectors.extend(embeddings)
    return vectors


def embed_text(text: str) -> list[float]:
    """Single-text convenience wrapper (used for questions)."""
    return embed_texts([text])[0]


if __name__ == "__main__":
    vecs = embed_texts(["hello", "world"])
    print(len(vecs), "vectors of length", len(vecs[0]))