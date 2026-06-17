"""Phase 3 smoke tests: agent engine core components."""

import json
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.context_manager import (
    build_context,
    context_token_count,
    estimate_tokens,
    should_compact,
)
from app.agent.cost_tracker import CostTracker
from app.agent.runner import AgentRunner, _next_seq, _save_message
from app.agent.tool_registry import ToolRegistry
from app.models.message import Message, MessageRole
from app.models.session import Session, SessionStatus


# ──────────────────── Helpers ────────────────────


async def _create_session(db: AsyncSession) -> Session:
    session = Session(status=SessionStatus.active)
    db.add(session)
    await db.flush()
    return session


# ──────────────────── Tool Registry Tests ────────────────────


class TestToolRegistry:
    def test_register_and_list(self):
        registry = ToolRegistry()

        async def echo(text: str) -> str:
            return text

        registry.register(
            name="echo",
            description="Echoes back the input text",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string", "description": "Text to echo"}},
                "required": ["text"],
            },
            fn=echo,
        )

        assert len(registry) == 1
        assert "echo" in registry.tool_names

        tools = registry.get_tools_for_llm()
        assert len(tools) == 1
        assert tools[0]["type"] == "function"
        assert tools[0]["function"]["name"] == "echo"
        assert tools[0]["function"]["parameters"]["required"] == ["text"]

    @pytest.mark.asyncio
    async def test_execute_tool_success(self):
        registry = ToolRegistry()

        async def echo(text: str) -> dict:
            return {"echoed": text}

        registry.register("echo", "Echo", {"type": "object", "properties": {}}, echo)

        result = await registry.execute_tool("echo", '{"text": "hello"}')
        parsed = json.loads(result)
        assert parsed["echoed"] == "hello"

    @pytest.mark.asyncio
    async def test_execute_unknown_tool(self):
        registry = ToolRegistry()
        result = await registry.execute_tool("nonexistent", "{}")
        parsed = json.loads(result)
        assert "error" in parsed
        assert "Unknown tool" in parsed["error"]

    @pytest.mark.asyncio
    async def test_execute_tool_error_wrapping(self):
        registry = ToolRegistry()

        async def fail(**kwargs):
            raise ValueError("intentional failure")

        registry.register("fail", "Always fails", {"type": "object", "properties": {}}, fail)

        result = await registry.execute_tool("fail", "{}")
        parsed = json.loads(result)
        assert "error" in parsed
        assert "intentional failure" in parsed["error"]

    @pytest.mark.asyncio
    async def test_execute_tool_timeout(self):
        import asyncio

        registry = ToolRegistry(timeout=1)

        async def slow(**kwargs):
            await asyncio.sleep(10)
            return "done"

        registry.register("slow", "Slow tool", {"type": "object", "properties": {}}, slow)

        result = await registry.execute_tool("slow", "{}")
        parsed = json.loads(result)
        assert "error" in parsed
        assert "timed out" in parsed["error"]

    @pytest.mark.asyncio
    async def test_execute_tool_invalid_json_args(self):
        registry = ToolRegistry()

        async def echo(text: str = "") -> str:
            return text

        registry.register("echo", "Echo", {"type": "object", "properties": {}}, echo)

        result = await registry.execute_tool("echo", "not valid json {{{")
        parsed = json.loads(result)
        assert "error" in parsed
        assert "Invalid JSON" in parsed["error"]


# ──────────────────── Context Manager Tests ────────────────────


