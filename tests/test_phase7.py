"""Phase 7 tests: Chat API, async job execution, session detail enrichment."""

import json
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import app
from app.models.cost_record import CostRecord
from app.models.document import Document, DocumentStatus
from app.models.extracted_field import ExtractedField, FieldDataType, FieldStatus
from app.models.message import Message, MessageRole
from app.models.session import Session, SessionStatus


# ──────────────────── Helpers ────────────────────


async def _create_session(db: AsyncSession) -> Session:
    session = Session(status=SessionStatus.active)
    db.add(session)
    await db.flush()
    return session


async def _create_session_with_data(db: AsyncSession) -> Session:
    """Create a session with documents, fields, messages, and cost records."""
    session = Session(status=SessionStatus.active)
    db.add(session)
    await db.flush()

    # Add a document
    doc = Document(
        session_id=session.id,
        filename="test.pdf",
        file_path="/tmp/test.pdf",
        status=DocumentStatus.uploaded,
        num_pages=2,
    )
    db.add(doc)
    await db.flush()

    # Add extracted fields
    for i in range(3):
        field = ExtractedField(
            document_id=doc.id,
            field_name=f"field_{i}",
            display_name=f"Field {i}",
            raw_value=f"value_{i}",
            data_type=FieldDataType.text,
            section="general_info",
            confidence=0.9,
            status=FieldStatus.extracted,
            citation_page=1,
            citation_text=f"source text {i}",
        )
        db.add(field)

    # Add messages
    for i in range(5):
        role = MessageRole.user if i % 2 == 0 else MessageRole.assistant
        msg = Message(
            session_id=session.id,
            seq_number=i,
            role=role,
            content=f"Message {i}",
        )
        db.add(msg)

    # Add cost records
    for op in ["query", "extraction"]:
        cr = CostRecord(
            session_id=session.id,
            operation=op,
            model="test-model",
            input_tokens=1000,
            output_tokens=500,
            cost_usd=0.05,
            duration_sec=1.0,
        )
        db.add(cr)

    await db.flush()
    return session


# ──────────────────── Session Detail Endpoint Tests ────────────────────


