import os
import threading
import time

RERANK_MODEL = os.environ.get("RERANK_MODEL", "BAAI/bge-reranker-base")
MAX_PASSAGE_CHARS = 2000  # safety cap; our chunks are far shorter than this

_model = None
_load_error: str | None = None
_lock = threading.Lock()  # one reranking job at a time (it is CPU-bound anyway)


def _load():
    """Load the model once. Call with _lock held. A failed load is remembered,
    so we don't retry the download on every question; restart to retry."""
    global _model, _load_error
    if _model is not None or _load_error is not None:
        return
    try:
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        print(f"[rerank] loading {RERANK_MODEL} (the first run downloads it)...")
        _model = TextCrossEncoder(model_name=RERANK_MODEL)
        print("[rerank] ready")
    except Exception as e:
        _load_error = f"{type(e).__name__}: {e}"
        print(f"[rerank] could not load {RERANK_MODEL}: {_load_error}")


def warmup() -> dict:
    with _lock:
        _load()
    return {"ready": _model is not None, "error": _load_error}


def rerank(query: str, texts: list[str]) -> list[float] | None:
    """Score each passage against the query (higher = better match), in input order.
    Returns None if the reranker is unavailable, so callers can fall back safely."""
    if not texts:
        return []
    with _lock:
        _load()
        if _model is None:
            return None
        try:
            started = time.perf_counter()
            scores = [float(s) for s in _model.rerank(query, [t[:MAX_PASSAGE_CHARS] for t in texts])]
        except Exception as e:
            print(f"[rerank] failed: {type(e).__name__}: {e}")
            return None
    print(
        f"[rerank] scored {len(texts)} passages in {time.perf_counter() - started:.2f}s, "
        f"raw scores: {[round(s, 2) for s in scores]}"
    )
    return scores