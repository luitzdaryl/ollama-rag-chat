# Ollama RAG Chat

Chat with your own documents, entirely on your own machine. Upload PDFs, Word, Excel, CSV, Markdown or text files, ask questions in plain language, and get answers grounded in those files, with the exact passages each answer came from shown underneath. Language models run locally through [Ollama](https://ollama.com), and your documents and chats are stored in databases on your own computer.


<!-- Add screenshots to a docs/ folder, then uncomment:
![Chat with sources](docs/chat-dark.png)
![Chat with sources](docs/chat-light.png)
![Advanced retrieval settings](docs/settings-dark.png)
![Advanced retrieval settings](docs/settings-light.png)
-->

**Contents:** 
- [Features](#features) 
- [Quick start](#quick-start) 
- [How it works](#how-it-works) 
- [Using the app](#using-the-app) 
- [Configuration](#configuration) 
- [API](#api) 
- [Design notes](#design-notes) 
- [Known limitations](#known-limitations) 
- [Troubleshooting](#troubleshooting) 
- [Project structure](#project-structure) 
- [Roadmap](#roadmap)

## Features

**Answers you can verify**
- Every answer lists the passages it was built from, with similarity score, keyword rank and (if enabled) reranker score. Expand a source to read the exact text.
- Choose which documents are searched with a checkbox per file.
- When nothing in your documents is relevant (for example "thanks!"), retrieval is skipped and the model simply replies.

**Retrieval pipeline**
- **Structure-aware chunking:** documents are split at headings first; only long sections are split further, at paragraph and sentence boundaries, with overlap. Continuation chunks carry their section heading.
- **Hybrid search:** meaning (dense embeddings with `bge-m3`) and exact terms (BM25-style keyword vectors) are searched together and merged with reciprocal-rank fusion.
- **Optional cross-encoder reranker** that re-reads the candidate passages against your question.
- **Follow-up handling:** a question like "and what about the second one?" is rewritten into a standalone search query using the recent conversation.

**Chat experience**
- Multiple chats in a sidebar, **stored in Postgres**: they survive page refreshes, switching browsers and rebuilding containers. Searchable by title and message text, renamable, and every delete asks for confirmation.
- Streaming answers with a Stop button, edit-and-resend, regenerate and copy.
- Markdown with tables, syntax-highlighted code with a Copy button, and LaTeX math. Model output is sanitised with DOMPurify before display.
- Per-message timestamp, response time and model name. Dark and light themes.

**Documents**
- Upload several files at once (`.pdf` `.docx` `.xlsx` `.csv` `.md` `.txt`). Re-uploading a file replaces the old version. The Documents tab shows chunk count, size and upload date.

**Tunable**
- Edit the answering instruction, and adjust retrieval thresholds, Top-K, keyword search, reranking and conversation memory from the Settings tab.

## Quick start

### Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Compose v2)
- [Ollama](https://ollama.com) running on the host machine, with an embedding model and at least one chat model:

```bash
ollama pull bge-m3   embeddinggemma:latest     # required: used to embed documents and questions or any embedding model you prefer 
ollama pull llama3.2  ministral-3:3b   # or any chat model you prefer
```

### Run

```bash
git clone https://github.com/luitzdaryl/ollama-rag-chat.git
cd ollama-rag-chat
docker compose up --build -d
```

Open **http://localhost:8080**. The first build takes a few minutes. If you hit `error getting credentials`, see [Docker credential helper error](#docker-error-docker-credential-desktop-not-found).

| Service  | URL / port                      | Notes                                               |
|----------|---------------------------------|-----------------------------------------------------|
| Frontend | http://localhost:8080           | The app                                             |
| Backend  | http://localhost:8000           | FastAPI; interactive docs at `/docs`                |
| Qdrant   | http://localhost:6333/dashboard | Vector database (documents)                         |
| Postgres | `localhost:5432`                | Chats. User `rag`, database `ragchat`               |

All four ports are bound to `127.0.0.1`, so they are reachable from your machine only. Do not change that without adding authentication first (see [Known limitations](#known-limitations)).

### Daily use

| I want to… | Command (from the project folder) |
|---|---|
| Start | `docker compose up -d` |
| Start after changing code or `requirements.txt` | `docker compose up --build -d` |
| Pause (keeps containers) | `docker compose stop`, then `docker compose start` |
| Stop and remove containers (keeps all data) | `docker compose down` |
| Delete everything: chats, documents, model cache | `docker compose down -v` |
| See state / logs | `docker compose ps` / `docker compose logs -f backend` |

Docker Desktop and Ollama must be running first. Containers use `restart: unless-stopped`: after a reboot they come back when Docker Desktop starts, unless you stopped them with `docker compose stop`.

`http://localhost:8080` (Docker) and `http://localhost:5173` (`npm run dev`) show the same chats and documents, but UI settings (theme, retrieval options, instruction text) are stored per address.

### Platform notes

- **macOS (Apple Silicon):** developed and tested here. Ollama stays outside Docker because containers on macOS cannot use the GPU.
- **Windows:** untested. Use Docker Desktop (WSL2) and Ollama for Windows. If the model dropdown is empty, set `OLLAMA_HOST=0.0.0.0` for Ollama and allow it through the firewall. Raise the WSL2 memory limit in `%UserProfile%\.wslconfig` if the backend is killed. Running without Docker needs `uvloop` removed or marked `sys_platform != "win32"` in `requirements.txt`.
- **Linux:** untested. Install Docker Engine with the Compose plugin. Ollama must listen on an address the container can reach (`OLLAMA_HOST=0.0.0.0`, restricted by a firewall), because `host.docker.internal` does not reach `127.0.0.1` on Linux.

### Where your data lives

| Data | Stored in | Survives `down` | Survives `down -v` |
|---|---|---|---|
| Chats and messages | Postgres, volume `postgres-data` | yes | **no** |
| Documents (chunks, vectors) | Qdrant, volume `qdrant-data` | yes | **no** |
| Reranker model (about 2.3 GB) | volume `model-cache` | yes | **no** |

Your knowledge base can always be rebuilt by uploading the files again, so chats are the data worth backing up.

**Back up and restore chats:**

```bash
# back up
docker compose exec postgres pg_dump -U rag --clean --if-exists -f /tmp/chats-backup.sql ragchat
docker compose cp postgres:/tmp/chats-backup.sql ./chats-backup.sql

# restore (stop the backend first so nothing writes during the restore)
docker compose stop backend
docker compose cp ./chats-backup.sql postgres:/tmp/chats-backup.sql
docker compose exec postgres psql -U rag -d ragchat -f /tmp/chats-backup.sql
docker compose start backend
```

**Changing the database password.** Put `POSTGRES_PASSWORD=...` in a `.env` file next to `docker-compose.yml` (use URL-safe characters, and do not commit that file). Postgres only reads the password when its volume is first created, so on an existing install the change has no effect until you run `docker compose down -v` (which deletes your chats) or change it inside Postgres with `ALTER USER`.

### Development: run the app code without Docker

```bash
docker compose up -d qdrant postgres        # the databases still run in Docker

cd backend
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cd app && python3 main.py                   # http://localhost:8000 (auto-reloads)

cd ../../frontend                           # in a second terminal
npm install
npm run dev                                 # http://localhost:5173
```

The backend's defaults (`localhost` for Qdrant, Postgres and Ollama) match this setup. If you changed `POSTGRES_PASSWORD`, set `DATABASE_URL` to match.

## How it works

**System overview**

```mermaid
flowchart LR
    B["Browser"] -->|"static files"| F["frontend<br/>(nginx, :8080)"]
    B -->|"API calls"| A["backend<br/>(FastAPI, :8000)"]
    A --> Q[("Qdrant<br/>document vectors")]
    A --> P[("Postgres<br/>chats and messages")]
    A --> O["Ollama<br/>(on the host)"]
```

**Indexing (on upload)**

```mermaid
flowchart LR
    A["Uploaded file<br/>pdf, docx, xlsx, csv, md, txt"] --> B["Extract and clean text"]
    B --> C["Split at headings,<br/>then at sentence boundaries"]
    C --> D["Embed with bge-m3<br/>(via Ollama)"]
    C --> E["Keyword weights<br/>(BM25-style)"]
    D --> F[("Qdrant<br/>dense + sparse vectors")]
    E --> F
```

**Answering (on each question)**

```mermaid
flowchart LR
    Q["Question"] --> R["Rewrite follow-ups<br/>into standalone queries"]
    R --> S1["Semantic search"]
    R --> S2["Keyword search"]
    S1 --> M["Merge rankings (RRF)"]
    S2 --> M
    M --> G{"Relevance gate<br/>(cosine similarity)"}
    G -->|passes| K["Cross-encoder reranker<br/>(optional)"]
    G -->|nothing relevant| N["Answer without documents"]
    K --> P["Prompt: instruction +<br/>top passages + question"]
    N --> L
    P --> L["Ollama chat model<br/>(streamed)"]
    L --> A["Answer + sources"]
```

| Layer     | Technology |
|-----------|------------|
| Frontend  | Vue 3, Vite, `marked`, highlight.js, KaTeX, DOMPurify |
| Backend   | FastAPI, httpx, document parsers (`pypdf`, `python-docx`, `openpyxl`) |
| Models    | Ollama: `bge-m3` for embeddings, any chat model for answers |
| Vector DB | Qdrant: one dense and one sparse vector per chunk |
| Chat DB   | Postgres 17 via `psycopg` 3, schema managed by numbered SQL migrations |
| Reranker  | `fastembed` cross-encoder (ONNX, runs inside the backend) |
| Packaging | Docker Compose |

## Using the app

1. **Documents tab:** upload one or more files. Tick or untick files to control what is searched.
2. **Chat tab:** ask a question. The line under the chat shows which documents are in use. Open **Sources** under an answer to read the retrieved passages. If a follow-up was rewritten, the rewritten query is shown. A new chat appears in the database once you send its first message.
3. **Settings tab:** turn the knowledge base on or off, edit the instruction the model receives, and open **Advanced retrieval**:

| Setting | Default | What it does |
|---|---|---|
| Keyword search (hybrid) | on | Also match exact words, not only meaning |
| Reranker | off | Re-read candidates against the question; the first use downloads a model |
| Minimum rerank score | off | Drop passages the reranker scores below this (the scale depends on the model) |
| Passages sent to the model (Top-K) | 3 | How many passages go into the prompt |
| Close-match threshold | 0.45 | Cosine similarity at which a passage is always used |
| Keyword-match floor | 0.30 | A keyword hit also needs at least this similarity |
| Keyword-rank limit | 5 | A keyword hit must rank in the top N keyword results |
| Conversation memory | 5 exchanges | Previous question/answer pairs the model sees. Follow-up searches always look at the recent conversation, whatever this is set to |

**Coming from an earlier version?** Chats used to live in the browser. Open the app at the address you used before (for example `http://localhost:5173`) and click **Import to database** in the sidebar. Importing is safe to repeat, and the copy in your browser is left untouched.

## Configuration

Environment variables for the backend (set in `docker-compose.yml`):

| Variable | Default | Purpose |
|---|---|---|
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Where Ollama is reachable (Compose uses `host.docker.internal`) |
| `QDRANT_HOST` / `QDRANT_PORT` | `localhost` / `6333` | Qdrant location (Compose uses the `qdrant` service) |
| `DATABASE_URL` | `postgresql://rag:ragchat_dev@localhost:5432/ragchat` | Postgres connection (Compose points it at the `postgres` service) |
| `EMBEDDING_MODEL` | `bge-m3` | Ollama embedding model. Changing it requires re-uploading documents, and the vector size in `vector_store.py` must match |
| `EMBED_BATCH_SIZE` | `16` | Chunks per embedding request |
| `RERANK_MODEL` | set in `rerank.py` | Compose pins `BAAI/bge-reranker-v2-m3` (multilingual, about 2.3 GB). `BAAI/bge-reranker-base` is a lighter alternative |
| `FASTEMBED_CACHE_PATH` | system temp dir | Where reranker models are stored (Compose mounts a volume at `/models`) |

Compose-level variable (read from your shell or a `.env` file): `POSTGRES_PASSWORD`, default `ragchat_dev`.

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | Liveness check |
| GET | `/api/models` | Chat models installed in Ollama |
| POST | `/api/chat` | Streamed answer (see below) |
| POST | `/api/documents/upload` | Multipart upload of one file; replaces a file with the same name |
| GET | `/api/documents` | Documents with chunk count, size and upload date |
| DELETE | `/api/documents/{filename}` | Remove a document and all its chunks |
| POST | `/api/rerank/warmup` | Load (and on first use, download) the reranker |
| GET | `/api/chats` | List chats (titles and dates, no messages) |
| GET | `/api/chats/search?q=` | Ids of chats whose title or messages contain the text |
| GET | `/api/chats/{id}` | One chat with all its messages |
| PUT | `/api/chats/{id}` | Create or fully replace a chat (one transaction) |
| PATCH | `/api/chats/{id}` | Rename a chat |
| DELETE | `/api/chats/{id}` | Delete a chat and its messages (idempotent) |
| POST | `/api/chats/import` | One-time import of chats from browser storage |

**`POST /api/chat`** takes `model`, `messages`, `prompt_instruction`, `use_knowledge_base`, `document_filter` (`null` = all documents, `[]` = no retrieval, or a list of filenames) and `retrieval_settings`. The response is `text/plain`, streamed. The **first line** is `__META__` followed by JSON (`sources`, `search_query`, `rerank_status`); everything after that line is the answer text.

## Design notes

**Chunking matters more than it looks.** My first version cut documents every 800 characters. On a 24-page user manual, the question "How do I install the scheduler?" ranked the real installation commands third (cosine 0.61), cut off mid-command, behind a chunk of generic interface descriptions (0.67). Splitting at the manual's numbered headings fixed it: `2. Prerequisites` (0.68) and `3. Installation` (0.65) came back intact at ranks 1 and 2, and the table of contents ranked last. Cleaning the table of contents' dot leaders also removed about 16% of the extracted text, which had been polluting embeddings. This is one document and one question, an anecdote rather than a benchmark.

**The relevance gate uses cosine similarity, not fused scores.** Reciprocal-rank-fusion scores are rank-based and on a different scale than cosine, so a threshold on them would be meaningless. I merge the two result lists in code, keep each passage's real cosine score, and gate on that. This is also why "thanks!" retrieves nothing.

**Keyword vectors use a stable hash.** Python's built-in `hash()` is randomised per process, which would give the same word a different index after every restart. Tokens are hashed with CRC32 instead. Qdrant applies IDF weighting server-side, so it tracks the corpus as documents come and go.

**Chats are rows, not a blob.** Messages live in their own table keyed by `(chat_id, position)`, with the retrieved sources as JSONB because their shape varies. The sidebar loads only titles; a chat's messages load when you open it, and search runs in Postgres rather than in the browser.

**Saves are whole-chat and ordered.** Each save replaces a chat's messages in one transaction, so a reader never sees half a chat. The browser queues saves per chat and each one sends the chat's current state when it runs, so a slow, older save can never overwrite a newer one. Cost: a save grows with chat length. The answer that is still streaming is never saved half-written.

**Failed saves recover on their own.** If the backend is down, the sidebar shows "Not saved". Because every save sends the whole chat, the next successful one catches everything up.

**Deleted chats stay deleted.** A chat is marked deleted before the delete request is sent, so a save that was still queued cannot re-create it.

**Postgres rejects NUL characters,** and PDF extraction occasionally produces them. They are stripped before saving, so a single odd character cannot make a chat unsaveable.

**Schema changes are numbered SQL files** (`backend/app/migrations/001_chats.sql`, and so on), applied at startup and recorded in a `schema_migrations` table, under an advisory lock so two starting processes can't collide.

**Failed re-uploads don't destroy data.** A replaced document is deleted only after the new version has been embedded successfully.

**Blocking work stays off the event loop.** Embedding, searching, reranking and database calls run in worker threads (or in plain `def` routes), so a large upload doesn't freeze chat.

**Stale data silently skews retrieval.** Early on, leftover chunks from an older chunking experiment kept showing up in results. That is why documents are keyed by filename, replaced on re-upload, and removable.

## Known limitations

- **No authentication.** Anyone who can reach port 8000 can read and delete every chat and document. All ports are bound to `127.0.0.1` for that reason. Do not expose the app to a network as it is.
- **One shared knowledge base and one shared chat history.** There are no users or per-user isolation.
- **The frontend calls `http://localhost:8000`** (set in `App.vue`), so it works from the same machine only unless you change it.
- **Saves replace the whole chat,** and two browser tabs are last-write-wins. A new chat exists only in the page until its first message is sent, and an answer that is mid-stream when you refresh is lost.
- **Chat search is a substring match** (`ILIKE`) scanning all messages. Fine for personal use; a trigram index would be the next step.
- **No OCR.** Scanned PDFs (pictures of text) produce no text and are rejected. PDF tables are flattened into lines of text.
- **Heading detection** covers numbered headings (`3. Installation`) and Markdown `#` headings only. Other documents fall back to paragraph and sentence splitting.
- **Keyword search has no stemming** ("installing" does not match "install"; the semantic side covers that), and the stop-word list is English. Chinese, Japanese and Korean text is tokenised into character pairs.
- **Thresholds were tuned by hand on a few documents,** not against a labelled test set. Treat the defaults as starting points.
- **Answer quality is bounded by the chat model.** Small local models can ignore the "only use the context" instruction.
- **The reranker is large** (about 2.3 GB by default), needs several GB of RAM, and adds latency per question.
- Documents are identified by filename: two different files with the same name replace each other.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `error getting credentials … docker-credential-desktop` | See [the section below](#docker-error-docker-credential-desktop-not-found) |
| `Cannot connect to the Docker daemon` | Start Docker Desktop |
| `port is already allocated` (8000, 8080, 5432 or 6333) | Stop whatever uses it: a local `python3 main.py`, `npm run dev`, or a local Postgres. For Postgres you can instead change the left side of `127.0.0.1:5432:5432` in `docker-compose.yml` (for example `5433:5432`) |
| Model dropdown is empty | The backend can't reach Ollama. Check `ollama list`. In Docker, test with `docker compose exec backend python -c "import urllib.request; print(urllib.request.urlopen('http://host.docker.internal:11434/api/tags').status)"` |
| Upload fails with an embedding error | Run `ollama pull bge-m3` |
| Sidebar says "Loading chats…" forever, or "Could not load chats" | The backend or Postgres isn't ready. Run `docker compose ps` and `docker compose logs backend --tail 40` |
| Sidebar shows "Not saved: …" | The backend couldn't be reached or rejected the save. The chat on screen is intact and is saved in full by the next successful save |
| Chats from an older version are missing | They are in the browser at the old address (for example `localhost:5173`). Open it there and use **Import to database** |
| All chats and documents gone | `docker compose down -v` deletes the volumes. Restore chats from a backup (see [Where your data lives](#where-your-data-lives)) |
| Backend exits with code 137 | Out of memory. Raise Docker Desktop's memory limit or use a lighter `RERANK_MODEL` |
| Reranker takes forever the first time | It is downloading about 2.3 GB, once. Watch `docker compose logs -f backend` |
| Answers ignore my documents | Check the status line under the chat, lower the close-match threshold, and look at Sources to see what was retrieved |

### Docker error: `docker-credential-desktop` not found

```
error getting credentials - err: exec: "docker-credential-desktop": executable file not found in $PATH
```

**What it means.** Docker's config file, `~/.docker/config.json`, contains `"credsStore": "desktop"`. That tells Docker to run a helper program called `docker-credential-desktop` to look up registry credentials, and your shell cannot find it. Public images such as Postgres and Python need no login, so the lookup is pointless here, but Docker still tries it.

**Why it appears at odd moments.** Docker only asks for credentials when it has to pull an image it doesn't already have. A stack can run for weeks and then fail the day you add a service that needs a new image. It has also come back after Docker Desktop updates.

**Fix 1: remove the line (simplest).**

```bash
cat ~/.docker/config.json
nano ~/.docker/config.json          # delete the "credsStore": "desktop", line
python3 -m json.tool ~/.docker/config.json    # must print the file back without errors
```

If the deleted line wasn't the last entry, make sure no comma is left dangling before the closing `}`; `json.tool` will complain if so. A working file looks like this:

```json
{
	"auths": {},
	"currentContext": "desktop-linux"
}
```

Then retry:

```bash
docker pull postgres:17-alpine
docker compose up --build -d
```

**Fix 2: put Docker's helper on your PATH instead (keeps credential storage).**

```bash
ls /Applications/Docker.app/Contents/Resources/bin | grep credential   # confirm the helper exists
export PATH="$PATH:/Applications/Docker.app/Contents/Resources/bin"    # this terminal session only
echo 'export PATH="$PATH:/Applications/Docker.app/Contents/Resources/bin"' >> ~/.zshrc   # permanent
```

**Trade-off of Fix 1.** Without a credential store, a later `docker login` (to a private registry) saves your credentials in `config.json` in an unencrypted form. That doesn't matter for this project, which only pulls public images. If you do log in to private registries, use Fix 2.

## Project structure

```
ollama-rag-chat/
├── docker-compose.yml
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app/
│       ├── main.py                 # app setup, RAG pipeline, streaming chat, documents
│       ├── chats.py                # chat schemas, SQL and /api/chats routes
│       ├── database.py             # Postgres pool and migration runner
│       ├── migrations/
│       │   └── 001_chats.sql       # chats and messages tables
│       ├── document_processing.py  # text extraction per file type, cleanup
│       ├── chunking.py             # structure-aware chunking
│       ├── embeddings.py           # batched embeddings via Ollama
│       ├── sparse.py               # keyword (BM25-style) vectors
│       ├── vector_store.py         # Qdrant storage and hybrid search
│       └── rerank.py               # optional cross-encoder reranker
└── frontend/
    ├── Dockerfile
    └── src/
        ├── App.vue                 # the whole UI
        ├── markdown.js             # markdown, highlighting, math, sanitising
        └── assets/theme.css        # light and dark theme variables
```

## Roadmap

- Authentication and per-user chats and knowledge bases
- A small labelled evaluation set to measure retrieval changes instead of eyeballing them
- OCR for scanned PDFs
- Trigram or full-text index for chat search
- Upload progress for very large files

## Acknowledgements

Built on [Ollama](https://ollama.com), [Qdrant](https://qdrant.tech), [PostgreSQL](https://www.postgresql.org), [FastEmbed](https://github.com/qdrant/fastembed), and the `bge-m3` and `bge-reranker` models from BAAI.