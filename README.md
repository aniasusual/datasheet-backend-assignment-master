# Datasheet Extraction Backend

A vision-first extraction pipeline for industrial process datasheets. Upload PDF datasheets, extract structured equipment data using an LLM (Gemini 2.5 Flash), review and correct fields through a human-in-the-loop interface, and query extracted data via a conversational agent.

## Demo - https://drive.google.com/file/d/1yFpbo9R0DwVTU7kMvMWRZrMCWuvZ8Q84/view?usp=sharing

## Tech Stack

- **Backend:** FastAPI, SQLAlchemy (async), Alembic, LiteLLM
- **Database:** PostgreSQL 16, Redis 7
- **LLM:** Gemini 2.5 Flash (via LiteLLM — supports OpenAI/Anthropic too)
- **Frontend:** React 19, TypeScript, TailwindCSS, Vite
- **PDF Processing:** PyMuPDF (page counting), native LLM vision (no text extraction)

---

## Prerequisites

- **Python 3.11+**
- **Node.js 18+** and **npm**
- **Docker** and **Docker Compose** (for PostgreSQL and Redis)
- **Gemini API Key** — get one from [Google AI Studio](https://aistudio.google.com/apikey)

---

## Local Setup

### 1. Clone the repository

```bash
git clone <repo-url>
cd datasheet-backend-assignment-master
```

### 2. Start PostgreSQL and Redis

```bash
docker compose up -d
```

This starts:
- **PostgreSQL 16** on port `5432` (user: `postgres`, password: `postgres`, db: `datasheet_extraction`)
- **Redis 7** on port `6379`

### 3. Create a Python virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 4. Install Python dependencies

```bash
pip install -e ".[dev]"
```

### 5. Configure environment variables

Create a `.env` file in the project root:

```env
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/datasheet_extraction
REDIS_URL=redis://localhost:6379/0
LLM_MODEL=gemini/gemini-2.5-flash
LLM_API_KEY=your-gemini-api-key-here
GEMINI_API_KEY=your-gemini-api-key-here
UPLOAD_DIR=./uploads
RENDERED_PAGES_DIR=./rendered_pages
```

Replace `your-gemini-api-key-here` with your actual Gemini API key.

### 6. Run database migrations

```bash
alembic upgrade head
```

### 7. Start the backend server

```bash
uvicorn app.main:app --reload --reload-dir app --port 8000
```

> **Note:** Use `--reload-dir app` to avoid unnecessary reloads from `.venv/` and other directories.

The API will be available at `http://localhost:8000`.

### 8. Start the frontend dev server

```bash
cd app/frontend
npm install
npm run dev
```

The frontend will be available at `http://localhost:5173` and proxies API requests to the backend.

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/health` | Health check |
| `POST` | `/api/v1/sessions` | Create a new session |
| `GET` | `/api/v1/sessions` | List all sessions |
| `GET` | `/api/v1/sessions/{id}` | Get session details |
| `POST` | `/api/v1/sessions/{id}/documents/upload` | Upload PDF datasheets |
| `GET` | `/api/v1/sessions/{id}/documents` | List documents in a session |
| `POST` | `/api/v1/sessions/{id}/documents/{doc_id}/extract` | Trigger LLM extraction |
| `GET` | `/api/v1/sessions/{id}/fields` | List extracted fields |
| `PUT` | `/api/v1/sessions/{id}/fields/{field_id}/correct` | Correct a field value |
| `PUT` | `/api/v1/sessions/{id}/fields/{field_id}/verify` | Verify a field |
| `PUT` | `/api/v1/sessions/{id}/fields/{field_id}/reject` | Reject a field |
| `GET` | `/api/v1/sessions/{id}/entities` | List equipment entities |
| `POST` | `/api/v1/sessions/{id}/query` | Single-shot query |
| `POST` | `/api/v1/sessions/{id}/agent` | Conversational agent query |

---

## How It Works

1. **Upload** — PDF datasheets are uploaded and stored on disk. PyMuPDF counts pages.
2. **Extract** — The full PDF is sent as base64 to Gemini 2.5 Flash, which reads it natively via vision (no OCR or text extraction). A single LLM call returns structured JSON with equipment metadata, field values, units, and citations.
3. **Review** — The React UI displays extracted fields alongside a PDF viewer. Users can verify, correct, or reject individual fields. All corrections are audited.
4. **Query** — A conversational agent answers questions about extracted data using the full dataset as context.

---

## Running Tests

```bash
pytest
```

Tests use `pytest-asyncio` with session-scoped fixtures. Make sure PostgreSQL is running before running tests.

---

## Sample Datasheets

The repo includes 4 sample PDF datasheets for testing:

- `pds-P718.pdf`
- `pds-P818.pdf`
- `pds-P300228.pdf`
- `pds-P600173.pdf`

---

## Project Structure

```
app/
├── main.py              # FastAPI app with CORS and routers
├── config.py            # Settings loaded from .env
├── database.py          # Async SQLAlchemy engine and sessions
├── api/                 # Route handlers
├── models/              # SQLAlchemy ORM models
├── schemas/             # Pydantic request/response schemas
├── services/            # Business logic (extraction, agent, queries)
└── frontend/            # React + TypeScript UI
alembic/                 # Database migrations
tests/                   # Test suite
docker-compose.yml       # PostgreSQL + Redis
```

---

## Troubleshooting

- **`uvicorn` not found** — Make sure your virtual environment is activated: `source .venv/bin/activate`
- **Constant reloading** — Use `--reload-dir app` to restrict file watching to the `app/` directory only
- **Database connection refused** — Ensure Docker containers are running: `docker compose ps`
- **Migration errors** — Ensure the database exists: `docker compose up -d` then `alembic upgrade head`