class TestContextManager:
    def test_estimate_tokens(self):
        assert estimate_tokens("") == 0
        assert estimate_tokens(None) == 0
        # "hello world" = 11 chars ≈ 2 tokens
        assert estimate_tokens("hello world") == 2

    def test_context_token_count(self):
        messages = [
            {"role": "system", "content": "You are a test agent."},
            {"role": "user", "content": "Hello!"},
        ]
        count = context_token_count(messages)
        assert count > 0

    def test_should_compact(self):
        # Small context — no compaction needed
        small = [{"role": "user", "content": "hi"}]
        assert should_compact(small, max_tokens=1000) is False

        # Large context — needs compaction
        big = [{"role": "user", "content": "x" * 40000}]
        assert should_compact(big, max_tokens=1000) is True

    @pytest.mark.asyncio
    async def test_build_context_basic(self, db: AsyncSession):
        session = await _create_session(db)

        # Add some messages
        for i, (role, content) in enumerate([
            (MessageRole.user, "Hello agent"),
            (MessageRole.assistant, "Hello! How can I help?"),
            (MessageRole.user, "What documents do I have?"),
        ]):
            msg = Message(
                session_id=session.id,
                seq_number=i,
                role=role,
                content=content,
            )
            db.add(msg)
        await db.flush()

        context = await build_context(session.id, "You are a test agent.", db)

        # Should have: system prompt + 3 messages
        assert len(context) == 4
        assert context[0]["role"] == "system"
        assert "test agent" in context[0]["content"]
        assert context[1]["role"] == "user"
        assert context[1]["content"] == "Hello agent"

    @pytest.mark.asyncio
    async def test_build_context_with_summary(self, db: AsyncSession):
        session = await _create_session(db)
        session.compact_summary = "User uploaded 2 documents. P718 extracted."
        session.head_ptr = 5

        msg = Message(
            session_id=session.id,
            seq_number=5,
            role=MessageRole.user,
            content="What about P818?",
        )
        db.add(msg)
        await db.flush()

        context = await build_context(session.id, "System prompt.", db)

        # Should have: system prompt + summary + 1 active message
        assert len(context) == 3
        assert context[0]["role"] == "system"
        assert context[1]["role"] == "system"
        assert "Summary of earlier conversation" in context[1]["content"]
        assert "P718 extracted" in context[1]["content"]
        assert context[2]["role"] == "user"


# ──────────────────── Cost Tracker Tests ────────────────────


class TestCostTracker:
    def test_record_and_check(self):
        tracker = CostTracker(
            session_id=uuid.uuid4(),
            max_iterations=5,
            max_tokens=10000,
        )
        tracker.record_llm_call(input_tokens=1000, output_tokens=500, cost=0.01)
        assert tracker.iterations == 1
        assert tracker.total_tokens == 1500
        assert tracker.cost_usd == 0.01
        assert tracker.is_run_budget_exceeded() is False

    def test_iteration_limit(self):
        tracker = CostTracker(
            session_id=uuid.uuid4(),
            max_iterations=2,
            max_tokens=100000,
        )
        tracker.record_llm_call(100, 50, 0.001)
        tracker.record_llm_call(100, 50, 0.001)
        assert tracker.is_run_budget_exceeded() is True

    def test_token_limit(self):
        tracker = CostTracker(
            session_id=uuid.uuid4(),
            max_iterations=100,
            max_tokens=500,
        )
        tracker.record_llm_call(300, 300, 0.01)
        assert tracker.is_run_budget_exceeded() is True

    def test_warning_threshold(self):
        tracker = CostTracker(
            session_id=uuid.uuid4(),
            max_iterations=10,
            max_tokens=10000,
        )
        # Under threshold — no warning
        tracker.record_llm_call(100, 50, 0.001)
        assert tracker.get_run_warning() is None

        # Push past 80% of iterations
        for _ in range(7):
            tracker.record_llm_call(100, 50, 0.001)
        warning = tracker.get_run_warning()
        assert warning is not None
        assert "iteration limit" in warning


# ──────────────────── Message Persistence Tests ────────────────────


