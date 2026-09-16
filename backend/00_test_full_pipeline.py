from document_processing import extract_text
from chunking import chunk_text
from embeddings import embed_text
from vector_store import ensure_collection, store_chunks, search

FILE_PATH = "/path/to/PTS-Smart-Scheduler-USER_MANUAL.en-09-07-2026.pdf"  # update this

ensure_collection()

text = extract_text(FILE_PATH)
chunks = chunk_text(text)
print(f"Extracted and chunked into {len(chunks)} chunks")

vectors = [embed_text(chunk) for chunk in chunks]
print(f"Embedded {len(vectors)} chunks")

store_chunks(filename="PTS-Manual.pdf", chunks=chunks, vectors=vectors)
print("Stored in Qdrant")

# Now the real test: ask a question, see if retrieval finds something relevant
question = "How do I install the scheduler?"
question_vector = embed_text(question)
results = search(question_vector, top_k=3)

print(f"\nTop {len(results)} results for: '{question}'")
for i, r in enumerate(results):
    print(f"\n--- Result {i+1} (score: {r.score:.4f}) ---")
    print(f"Source: {r.payload['filename']}")
    print(r.payload['text'][:300])