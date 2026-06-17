"""Phase 10 tests: HITL correction flow, re-extraction with correction awareness,
and global correction pattern detection.

Tests the correction-through-conversation flow, correction-aware re-extraction,
and the pattern_detector service that promotes repeated corrections into global patterns.
"""

import json
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.context_manager import build_context, load_correction_patterns
from app.agent.llm_client import LLMResponse
from app.agent.runner import AgentRunner
from app.agent.tool_registry import ToolRegistry
from app.models.correction_pattern import CorrectionPattern
from app.models.document import Document, DocumentStatus
from app.models.document_page import DocumentPage
from app.models.entity_relationship import EntityRelationship, RelationshipType
from app.models.equipment_entity import EquipmentEntity
from app.models.extracted_field import ExtractedField, FieldDataType, FieldStatus
from app.models.field_correction import FieldCorrection
from app.models.message import Message, MessageRole
from app.models.session import Session, SessionStatus
from app.prompts.extraction_prompt import build_extraction_prompt
from app.prompts.orchestrator_prompt import build_orchestrator_prompt
from app.services.pattern_detector import (
    ACTIVATION_THRESHOLD,
    DetectedPattern,
    detect_patterns,
    promote_patterns,
)
from app.tools import create_orchestrator_tools
from app.tools.document_tools import register_document_tools
from app.tools.entity_tools import register_entity_tools
from app.tools.extraction_tools import register_extraction_tools
from app.tools.query_tools import register_query_tools
from app.tools.subagent_tools import _load_corrections_for_document


# ──────────────────── Helpers ────────────────────


async def _create_session(db: AsyncSession) -> Session:
    session = Session(status=SessionStatus.active)
    db.add(session)
    await db.flush()
    return session


async def _create_document(
    db: AsyncSession,
    session_id: uuid.UUID,
    filename: str = "pds-P718.pdf",
    pump_tag: str = "P-718",
    status: DocumentStatus = DocumentStatus.extracted,
) -> Document:
    doc = Document(
        session_id=session_id,
        filename=filename,
        file_path=f"/tmp/uploads/{filename}",
        pump_tag=pump_tag,
        format_type="english_tabular",
        status=status,
        num_pages=3,
    )
    db.add(doc)
    await db.flush()
    return doc


async def _create_field(
    db: AsyncSession,
    document_id: uuid.UUID,
    field_name: str = "impeller_material",
    raw_value: str = "CS",
    section: str = "construction_materials",
    confidence: float = 0.88,
) -> ExtractedField:
    field = ExtractedField(
        document_id=document_id,
        field_name=field_name,
        display_name=field_name.replace("_", " ").title(),
        raw_value=raw_value,
        data_type=FieldDataType.text,
        section=section,
        confidence=confidence,
        status=FieldStatus.extracted,
        citation_page=1,
        citation_text=f"{field_name}: {raw_value}",
    )
    db.add(field)
    await db.flush()
    return field


async def _create_correction(
    db: AsyncSession,
    field_id: uuid.UUID,
    original: str,
    corrected: str,
    reason: str | None = None,
) -> FieldCorrection:
    correction = FieldCorrection(
        field_id=field_id,
        original_value=original,
        corrected_value=corrected,
        reason=reason,
        corrected_by="user",
    )
    db.add(correction)
    await db.flush()
    return correction


def _make_llm_response(
    content: str | None = None,
    tool_calls: list[dict] | None = None,
    input_tokens: int = 200,
    output_tokens: int = 50,
) -> LLMResponse:
    return LLMResponse(
        content=content,
        tool_calls=tool_calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=0.001,
        model="test-model",
        duration_sec=0.3,
    )


def _tool_call(name: str, arguments: dict, call_id: str | None = None) -> dict:
    return {
        "id": call_id or f"call_{name}_{uuid.uuid4().hex[:8]}",
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments),
        },
    }


# ──────────────────── 10.1: Correction Through Chat ────────────────────