class TestMessagePersistence:
    @pytest.mark.asyncio
    async def test_save_and_sequence(self, db: AsyncSession):
        session = await _create_session(db)

        msg1 = await _save_message(session.id, MessageRole.user, db, content="First")
        msg2 = await _save_message(session.id, MessageRole.assistant, db, content="Second")
        msg3 = await _save_message(session.id, MessageRole.user, db, content="Third")

        assert msg1.seq_number == 0
        assert msg2.seq_number == 1
        assert msg3.seq_number == 2

        # Verify persistence
        stmt = (
            select(Message)
            .where(Message.session_id == session.id)
            .order_by(Message.seq_number)
        )
        result = await db.execute(stmt)
        msgs = result.scalars().all()
        assert len(msgs) == 3
        assert msgs[0].content == "First"
        assert msgs[2].content == "Third"

    @pytest.mark.asyncio
    async def test_save_tool_call_message(self, db: AsyncSession):
        session = await _create_session(db)

        tool_calls = [
            {
                "id": "call_123",
                "type": "function",
                "function": {"name": "echo", "arguments": '{"text": "hello"}'},
            }
        ]
        msg = await _save_message(
            session.id, MessageRole.assistant, db,
            content=None, tool_calls=tool_calls,
        )
        assert msg.tool_calls == tool_calls

        # Tool result message
        result_msg = await _save_message(
            session.id, MessageRole.tool, db,
            content='{"echoed": "hello"}',
            tool_call_id="call_123",
            tool_result={"echoed": "hello"},
        )
        assert result_msg.tool_call_id == "call_123"
        assert result_msg.tool_result["echoed"] == "hello"


# ──────────────────── Agent Runner Smoke Test (with mock LLM) ────────────────────


