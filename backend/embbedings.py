import requests

OLLAMA_BASE_URL = "http://localhost:11434"
EMBEDDING_MODEL = "bge-m3"  # already pulled, per your earlier model list


def embed_text(text: str) -> list[float]:
    """Call Ollama's embedding endpoint, return the vector as a plain list of floats."""
    response = requests.post(
        f"{OLLAMA_BASE_URL}/api/embed",
        json={"model": EMBEDDING_MODEL, "input": text},
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    return data["embeddings"][0]  # one input -> one embedding, take the first


if __name__ == "__main__":
    vector = embed_text("The scheduler runs Ollama on 127.0.0.1:11434.")
    print(f"Vector length: {len(vector)}")
    print(f"First 5 values: {vector[:5]}")