class TestCorrectionThroughChat:
    """Full HITL correction flow through the conversational agent."""

    @pytest.mark.asyncio
    async def test_correction_search_update_confirm(self, db: AsyncSession, monkeypatch):
        """Agent searches for field, updates it, confirms, and checks siblings."""
        session = await _create_session(db)
        doc = await _create_document(db, session.id, "pds-P718.pdf", "P-718")
        field = await _create_field(db, doc.id, "impeller_material", "CS")
        field_id = field.id

        # Create sibling entity setup
        doc2 = await _create_document(db, session.id, "pds-P818.pdf", "P-818")
        entity1 = EquipmentEntity(
            session_id=session.id, tag="P-718",
            entity_type="centrifugal_pump", name="Diesel Product Pump",
        )
        entity2 = EquipmentEntity(
            session_id=session.id, tag="P-818",
            entity_type="centrifugal_pump", name="Diesel Transfer Pump",
        )
        db.add_all([entity1, entity2])
        await db.flush()

        rel = EntityRelationship(
            entity_a_id=entity1.id, entity_b_id=entity2.id,
            relationship_type=RelationshipType.sibling,
        )
        db.add(rel)
        field.entity_id = entity1.id
        await db.flush()
        await db.commit()

        from app.agent import runner

        call_count = 0

        async def mock_call_llm(**kwargs):
            nonlocal call_count
            call_count += 1

            # Step 1: Search for the field
            if call_count == 1:
                return _make_llm_response(
                    tool_calls=[_tool_call("search_fields", {"field_name": "impeller_material"})]
                )

            # Step 2: Update the field with corrected value
            if call_count == 2:
                return _make_llm_response(
                    tool_calls=[_tool_call("update_extracted_field", {
                        "field_id": str(field_id),
                        "corrected_value": "SS 316",
                        "reason": "Spec updated last month",
                    })]
                )

            # Step 3: Check entity detail for sibling relationships
            if call_count == 3:
                return _make_llm_response(
                    tool_calls=[_tool_call("get_entity_detail", {"entity_id": str(entity1.id)})]
                )

            # Step 4: Final response — confirm and offer to check sibling
            return _make_llm_response(
                content=(
                    "Corrected impeller_material for P-718: CS → SS 316. "
                    "P-818 is a sibling pump — would you like me to re-check it?"
                )
            )

        monkeypatch.setattr(runner, "call_llm", mock_call_llm)

        tools = create_orchestrator_tools(session.id, db)
        agent_runner = AgentRunner(
            session_id=session.id, system_prompt=build_orchestrator_prompt(),
            tools=tools, db=db,
        )
        result = await agent_runner.run(
            "The impeller material for P-718 is wrong. Should be SS 316."
        )

        # Verify response mentions the correction and sibling
        assert "SS 316" in result
        assert "P-818" in result
        assert "sibling" in result.lower()

        # Verify field updated in DB
        await db.refresh(field)
        assert field.raw_value == "SS 316"
        assert field.status == FieldStatus.corrected

        # Verify FieldCorrection audit record
        corr_stmt = select(FieldCorrection).where(FieldCorrection.field_id == field_id)
        correction = (await db.execute(corr_stmt)).scalar_one()
        assert correction.original_value == "CS"
        assert correction.corrected_value == "SS 316"
        assert correction.reason == "Spec updated last month"
        assert correction.corrected_by == "user"

    @pytest.mark.asyncio
    async def test_correction_preserves_original_extraction(self, db: AsyncSession):
        """Original extraction values are never destroyed — corrections are additive."""
        session = await _create_session(db)
        doc = await _create_document(db, session.id)
        field = await _create_field(db, doc.id, "flow_nominal", "335")

        # Apply multiple corrections
        corr1 = await _create_correction(db, field.id, "335", "3.35", "Decimal misread")
        field.raw_value = "3.35"
        field.status = FieldStatus.corrected
        await db.flush()

        corr2 = await _create_correction(db, field.id, "3.35", "3.50", "Verified with vendor")
        field.raw_value = "3.50"
        await db.flush()

        # Both correction records exist
        stmt = (
            select(FieldCorrection)
            .where(FieldCorrection.field_id == field.id)
            .order_by(FieldCorrection.created_at)
        )
        result = await db.execute(stmt)
        corrections = result.scalars().all()

        assert len(corrections) == 2
        assert corrections[0].original_value == "335"
        assert corrections[0].corrected_value == "3.35"
        assert corrections[1].original_value == "3.35"
        assert corrections[1].corrected_value == "3.50"

        # Current field value is the latest correction
        await db.refresh(field)
        assert field.raw_value == "3.50"
        assert field.status == FieldStatus.corrected

    @pytest.mark.asyncio
    async def test_correction_via_api_chat_endpoint(self, db: AsyncSession, monkeypatch):
        """Correction flows through the /chat API endpoint correctly."""
        session = await _create_session(db)
        doc = await _create_document(db, session.id)
        field = await _create_field(db, doc.id, "impeller_material", "CS")
        field_id = field.id
        await db.commit()

        from app.agent import runner

        call_count = 0

        async def mock_call_llm(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_llm_response(
                    tool_calls=[_tool_call("search_fields", {"field_name": "impeller_material"})]
                )
            if call_count == 2:
                return _make_llm_response(
                    tool_calls=[_tool_call("update_extracted_field", {
                        "field_id": str(field_id),
                        "corrected_value": "SS 316",
                        "reason": "User correction",
                    })]
                )
            return _make_llm_response(
                content="Done. Impeller material corrected to SS 316."
            )

        monkeypatch.setattr(runner, "call_llm", mock_call_llm)

        from httpx import ASGITransport, AsyncClient
        from app.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                f"/api/v1/sessions/{session.id}/chat",
                json={"message": "Fix the impeller material for P-718 to SS 316"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "completed"
        assert "SS 316" in data["response"]

        # Verify DB state via API
        from app.database import async_session_factory
        async with async_session_factory() as fresh_db:
            updated_field = await fresh_db.get(ExtractedField, field_id)
            assert updated_field.raw_value == "SS 316"
            assert updated_field.status == FieldStatus.corrected


# ──────────────────── 10.2: Re-Extraction with Correction Awareness ────────────────────


class TestReExtractionWithCorrectionAwareness:
    """Corrections from earlier documents inform subsequent re-extractions."""

    @pytest.mark.asyncio
    async def test_correction_history_injected_into_extraction_prompt(self, db: AsyncSession):
        """Extraction prompt includes correction history from sibling documents."""
        session = await _create_session(db)
        doc1 = await _create_document(db, session.id, "pds-P718.pdf", "P-718")
        doc2 = await _create_document(db, session.id, "pds-P818.pdf", "P-818", status=DocumentStatus.uploaded)

        field = await _create_field(db, doc1.id, "impeller_material", "SS 316")
        field.status = FieldStatus.corrected
        await db.flush()

        await _create_correction(db, field.id, "CS", "SS 316", "Spec updated")

        # Load corrections for doc2 — should include doc1's corrections
        corrections = await _load_corrections_for_document(doc2.id, session.id, db)

        # Build extraction prompt with corrections
        prompt = build_extraction_prompt(
            document_info={"filename": "pds-P818.pdf", "num_pages": 3, "format_type": "english_tabular"},
            correction_history=corrections,
        )

        assert "Correction History" in prompt
        assert "impeller_material" in prompt
        assert "CS" in prompt
        assert "SS 316" in prompt
        assert "Spec updated" in prompt

    @pytest.mark.asyncio
    async def test_reextraction_spawns_subagent_with_corrections(self, db: AsyncSession, monkeypatch):
        """When re-extracting, the orchestrator spawns a sub-agent that has correction context."""
        session = await _create_session(db)
        doc = await _create_document(db, session.id, "pds-P718.pdf", "P-718", status=DocumentStatus.extracted)
        field = await _create_field(db, doc.id, "impeller_material", "SS 316")
        field.status = FieldStatus.corrected
        await db.flush()
        await _create_correction(db, field.id, "CS", "SS 316", "Spec updated")

        # Add page data for the sub-agent
        page = DocumentPage(
            document_id=doc.id, page_number=1,
            raw_text="Impeller Material: CS", layout_text="Impeller Material: CS",
            image_path="test/page_1.png", width=595.0, height=842.0,
        )
        db.add(page)
        await db.flush()
        await db.commit()

        from app.agent import runner

        call_count = 0
        captured_prompts = []

        async def mock_call_llm(**kwargs):
            nonlocal call_count
            call_count += 1
            messages = kwargs.get("messages", [])

            # Capture system prompts to verify correction injection
            for m in messages:
                if m.get("role") == "system" and "Correction History" in (m.get("content") or ""):
                    captured_prompts.append(m["content"])

            # Orchestrator: list docs, then spawn extraction
            if call_count == 1:
                return _make_llm_response(
                    tool_calls=[_tool_call("get_session_documents", {})]
                )

            if call_count == 2:
                return _make_llm_response(
                    tool_calls=[_tool_call("spawn_extraction_agent", {"document_id": str(doc.id)})]
                )

            # Sub-agent responses (nested LLM calls from spawn_extraction_agent)
            # The sub-agent will make its own LLM calls
            return _make_llm_response(
                content="Re-extraction complete for P-718. Applied correction history."
            )

        monkeypatch.setattr(runner, "call_llm", mock_call_llm)

        # Build tools manually with mock spawn that verifies corrections are loaded
        tools = ToolRegistry()
        register_document_tools(tools, session.id, db)
        register_extraction_tools(tools, db)
        register_entity_tools(tools, session.id, db)
        register_query_tools(tools, session.id, db)

        correction_contexts_seen = []

        async def mock_spawn_extraction(document_id: str) -> dict:
            # Verify that corrections are loaded
            corrections = await _load_corrections_for_document(
                uuid.UUID(document_id), session.id, db
            )
            correction_contexts_seen.append(corrections)

            prompt = build_extraction_prompt(
                document_info={"filename": "pds-P718.pdf", "num_pages": 3, "format_type": "english_tabular"},
                correction_history=corrections,
            )
            # Verify the prompt includes correction history
            assert "Correction History" in prompt
            assert "impeller_material" in prompt

            return {
                "document_id": document_id,
                "filename": "pds-P718.pdf",
                "summary": "Re-extracted with correction awareness.",
                "sub_session_id": str(uuid.uuid4()),
            }

        tools.register(
            name="spawn_extraction_agent",
            description="Spawn extraction sub-agent.",
            parameters={"type": "object", "properties": {"document_id": {"type": "string"}}, "required": ["document_id"]},
            fn=mock_spawn_extraction,
        )
        tools.register(
            name="spawn_validation_agent",
            description="Spawn validation sub-agent.",
            parameters={"type": "object", "properties": {}, "required": []},
            fn=lambda: {"summary": "OK", "documents_validated": 1, "sub_session_id": str(uuid.uuid4())},
        )

        agent_runner = AgentRunner(
            session_id=session.id, system_prompt=build_orchestrator_prompt(),
            tools=tools, db=db,
        )
        await agent_runner.run("Re-extract P-718")

        # Verify corrections were loaded and passed to the sub-agent
        assert len(correction_contexts_seen) == 1
        corrections = correction_contexts_seen[0]
        assert len(corrections) >= 1
        assert corrections[0]["field_name"] == "impeller_material"
        assert corrections[0]["original"] == "CS"
        assert corrections[0]["corrected"] == "SS 316"

    @pytest.mark.asyncio
    async def test_corrections_from_multiple_docs_aggregated(self, db: AsyncSession):
        """Corrections from all documents in a session are available to any sub-agent."""
        session = await _create_session(db)
        doc1 = await _create_document(db, session.id, "pds-P718.pdf", "P-718")
        doc2 = await _create_document(db, session.id, "pds-P818.pdf", "P-818")
        doc3 = await _create_document(db, session.id, "pds-P300228.pdf", "P-300228", status=DocumentStatus.uploaded)

        # Corrections on doc1
        f1 = await _create_field(db, doc1.id, "impeller_material", "SS 316")
        f1.status = FieldStatus.corrected
        await db.flush()
        await _create_correction(db, f1.id, "CS", "SS 316", "Spec updated")

        # Corrections on doc2
        f2 = await _create_field(db, doc2.id, "flow_nominal", "3.50")
        f2.status = FieldStatus.corrected
        await db.flush()
        await _create_correction(db, f2.id, "350", "3.50", "Decimal misread")

        # Load corrections for doc3 — should include corrections from both doc1 and doc2
        corrections = await _load_corrections_for_document(doc3.id, session.id, db)
        assert len(corrections) == 2

        field_names = {c["field_name"] for c in corrections}
        assert "impeller_material" in field_names
        assert "flow_nominal" in field_names


# ──────────────────── 10.3: Global Correction Pattern Detection ────────────────────


class TestPatternDetection:
    """Test the pattern_detector service that promotes repeated corrections."""

    @pytest.mark.asyncio
    async def test_detect_patterns_below_threshold(self, db: AsyncSession):
        """Patterns below the threshold are not detected."""
        # Use a unique field name to avoid cross-test contamination
        unique_field = "gasket_material_below_test"

        session1 = await _create_session(db)
        session2 = await _create_session(db)

        doc1 = await _create_document(db, session1.id, "pds-bt1.pdf", "P-BT1")
        doc2 = await _create_document(db, session2.id, "pds-bt2.pdf", "P-BT2")

        # Same correction in 2 sessions (below threshold of 3)
        f1 = await _create_field(db, doc1.id, unique_field, "graphite")
        f2 = await _create_field(db, doc2.id, unique_field, "graphite")
        await _create_correction(db, f1.id, "graphite", "spiral_wound")
        await _create_correction(db, f2.id, "graphite", "spiral_wound")

        patterns = await detect_patterns(db)
        matching = [p for p in patterns if p.field_name == unique_field]
        assert len(matching) == 0

    @pytest.mark.asyncio
    async def test_detect_patterns_at_threshold(self, db: AsyncSession):
        """Patterns at or above the threshold are detected."""
        # Use a unique field name
        unique_field = "wear_ring_material_thresh_test"

        sessions = []
        for _ in range(3):
            s = await _create_session(db)
            sessions.append(s)

        for s in sessions:
            doc = await _create_document(db, s.id, "pds-thresh.pdf", "P-THRESH")
            field = await _create_field(db, doc.id, unique_field, "bronze")
            await _create_correction(db, field.id, "bronze", "SS 304", "Material spec")

        patterns = await detect_patterns(db)
        matching = [p for p in patterns if p.field_name == unique_field
                    and p.original_value == "bronze" and p.corrected_value == "SS 304"]
        assert len(matching) == 1
        assert matching[0].session_count == 3
        assert matching[0].total_count == 3

    @pytest.mark.asyncio
    async def test_detect_patterns_multiple_corrections_per_session(self, db: AsyncSession):
        """Multiple corrections of the same type in one session count as one session."""
        sessions = []
        for _ in range(3):
            s = await _create_session(db)
            sessions.append(s)

        # Session 1: two corrections of the same type
        doc1a = await _create_document(db, sessions[0].id, "pds-1a.pdf", "P-1A")
        doc1b = await _create_document(db, sessions[0].id, "pds-1b.pdf", "P-1B")
        f1a = await _create_field(db, doc1a.id, "casing_material", "CI")
        f1b = await _create_field(db, doc1b.id, "casing_material", "CI")
        await _create_correction(db, f1a.id, "CI", "CS", "Wrong material")
        await _create_correction(db, f1b.id, "CI", "CS", "Wrong material")

        # Sessions 2 and 3: one correction each
        for s in sessions[1:]:
            doc = await _create_document(db, s.id, "pds-x.pdf", "P-X")
            f = await _create_field(db, doc.id, "casing_material", "CI")
            await _create_correction(db, f.id, "CI", "CS", "Wrong material")

        patterns = await detect_patterns(db)
        matching = [p for p in patterns if p.field_name == "casing_material"]
        assert len(matching) == 1
        assert matching[0].session_count == 3  # 3 distinct sessions
        assert matching[0].total_count == 4  # 4 total corrections

    @pytest.mark.asyncio
    async def test_detect_different_corrections_separate(self, db: AsyncSession):
        """Different correction values for the same field are tracked separately."""
        sessions = []
        for _ in range(3):
            s = await _create_session(db)
            sessions.append(s)

        # Correction A: impeller_material CS → SS 316 (3 sessions)
        for s in sessions:
            doc = await _create_document(db, s.id, "pds-a.pdf", "P-A")
            f = await _create_field(db, doc.id, "impeller_material", "CS")
            await _create_correction(db, f.id, "CS", "SS 316")

        # Correction B: impeller_material CI → CS (2 sessions, below threshold)
        for s in sessions[:2]:
            doc = await _create_document(db, s.id, "pds-b.pdf", "P-B")
            f = await _create_field(db, doc.id, "impeller_material", "CI")
            await _create_correction(db, f.id, "CI", "CS")

        patterns = await detect_patterns(db)
        # Only CS → SS 316 should meet the threshold
        ss316_patterns = [p for p in patterns if p.corrected_value == "SS 316"]
        cs_patterns = [p for p in patterns if p.original_value == "CI" and p.corrected_value == "CS"]
        assert len(ss316_patterns) >= 1
        assert len(cs_patterns) == 0

    @pytest.mark.asyncio
    async def test_detect_patterns_custom_threshold(self, db: AsyncSession):
        """Custom threshold overrides the default."""
        sessions = []
        for _ in range(2):
            s = await _create_session(db)
            sessions.append(s)

        for s in sessions:
            doc = await _create_document(db, s.id, "pds-t.pdf", "P-T")
            f = await _create_field(db, doc.id, "shaft_material", "SS 304")
            await _create_correction(db, f.id, "SS 304", "AISI 4140")

        # Default threshold=3 would not find it
        patterns_default = await detect_patterns(db)
        shaft_default = [p for p in patterns_default if p.field_name == "shaft_material"
                         and p.original_value == "SS 304"]
        assert len(shaft_default) == 0

        # Custom threshold=2 finds it
        patterns_custom = await detect_patterns(db, threshold=2)
        shaft_custom = [p for p in patterns_custom if p.field_name == "shaft_material"
                        and p.original_value == "SS 304"]
        assert len(shaft_custom) == 1


class TestPatternPromotion:
    """Test promote_patterns: creating/updating CorrectionPattern records."""

    @pytest.mark.asyncio
    async def test_promote_creates_new_pattern(self, db: AsyncSession):
        """promote_patterns creates a new CorrectionPattern for detected patterns."""
        sessions = []
        for _ in range(3):
            s = await _create_session(db)
            sessions.append(s)

        for s in sessions:
            doc = await _create_document(db, s.id, "pds-promo.pdf", "P-PROMO")
            f = await _create_field(db, doc.id, "seal_type", "mechanical")
            await _create_correction(db, f.id, "mechanical", "double_mechanical", "Safety requirement")

        result = await promote_patterns(db)
        assert len(result) >= 1

        # Find our pattern
        seal_patterns = [p for p in result if "seal_type" in p.description]
        assert len(seal_patterns) == 1

        pattern = seal_patterns[0]
        assert pattern.is_active is True
        assert pattern.frequency == 3
        assert "seal_type" in pattern.description
        assert "mechanical" in pattern.guidance_text
        assert "double_mechanical" in pattern.guidance_text

        # Verify it's in the DB
        db_stmt = select(CorrectionPattern).where(
            CorrectionPattern.description == pattern.description
        )
        db_pattern = (await db.execute(db_stmt)).scalar_one()
        assert db_pattern.is_active is True

    @pytest.mark.asyncio
    async def test_promote_updates_existing_pattern(self, db: AsyncSession):
        """If a pattern already exists, promote_patterns updates its frequency."""
        sessions = []
        for _ in range(3):
            s = await _create_session(db)
            sessions.append(s)

        for s in sessions:
            doc = await _create_document(db, s.id, "pds-exist.pdf", "P-EXIST")
            f = await _create_field(db, doc.id, "bearing_type", "ball")
            await _create_correction(db, f.id, "ball", "roller", "Wrong type")

        # Create an existing pattern manually
        existing = CorrectionPattern(
            description="bearing_type: 'ball' → 'roller'",
            guidance_text="Old guidance",
            frequency=1,
            is_active=True,
        )
        db.add(existing)
        await db.flush()
        existing_id = existing.id

        # Promote — should update, not duplicate
        result = await promote_patterns(db)
        bearing_patterns = [p for p in result if "bearing_type" in p.description]
        assert len(bearing_patterns) == 1
        assert bearing_patterns[0].id == existing_id  # Same record
        assert bearing_patterns[0].frequency == 3  # Updated

        # Verify no duplicates in DB
        count_stmt = select(CorrectionPattern).where(
            CorrectionPattern.description.contains("bearing_type")
        )
        all_bearing = (await db.execute(count_stmt)).scalars().all()
        assert len(all_bearing) == 1

    @pytest.mark.asyncio
    async def test_promote_returns_empty_when_no_patterns(self, db: AsyncSession):
        """promote_patterns returns empty list when no patterns meet threshold."""
        # Just one session with one correction — below threshold
        session = await _create_session(db)
        doc = await _create_document(db, session.id)
        field = await _create_field(db, doc.id, "random_field", "old_val")
        await _create_correction(db, field.id, "old_val", "new_val")

        result = await promote_patterns(db)
        # May return patterns from other test data, but our specific one shouldn't be there
        random_patterns = [p for p in result if "random_field" in p.description]
        assert len(random_patterns) == 0


class TestPatternInjection:
    """Test that promoted patterns are injected into agent system prompts."""

    @pytest.mark.asyncio
    async def test_patterns_loaded_from_db(self, db: AsyncSession):
        """Active patterns are loaded and returned by load_correction_patterns."""
        # Clean existing patterns
        existing = (await db.execute(select(CorrectionPattern))).scalars().all()
        for p in existing:
            await db.delete(p)
        await db.flush()

        # Create patterns
        p1 = CorrectionPattern(
            description="French suction pressure",
            guidance_text="PRESSION ASPIRATION means suction pressure",
            frequency=5, is_active=True,
        )
        p2 = CorrectionPattern(
            description="Decimal misread",
            guidance_text="Watch for decimal points in flow values",
            frequency=3, is_active=True,
        )
        p3 = CorrectionPattern(
            description="Inactive",
            guidance_text="This is inactive",
            frequency=2, is_active=False,
        )
        db.add_all([p1, p2, p3])
        await db.flush()

        patterns = await load_correction_patterns(db)
        assert len(patterns) == 2  # Only active ones
        assert "suction pressure" in patterns[0] or "suction pressure" in patterns[1]

    @pytest.mark.asyncio
    async def test_patterns_injected_into_context(self, db: AsyncSession):
        """Active patterns appear in the system prompt of the context window."""
        # Clean existing patterns
        existing = (await db.execute(select(CorrectionPattern))).scalars().all()
        for p in existing:
            await db.delete(p)
        await db.flush()

        # Create a pattern
        pattern = CorrectionPattern(
            description="French form correction",
            guidance_text="In French datasheets, PRESSION ASPIRATION = suction pressure",
            frequency=5, is_active=True,
        )
        db.add(pattern)
        await db.flush()

        session = await _create_session(db)
        # Add a user message so context has something
        msg = Message(
            session_id=session.id, seq_number=0,
            role=MessageRole.user, content="Hello",
        )
        db.add(msg)
        await db.flush()

        context = await build_context(session.id, build_orchestrator_prompt(), db)

        # System prompt should include the pattern
        system_msg = context[0]
        assert system_msg["role"] == "system"
        assert "PRESSION ASPIRATION" in system_msg["content"]
        assert "suction pressure" in system_msg["content"]

    @pytest.mark.asyncio
    async def test_full_lifecycle_correction_to_pattern_to_prompt(self, db: AsyncSession):
        """End-to-end: corrections → pattern detection → promotion → prompt injection."""
        # Clean existing patterns
        existing = (await db.execute(select(CorrectionPattern))).scalars().all()
        for p in existing:
            await db.delete(p)
        await db.flush()

        # Step 1: Create corrections across 3 sessions
        for i in range(3):
            s = await _create_session(db)
            doc = await _create_document(db, s.id, f"pds-lifecycle-{i}.pdf", f"P-LC{i}")
            f = await _create_field(db, doc.id, "pressure_suction", "discharge")
            await _create_correction(db, f.id, "discharge", "suction", "Label mistranslation")

        # Step 2: Detect patterns
        patterns = await detect_patterns(db)
        matching = [p for p in patterns if p.field_name == "pressure_suction"]
        assert len(matching) == 1
        assert matching[0].session_count == 3

        # Step 3: Promote to CorrectionPattern
        promoted = await promote_patterns(db)
        promoted_matching = [p for p in promoted if "pressure_suction" in p.description]
        assert len(promoted_matching) == 1
        assert promoted_matching[0].is_active is True

        # Step 4: Verify pattern appears in a new session's context
        new_session = await _create_session(db)
        msg = Message(
            session_id=new_session.id, seq_number=0,
            role=MessageRole.user, content="Extract all fields",
        )
        db.add(msg)
        await db.flush()

        context = await build_context(new_session.id, build_orchestrator_prompt(), db)
        system_content = context[0]["content"]
        assert "pressure_suction" in system_content
        assert "suction" in system_content