class TestAgentRunnerWithMock:
    """Tests the agent runner by monkeypatching the LLM call."""

    @pytest.mark.asyncio
    async def test_simple_text_response(self, db: AsyncSession, monkeypatch):
        """Agent receives a message, LLM returns plain text — no tool calls."""
        session = await _create_session(db)

        # Mock LLM to return a simple text response
        from app.agent import llm_client, runner

        async def mock_call_llm(**kwargs):
            return llm_client.LLMResponse(
                content="Hello! I'm a test agent.",
                tool_calls=None,
                input_tokens=100,
                output_tokens=20,
                cost_usd=0.001,
                model="test-model",
                duration_sec=0.5,
            )

        monkeypatch.setattr(runner, "call_llm", mock_call_llm)

        registry = ToolRegistry()
        agent = AgentRunner(
            session_id=session.id,
            system_prompt="You are a test agent.",
            tools=registry,
            db=db,
        )

        result = await agent.run("Hi there!")

        assert result == "Hello! I'm a test agent."

        # Verify messages in DB: user + assistant
        stmt = (
            select(Message)
            .where(Message.session_id == session.id)
            .order_by(Message.seq_number)
        )
        res = await db.execute(stmt)
        msgs = res.scalars().all()
        assert len(msgs) == 2
        assert msgs[0].role == MessageRole.user
        assert msgs[0].content == "Hi there!"
        assert msgs[1].role == MessageRole.assistant
        assert msgs[1].content == "Hello! I'm a test agent."

    @pytest.mark.asyncio
    async def test_tool_call_then_response(self, db: AsyncSession, monkeypatch):
        """Agent calls a tool, gets the result, then responds with text."""
        session = await _create_session(db)

        from app.agent import llm_client, runner

        call_count = 0

        async def mock_call_llm(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call: LLM wants to use the echo tool
                return llm_client.LLMResponse(
                    content=None,
                    tool_calls=[
                        {
                            "id": "call_001",
                            "type": "function",
                            "function": {
                                "name": "echo",
                                "arguments": '{"text": "test message"}',
                            },
                        }
                    ],
                    input_tokens=150,
                    output_tokens=30,
                    cost_usd=0.002,
                    model="test-model",
                    duration_sec=0.3,
                )
            else:
                # Second call: LLM sees tool result, responds with text
                return llm_client.LLMResponse(
                    content="The echo tool returned: test message",
                    tool_calls=None,
                    input_tokens=200,
                    output_tokens=25,
                    cost_usd=0.002,
                    model="test-model",
                    duration_sec=0.4,
                )

        monkeypatch.setattr(runner, "call_llm", mock_call_llm)

        # Register echo tool
        registry = ToolRegistry()

        async def echo(text: str) -> dict:
            return {"echoed": text}

        registry.register(
            "echo",
            "Echoes text",
            {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            echo,
        )

        agent = AgentRunner(
            session_id=session.id,
            system_prompt="You are a test agent with an echo tool.",
            tools=registry,
            db=db,
        )

        result = await agent.run("Say hello using the echo tool")

        assert "test message" in result
        assert call_count == 2

        # Verify full message sequence in DB:
        # 0: user message
        # 1: assistant with tool_calls
        # 2: tool result
        # 3: assistant final response
        stmt = (
            select(Message)
            .where(Message.session_id == session.id)
            .order_by(Message.seq_number)
        )
        res = await db.execute(stmt)
        msgs = res.scalars().all()
        assert len(msgs) == 4

        assert msgs[0].role == MessageRole.user
        assert msgs[1].role == MessageRole.assistant
        assert msgs[1].tool_calls is not None
        assert msgs[1].tool_calls[0]["function"]["name"] == "echo"
        assert msgs[2].role == MessageRole.tool
        assert msgs[2].tool_call_id == "call_001"
        assert msgs[3].role == MessageRole.assistant
        assert msgs[3].tool_calls is None

    @pytest.mark.asyncio
    async def test_budget_exceeded_stops_loop(self, db: AsyncSession, monkeypatch):
        """Agent hits iteration budget and stops gracefully."""
        session = await _create_session(db)

        from app.agent import llm_client, runner

        async def mock_call_llm(**kwargs):
            # Always return tool calls — should be stopped by budget
            return llm_client.LLMResponse(
                content=None,
                tool_calls=[
                    {
                        "id": "call_loop",
                        "type": "function",
                        "function": {"name": "echo", "arguments": '{"text": "loop"}'},
                    }
                ],
                input_tokens=100,
                output_tokens=20,
                cost_usd=0.001,
                model="test-model",
                duration_sec=0.1,
            )

        monkeypatch.setattr(runner, "call_llm", mock_call_llm)

        registry = ToolRegistry()

        async def echo(text: str) -> dict:
            return {"echoed": text}

        registry.register(
            "echo", "Echo", {"type": "object", "properties": {"text": {"type": "string"}}}, echo
        )

        agent = AgentRunner(
            session_id=session.id,
            system_prompt="Test agent.",
            tools=registry,
            db=db,
            max_iterations=3,
        )

        result = await agent.run("Keep going forever")

        assert "Budget exceeded" in result
        assert agent.cost_tracker.iterations == 3

    @pytest.mark.asyncio
    async def test_multiple_tool_calls_in_one_response(self, db: AsyncSession, monkeypatch):
        """Agent makes multiple tool calls in a single LLM response."""
        session = await _create_session(db)

        from app.agent import llm_client, runner

        call_count = 0

        async def mock_call_llm(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return llm_client.LLMResponse(
                    content=None,
                    tool_calls=[
                        {
                            "id": "call_a",
                            "type": "function",
                            "function": {"name": "echo", "arguments": '{"text": "first"}'},
                        },
                        {
                            "id": "call_b",
                            "type": "function",
                            "function": {"name": "echo", "arguments": '{"text": "second"}'},
                        },
                    ],
                    input_tokens=150,
                    output_tokens=40,
                    cost_usd=0.002,
                    model="test-model",
                    duration_sec=0.3,
                )
            else:
                return llm_client.LLMResponse(
                    content="Both tools executed successfully.",
                    tool_calls=None,
                    input_tokens=200,
                    output_tokens=20,
                    cost_usd=0.002,
                    model="test-model",
                    duration_sec=0.3,
                )

        monkeypatch.setattr(runner, "call_llm", mock_call_llm)

        registry = ToolRegistry()

        async def echo(text: str) -> dict:
            return {"echoed": text}

        registry.register(
            "echo", "Echo",
            {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            echo,
        )

        agent = AgentRunner(
            session_id=session.id,
            system_prompt="Test agent.",
            tools=registry,
            db=db,
        )

        result = await agent.run("Call echo twice")
        assert "Both tools executed" in result

        # DB should have: user + assistant(tool_calls) + tool_a + tool_b + assistant(final)
        stmt = (
            select(Message)
            .where(Message.session_id == session.id)
            .order_by(Message.seq_number)
        )
        res = await db.execute(stmt)
        msgs = res.scalars().all()
        assert len(msgs) == 5
        tool_msgs = [m for m in msgs if m.role == MessageRole.tool]
        assert len(tool_msgs) == 2