class TestSessionDetailEndpoint:
    @pytest.mark.asyncio
    async def test_session_detail_with_stats(self, db: AsyncSession):
        """GET /sessions/{id} returns field_count, message_count, cost_total."""
        session = await _create_session_with_data(db)
        await db.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/api/v1/sessions/{session.id}")

        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == str(session.id)
        assert data["document_count"] == 1
        assert data["field_count"] == 3
        assert data["message_count"] == 5
        assert data["cost_total"] == pytest.approx(0.10, abs=0.001)

    @pytest.mark.asyncio
    async def test_session_detail_empty_session(self, db: AsyncSession):
        """GET /sessions/{id} returns zeros for empty session."""
        session = await _create_session(db)
        await db.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/api/v1/sessions/{session.id}")

        assert resp.status_code == 200
        data = resp.json()
        assert data["document_count"] == 0
        assert data["field_count"] == 0
        assert data["message_count"] == 0
        assert data["cost_total"] == 0.0

    @pytest.mark.asyncio
    async def test_session_detail_not_found(self):
        """GET /sessions/{id} returns 404 for non-existent session."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/api/v1/sessions/{uuid.uuid4()}")

        assert resp.status_code == 404


# ──────────────────── Chat Endpoint Tests ────────────────────


class TestChatEndpoint:
    @pytest.mark.asyncio
    async def test_chat_sync_simple_query(self, db: AsyncSession, monkeypatch):
        """POST /chat with a simple query runs agent synchronously."""
        session = await _create_session(db)
        await db.commit()

        # Mock the agent runner to avoid needing a real LLM
        from app.agent import llm_client

        async def mock_call_llm(**kwargs):
            return llm_client.LLMResponse(
                content="I found 2 documents in this session.",
                tool_calls=None,
                input_tokens=100,
                output_tokens=20,
                cost_usd=0.001,
                model="test-model",
                duration_sec=0.5,
            )

        # Patch at the runner module level
        from app.agent import runner
        monkeypatch.setattr(runner, "call_llm", mock_call_llm)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                f"/api/v1/sessions/{session.id}/chat",
                json={"message": "What documents are in this session?"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "completed"
        assert "2 documents" in data["response"]
        assert data["job_id"] is None

    @pytest.mark.asyncio
    async def test_chat_empty_message_rejected(self, db: AsyncSession):
        """POST /chat rejects empty messages."""
        session = await _create_session(db)
        await db.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                f"/api/v1/sessions/{session.id}/chat",
                json={"message": "   "},
            )

        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_chat_session_not_found(self):
        """POST /chat returns 404 for non-existent session."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                f"/api/v1/sessions/{uuid.uuid4()}/chat",
                json={"message": "Hello"},
            )

        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_chat_archived_session_rejected(self, db: AsyncSession):
        """POST /chat rejects messages to archived sessions."""
        session = Session(status=SessionStatus.archived)
        db.add(session)
        await db.flush()
        await db.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                f"/api/v1/sessions/{session.id}/chat",
                json={"message": "Hello"},
            )

        assert resp.status_code == 400
        assert "not active" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_chat_heavy_request_falls_back_to_sync(self, db: AsyncSession, monkeypatch):
        """Heavy requests fall back to sync when Arq/Redis is unavailable."""
        session = await _create_session(db)
        await db.commit()

        from app.agent import llm_client, runner

        async def mock_call_llm(**kwargs):
            return llm_client.LLMResponse(
                content="Extraction complete: 28 fields from P-718.",
                tool_calls=None,
                input_tokens=500,
                output_tokens=100,
                cost_usd=0.01,
                model="test-model",
                duration_sec=2.0,
            )

        monkeypatch.setattr(runner, "call_llm", mock_call_llm)

        # Make enqueue fail to force sync fallback
        from app.api import chat as chat_module
        monkeypatch.setattr(
            chat_module,
            "_is_heavy_request",
            lambda msg: True,
        )

        # Import the worker enqueue and make it fail
        import app.worker as worker_module

        async def mock_enqueue_fail(*args, **kwargs):
            raise ConnectionError("Redis unavailable")

        monkeypatch.setattr(worker_module, "enqueue_agent_job", mock_enqueue_fail)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                f"/api/v1/sessions/{session.id}/chat",
                json={"message": "Extract all fields from these datasheets"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "completed"
        assert "28 fields" in data["response"]

    @pytest.mark.asyncio
    async def test_chat_messages_persisted(self, db: AsyncSession, monkeypatch):
        """Chat messages are persisted to the database."""
        session = await _create_session(db)
        await db.commit()

        from app.agent import llm_client, runner

        async def mock_call_llm(**kwargs):
            return llm_client.LLMResponse(
                content="Response from agent.",
                tool_calls=None,
                input_tokens=100,
                output_tokens=20,
                cost_usd=0.001,
                model="test-model",
                duration_sec=0.3,
            )

        monkeypatch.setattr(runner, "call_llm", mock_call_llm)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                f"/api/v1/sessions/{session.id}/chat",
                json={"message": "Tell me about P-718"},
            )

        assert resp.status_code == 200

        # Verify messages in DB
        # Need a fresh db session since the endpoint used its own
        from app.database import async_session_factory
        async with async_session_factory() as fresh_db:
            stmt = (
                select(Message)
                .where(Message.session_id == session.id)
                .order_by(Message.seq_number)
            )
            result = await fresh_db.execute(stmt)
            msgs = result.scalars().all()
            assert len(msgs) >= 2
            assert msgs[0].role == MessageRole.user
            assert msgs[0].content == "Tell me about P-718"
            assert msgs[1].role == MessageRole.assistant
            assert msgs[1].content == "Response from agent."

    @pytest.mark.asyncio
    async def test_chat_with_tool_calls(self, db: AsyncSession, monkeypatch):
        """Chat that triggers tool calls persists the full sequence."""
        session = await _create_session(db)
        await db.commit()

        from app.agent import llm_client, runner

        call_count = 0

        async def mock_call_llm(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return llm_client.LLMResponse(
                    content=None,
                    tool_calls=[{
                        "id": "call_001",
                        "type": "function",
                        "function": {
                            "name": "get_session_documents",
                            "arguments": "{}",
                        },
                    }],
                    input_tokens=150,
                    output_tokens=30,
                    cost_usd=0.002,
                    model="test-model",
                    duration_sec=0.3,
                )
            else:
                return llm_client.LLMResponse(
                    content="You have 3 documents uploaded.",
                    tool_calls=None,
                    input_tokens=200,
                    output_tokens=25,
                    cost_usd=0.002,
                    model="test-model",
                    duration_sec=0.4,
                )

        monkeypatch.setattr(runner, "call_llm", mock_call_llm)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                f"/api/v1/sessions/{session.id}/chat",
                json={"message": "What documents do I have?"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "completed"
        assert "3 documents" in data["response"]


# ──────────────────── Heavy Request Detection Tests ────────────────────


class TestHeavyRequestDetection:
    def test_extraction_requests_detected(self):
        from app.api.chat import _is_heavy_request

        assert _is_heavy_request("Extract all fields from these datasheets") is True
        assert _is_heavy_request("Please extract the data") is True
        assert _is_heavy_request("Re-extract P-718") is True
        assert _is_heavy_request("Process all documents") is True
        assert _is_heavy_request("process these PDFs") is True

    def test_simple_queries_not_detected(self):
        from app.api.chat import _is_heavy_request

        assert _is_heavy_request("What is the flow rate for P-718?") is False
        assert _is_heavy_request("Tell me about the impeller material") is False
        assert _is_heavy_request("How many documents are there?") is False
        assert _is_heavy_request("Compare P-718 and P-818") is False


# ──────────────────── Worker Module Tests ────────────────────


class TestWorkerModule:
    def test_worker_settings_configured(self):
        """WorkerSettings has required Arq configuration."""
        from app.worker import WorkerSettings

        assert len(WorkerSettings.functions) == 1
        assert WorkerSettings.max_jobs == 4
        assert WorkerSettings.job_timeout == 600
        assert WorkerSettings.max_tries == 3

    @pytest.mark.asyncio
    async def test_run_agent_job_with_mock(self, db: AsyncSession, monkeypatch):
        """run_agent_job executes the agent and returns a response."""
        session = await _create_session(db)
        await db.commit()

        from app.agent import llm_client, runner

        async def mock_call_llm(**kwargs):
            return llm_client.LLMResponse(
                content="Extraction complete.",
                tool_calls=None,
                input_tokens=200,
                output_tokens=40,
                cost_usd=0.005,
                model="test-model",
                duration_sec=1.0,
            )

        monkeypatch.setattr(runner, "call_llm", mock_call_llm)

        from app.worker import run_agent_job
        import redis.asyncio as aioredis

        # Create a mock Redis that captures published events
        published_events = []

        class MockRedis:
            async def publish(self, channel, data):
                published_events.append({"channel": channel, "data": data})

        ctx = {"redis": MockRedis()}
        result = await run_agent_job(ctx, str(session.id), "Extract everything")

        assert result == "Extraction complete."
        assert len(published_events) >= 2  # at least thinking + response events

        # Verify events include agent_thinking and agent_response
        event_types = [json.loads(e["data"])["type"] for e in published_events]
        assert "agent_thinking" in event_types
        assert "agent_response" in event_types


# ──────────────────── Chat Schemas Tests ────────────────────


class TestChatSchemas:
    def test_chat_request_validation(self):
        from app.schemas.chat import ChatRequest

        req = ChatRequest(message="Hello agent")
        assert req.message == "Hello agent"

    def test_chat_response_completed(self):
        from app.schemas.chat import ChatResponse

        resp = ChatResponse(response="I found 3 documents.", status="completed")
        assert resp.response == "I found 3 documents."
        assert resp.job_id is None

    def test_chat_response_queued(self):
        from app.schemas.chat import ChatResponse

        resp = ChatResponse(job_id="abc123", status="queued")
        assert resp.job_id == "abc123"
        assert resp.response is None

    def test_job_status_response(self):
        from app.schemas.chat import JobStatusResponse

        resp = JobStatusResponse(job_id="abc123", status="completed", response="Done.")
        assert resp.status == "completed"
        assert resp.error is None
