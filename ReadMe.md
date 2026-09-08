# Ollama Chat App

A local chat interface for [Ollama](https://ollama.com) models, built with **FastAPI** (backend) and **Vue 3** (frontend). Streams responses live, lets you switch between any locally installed Ollama model, supports light/dark themes, and renders markdown (including tables and code blocks) in assistant replies.

## Features

- 🔄 **Live streaming responses** — text appears as the model generates it, not all at once
- 🧠 **Model switcher** — pick from any model you've already pulled with Ollama
- 🎨 **Light/dark theme toggle** — persisted across page reloads
- 📝 **Markdown rendering** — headings, lists, code blocks, and tables in assistant messages
- 💬 **Multi-turn context** — the full conversation is sent with each request, so the model remembers earlier messages

## Prerequisites

- [Ollama](https://ollama.com) installed and running locally, with at least one model pulled (e.g. `ollama pull llama3.2`)
- Python 3.10+
- Node.js 18+

## Project Structure

```
ollama-chat-app/
├── backend/
│   ├── app/
│   │   └── main.py
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── App.vue
│   │   ├── main.js
│   │   └── assets/
│   │       └── theme.css
│   └── package.json
|
|__ .gitignore
|
|__ docker-compose.yml
|
└── README.md
```

## Getting Started

### 1. Backend (FastAPI)

```bash
cd backend
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
cd app
python3 main.py
```

The API will be available at `http://localhost:8000`. Check `http://localhost:8000/docs` for the interactive API playground.

### 2. Frontend (Vue 3)

In a separate terminal:

```bash
cd frontend
npm install
npm run dev
```

The app will be available at `http://localhost:5173`.

### 3. Make sure Ollama is running

This app expects Ollama's API to be reachable at `http://localhost:11434` (Ollama's default). If you can run `ollama list` in your terminal and see your models, you're good to go.

## API Endpoints (Backend)

| Method | Endpoint       | Description                              |
|--------|----------------|-------------------------------------------|
| GET    | `/api/health`  | Health check                              |
| GET    | `/api/models`  | Lists installed Ollama models             |
| POST   | `/api/chat`    | Streams a chat response from a given model|

## Tech Stack

- **Backend:** FastAPI, httpx, Uvicorn
- **Frontend:** Vue 3 (Composition API), Vite, marked (markdown rendering)
- **Model runtime:** Ollama (external, run locally)

## Running with Docker (recommended)

The easiest way to run the full stack — no need to install Python or Node locally.

**Prerequisites:** Docker Desktop installed and running, and Ollama running natively on your machine with at least one model pulled.

From the project root:

```bash
docker compose up
```

or (if you made some changes):

```bash
docker compose up --build
```

> **Note:** If you make changes to the code after already running this once, use `docker compose up --build` instead — plain `up` reuses the previously built images and won't pick up new changes.

This builds and starts both the backend (`http://localhost:8000`) and frontend (`http://localhost:8080`) together. Open `http://localhost:8080` in your browser once both containers report as started.

To stop everything: `Ctrl+C`, or `docker compose down` from another terminal.

> **Note:** The backend reaches your locally-running Ollama via `host.docker.internal`, Docker's built-in DNS name for the host machine — this is configured automatically in `docker-compose.yml`.


## Roadmap

- [x] Dockerize backend and frontend
- [x] Docker Compose setup for one-command startup
- [ ] Deployment instructions (cloud hosting)

## License

MIT
