"""Phase 9 tests: Full extraction pipeline integration.

Tests the complete flow: session creation → PDF upload → extraction via orchestrator
→ sub-agent extraction per document → validation → query → context management → budget guards.

All LLM calls are mocked to produce deterministic, realistic tool-call sequences that
exercise the actual tool implementations against a real PostgreSQL database.
"""

import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.context_manager import (
    build_context,
    compact,
    context_token_count,
    should_compact,
)
from app.agent.cost_tracker import CostTracker
from app.agent.llm_client import LLMResponse
from app.agent.runner import AgentRunner
from app.config import settings
from app.main import app
from app.models.cost_record import CostRecord
from app.models.document import Document, DocumentStatus
from app.models.document_page import DocumentPage
from app.models.entity_relationship import EntityRelationship, RelationshipType
from app.models.equipment_entity import EquipmentEntity
from app.models.extracted_field import ExtractedField, FieldDataType, FieldStatus
from app.models.field_correction import FieldCorrection
from app.models.message import Message, MessageRole
from app.models.session import Session, SessionStatus
from app.prompts.orchestrator_prompt import build_orchestrator_prompt
from app.tools import create_orchestrator_tools


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
    num_pages: int = 3,
    status: DocumentStatus = DocumentStatus.uploaded,
) -> Document:
    doc = Document(
        session_id=session_id,
        filename=filename,
        file_path=f"/tmp/uploads/{filename}",
        pump_tag=pump_tag,
        format_type="english_tabular",
        status=status,
        num_pages=num_pages,
    )
    db.add(doc)
    await db.flush()
    return doc


async def _create_document_pages(
    db: AsyncSession,
    document_id: uuid.UUID,
    num_pages: int = 3,
) -> list[DocumentPage]:
    """Create document pages with realistic text content."""
    page_texts = [
        (
            "PUMP DATA SHEET\n"
            "Equipment Tag: P-718\n"
            "Service: Diesel Product Pump\n"
            "Type: Centrifugal, Horizontal, Single Stage\n"
            "Manufacturer: Flowserve\n"
            "Project No: 12345\n"
        ),
        (
            "OPERATING CONDITIONS\n"
            "Flow Nominal: 335 m³/h\n"
            "Flow Rated: 350 m³/h\n"
            "Temperature: 40 °C\n"
            "Density: 850 kg/m³\n"
            "Suction Pressure: 2.5 bar\n"
            "Discharge Pressure: 12.0 bar\n"
            "NPSH Available: 8.5 m\n"
            "NPSH Required: 3.2 m\n"
        ),
        (
            "CONSTRUCTION MATERIALS\n"
            "Casing: Carbon Steel\n"
            "Impeller: CS\n"
            "Shaft: AISI 4140\n"
            "MOTOR DATA\n"
            "Motor Power: 250 kW\n"
            "Voltage: 400 V\n"
            "Frequency: 50 Hz\n"
            "Speed: 2950 rpm\n"
        ),
    ]

    pages = []
    for i in range(min(num_pages, len(page_texts))):
        # Use a dummy image path — tests don't need real images
        image_path = f"test_renders/{document_id}/page_{i+1}.png"
        page = DocumentPage(
            document_id=document_id,
            page_number=i + 1,
            raw_text=page_texts[i],
            layout_text=page_texts[i],
            tables_json=None,
            image_path=image_path,
            width=595.0,
            height=842.0,
        )
        db.add(page)
        pages.append(page)
    await db.flush()
    return pages


def _make_llm_response(
    content: str | None = None,
    tool_calls: list[dict] | None = None,
    input_tokens: int = 200,
    output_tokens: int = 50,
) -> LLMResponse:
    """Create a mock LLM response."""
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
    """Create a tool call dict in OpenAI format."""
    return {
        "id": call_id or f"call_{name}_{uuid.uuid4().hex[:8]}",
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments),
        },
    }


# ──────────────────── 9.1: End-to-End Extraction Pipeline ────────────────────


class TestEndToEndExtractionPipeline:
    """Test the full orchestrator → sub-agent extraction → validation flow."""

    @pytest.mark.asyncio
    async def test_orchestrator_lists_documents_then_spawns_extraction(self, db: AsyncSession, monkeypatch):
        """Orchestrator calls get_session_documents, then spawn_extraction_agent for each doc."""
        session = await _create_session(db)
        doc1 = await _create_document(db, session.id, "pds-P718.pdf", "P-718", 3)
        doc2 = await _create_document(db, session.id, "pds-P818.pdf", "P-818", 3)
        await _create_document_pages(db, doc1.id, 3)
        await _create_document_pages(db, doc2.id, 3)
        await db.commit()

        from app.agent import runner

        call_count = 0
        spawned_docs = []

        async def mock_call_llm(**kwargs):
            nonlocal call_count
            call_count += 1

            messages = kwargs.get("messages", [])
            last_content = ""
            for m in reversed(messages):
                if m.get("content"):
                    last_content = m["content"]
                    break

            # First call: orchestrator lists documents
            if call_count == 1:
                return _make_llm_response(
                    tool_calls=[_tool_call("get_session_documents", {})]
                )

            # After seeing doc list, spawn extraction for doc1
            if call_count == 2:
                return _make_llm_response(
                    tool_calls=[_tool_call("spawn_extraction_agent", {"document_id": str(doc1.id)})]
                )

            # After doc1 extraction, spawn extraction for doc2
            if call_count == 3:
                return _make_llm_response(
                    tool_calls=[_tool_call("spawn_extraction_agent", {"document_id": str(doc2.id)})]
                )

            # After doc2 extraction, spawn validation
            if call_count == 4:
                return _make_llm_response(
                    tool_calls=[_tool_call("spawn_validation_agent", {})]
                )

            # Final response
            return _make_llm_response(
                content="Extraction complete. Processed 2 documents with validation."
            )

        monkeypatch.setattr(runner, "call_llm", mock_call_llm)

        # Build registry manually, replacing subagent tools with mocks
        from app.agent.tool_registry import ToolRegistry
        from app.tools.document_tools import register_document_tools
        from app.tools.extraction_tools import register_extraction_tools
        from app.tools.entity_tools import register_entity_tools
        from app.tools.query_tools import register_query_tools

        tools = ToolRegistry()
        register_document_tools(tools, session.id, db)
        register_extraction_tools(tools, db)
        register_entity_tools(tools, session.id, db)
        register_query_tools(tools, session.id, db)

        # Register mock subagent tools directly
        async def mock_spawn_extraction(document_id: str) -> dict:
            spawned_docs.append(document_id)
            doc_obj = await db.get(Document, uuid.UUID(document_id))
            for fname, val, section in [
                ("flow_nominal", "335", "operating_conditions"),
                ("impeller_material", "CS", "construction_materials"),
                ("motor_power", "250", "motor_data"),
            ]:
                field = ExtractedField(
                    document_id=uuid.UUID(document_id),
                    field_name=fname,
                    display_name=fname.replace("_", " ").title(),
                    raw_value=val,
                    unit="m³/h" if "flow" in fname else ("kW" if "power" in fname else None),
                    data_type=FieldDataType.numeric if "flow" in fname or "power" in fname else FieldDataType.text,
                    section=section,
                    confidence=0.92,
                    status=FieldStatus.extracted,
                    citation_page=1,
                    citation_text=f"{fname}: {val}",
                )
                db.add(field)
            doc_obj.status = DocumentStatus.extracted
            await db.flush()
            return {
                "document_id": document_id,
                "filename": doc_obj.filename,
                "summary": f"Extracted 3 fields from {doc_obj.filename}",
                "sub_session_id": str(uuid.uuid4()),
            }

        async def mock_spawn_validation() -> dict:
            return {
                "summary": "Validation passed. No naming inconsistencies found.",
                "documents_validated": 2,
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
            fn=mock_spawn_validation,
        )

        system_prompt = build_orchestrator_prompt()

        agent = AgentRunner(
            session_id=session.id,
            system_prompt=system_prompt,
            tools=tools,
            db=db,
            operation="extraction",
        )
        result = await agent.run("Extract all fields from these datasheets")

        # Verify orchestrator flow
        assert "Extraction complete" in result
        assert len(spawned_docs) == 2
        assert str(doc1.id) in spawned_docs
        assert str(doc2.id) in spawned_docs

        # Verify fields were saved to DB
        field_count_stmt = select(func.count()).select_from(ExtractedField).where(
            ExtractedField.document_id.in_([doc1.id, doc2.id])
        )
        total_fields = (await db.execute(field_count_stmt)).scalar()
        assert total_fields == 6  # 3 per doc

        # Verify documents marked as extracted
        await db.refresh(doc1)
        await db.refresh(doc2)
        assert doc1.status == DocumentStatus.extracted
        assert doc2.status == DocumentStatus.extracted

    @pytest.mark.asyncio
    async def test_extraction_persists_all_messages(self, db: AsyncSession, monkeypatch):
        """Every message in the agent loop is persisted: user, assistant, tool calls, tool results."""
        session = await _create_session(db)
        doc = await _create_document(db, session.id)
        await _create_document_pages(db, doc.id)
        await db.commit()

        from app.agent import runner

        call_count = 0

        async def mock_call_llm(**kwargs):
            nonlocal call_count
            call_count += 1

            if call_count == 1:
                return _make_llm_response(
                    tool_calls=[_tool_call("get_session_documents", {})]
                )
            return _make_llm_response(
                content="Found 1 document: pds-P718.pdf"
            )

        monkeypatch.setattr(runner, "call_llm", mock_call_llm)

        system_prompt = build_orchestrator_prompt()
        tools = create_orchestrator_tools(session.id, db)
        agent = AgentRunner(session_id=session.id, system_prompt=system_prompt, tools=tools, db=db)
        await agent.run("What documents do I have?")

        # Check persisted messages
        stmt = select(Message).where(Message.session_id == session.id).order_by(Message.seq_number)
        result = await db.execute(stmt)
        msgs = result.scalars().all()

        assert len(msgs) >= 4
        assert msgs[0].role == MessageRole.user
        assert msgs[0].content == "What documents do I have?"
        assert msgs[1].role == MessageRole.assistant
        assert msgs[1].tool_calls is not None
        assert msgs[1].tool_calls[0]["function"]["name"] == "get_session_documents"
        assert msgs[2].role == MessageRole.tool
        assert msgs[2].tool_call_id is not None
        assert msgs[3].role == MessageRole.assistant
        assert "1 document" in msgs[3].content

    @pytest.mark.asyncio
    async def test_cost_tracked_per_llm_call(self, db: AsyncSession, monkeypatch):
        """Every LLM call is tracked by the CostTracker (in-memory accumulator).

        CostRecords in the DB are written by the real call_llm function.
        With a mocked LLM, we verify the runner's cost_tracker accumulates correctly.
        """
        session = await _create_session(db)
        doc = await _create_document(db, session.id)
        await _create_document_pages(db, doc.id)
        await db.commit()

        from app.agent import runner

        call_count = 0

        async def mock_call_llm(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_llm_response(
                    tool_calls=[_tool_call("get_session_documents", {})],
                    input_tokens=500,
                    output_tokens=100,
                )
            return _make_llm_response(
                content="Done.",
                input_tokens=300,
                output_tokens=50,
            )

        monkeypatch.setattr(runner, "call_llm", mock_call_llm)

        system_prompt = build_orchestrator_prompt()
        tools = create_orchestrator_tools(session.id, db)
        agent = AgentRunner(
            session_id=session.id, system_prompt=system_prompt, tools=tools, db=db, operation="query"
        )
        await agent.run("List documents")

        # Verify cost tracker accumulated 2 LLM calls
        assert agent.cost_tracker.iterations == 2
        assert agent.cost_tracker.input_tokens == 800
        assert agent.cost_tracker.output_tokens == 150
        assert agent.cost_tracker.total_tokens == 950
        assert agent.cost_tracker.cost_usd > 0


# ──────────────────── 9.2: Extraction Quality Validation ────────────────────


class TestExtractionQualityValidation:
    """Validate that extraction sub-agents save correctly cited fields to the DB."""

    @pytest.mark.asyncio
    async def test_extraction_subagent_saves_fields_with_citations(self, db: AsyncSession, monkeypatch):
        """Sub-agent extracts fields page-by-page with proper citations."""
        session = await _create_session(db)
        doc = await _create_document(db, session.id, "pds-P718.pdf", "P-718", 3)
        await _create_document_pages(db, doc.id, 3)
        await db.commit()

        from app.agent import runner

        call_count = 0

        async def mock_call_llm(**kwargs):
            nonlocal call_count
            call_count += 1

            # Step 1: Get document info
            if call_count == 1:
                return _make_llm_response(
                    tool_calls=[_tool_call("get_document_info", {"document_id": str(doc.id)})]
                )

            # Step 2: Get page 1 content
            if call_count == 2:
                return _make_llm_response(
                    tool_calls=[_tool_call("get_page_content", {"document_id": str(doc.id), "page_number": 1})]
                )

            # Step 3: Save fields from page 1 (general info)
            if call_count == 3:
                return _make_llm_response(
                    tool_calls=[
                        _tool_call("save_extracted_field", {
                            "document_id": str(doc.id),
                            "field_name": "equipment_tag",
                            "display_name": "Equipment Tag",
                            "raw_value": "P-718",
                            "section": "general_info",
                            "confidence": 0.98,
                            "citation_page": 1,
                            "citation_text": "Equipment Tag: P-718",
                            "data_type": "text",
                        }),
                        _tool_call("save_extracted_field", {
                            "document_id": str(doc.id),
                            "field_name": "service_description",
                            "display_name": "Service Description",
                            "raw_value": "Diesel Product Pump",
                            "section": "general_info",
                            "confidence": 0.95,
                            "citation_page": 1,
                            "citation_text": "Service: Diesel Product Pump",
                            "data_type": "text",
                        }),
                    ]
                )

            # Step 4: Get page 2
            if call_count == 4:
                return _make_llm_response(
                    tool_calls=[_tool_call("get_page_content", {"document_id": str(doc.id), "page_number": 2})]
                )

            # Step 5: Save fields from page 2 (operating conditions)
            if call_count == 5:
                return _make_llm_response(
                    tool_calls=[
                        _tool_call("save_extracted_field", {
                            "document_id": str(doc.id),
                            "field_name": "flow_nominal",
                            "display_name": "Flow Nominal",
                            "raw_value": "335",
                            "unit": "m³/h",
                            "section": "operating_conditions",
                            "confidence": 0.95,
                            "citation_page": 2,
                            "citation_text": "Flow Nominal: 335 m³/h",
                            "data_type": "numeric",
                        }),
                        _tool_call("save_extracted_field", {
                            "document_id": str(doc.id),
                            "field_name": "pressure_suction",
                            "display_name": "Suction Pressure",
                            "raw_value": "2.5",
                            "unit": "bar",
                            "section": "pressure_conditions",
                            "confidence": 0.92,
                            "citation_page": 2,
                            "citation_text": "Suction Pressure: 2.5 bar",
                            "data_type": "numeric",
                        }),
                    ]
                )

            # Step 6: Get page 3
            if call_count == 6:
                return _make_llm_response(
                    tool_calls=[_tool_call("get_page_content", {"document_id": str(doc.id), "page_number": 3})]
                )

            # Step 7: Save fields from page 3 (materials + motor)
            if call_count == 7:
                return _make_llm_response(
                    tool_calls=[
                        _tool_call("save_extracted_field", {
                            "document_id": str(doc.id),
                            "field_name": "impeller_material",
                            "display_name": "Impeller Material",
                            "raw_value": "CS",
                            "section": "construction_materials",
                            "confidence": 0.88,
                            "citation_page": 3,
                            "citation_text": "Impeller: CS",
                            "data_type": "text",
                        }),
                        _tool_call("save_extracted_field", {
                            "document_id": str(doc.id),
                            "field_name": "motor_power",
                            "display_name": "Motor Power",
                            "raw_value": "250",
                            "unit": "kW",
                            "section": "motor_data",
                            "confidence": 0.96,
                            "citation_page": 3,
                            "citation_text": "Motor Power: 250 kW",
                            "data_type": "numeric",
                        }),
                    ]
                )

            # Step 8: Check extraction progress
            if call_count == 8:
                return _make_llm_response(
                    tool_calls=[_tool_call("get_extraction_progress", {"document_id": str(doc.id)})]
                )

            # Step 9: Create equipment entity
            if call_count == 9:
                return _make_llm_response(
                    tool_calls=[_tool_call("create_equipment_entity", {
                        "tag": "P-718",
                        "entity_type": "centrifugal_pump",
                        "name": "Diesel Product Pump",
                    })]
                )

            # Step 10: Mark extraction complete
            if call_count == 10:
                return _make_llm_response(
                    tool_calls=[_tool_call("mark_extraction_complete", {"document_id": str(doc.id)})]
                )

            # Final response
            return _make_llm_response(
                content="Extracted 6 fields from P-718 across 3 pages. Equipment entity created."
            )

        monkeypatch.setattr(runner, "call_llm", mock_call_llm)

        from app.tools import create_extraction_subagent_tools
        sub_tools = create_extraction_subagent_tools(session.id, doc.id, db)

        from app.prompts.extraction_prompt import build_extraction_prompt
        system_prompt = build_extraction_prompt(
            document_info={"filename": "pds-P718.pdf", "num_pages": 3, "format_type": "english_tabular"},
        )

        agent = AgentRunner(
            session_id=session.id,
            system_prompt=system_prompt,
            tools=sub_tools,
            db=db,
            max_iterations=settings.MAX_ITERATIONS_PER_SUBAGENT,
            token_budget=settings.MAX_TOKENS_PER_SUBAGENT,
            operation="extraction",
            document_id=doc.id,
        )
        result = await agent.run(f"Extract all fields from document {doc.id} (pds-P718.pdf, 3 pages).")

        assert "6 fields" in result

        # Verify fields in DB
        fields_stmt = select(ExtractedField).where(ExtractedField.document_id == doc.id)
        fields_result = await db.execute(fields_stmt)
        fields = fields_result.scalars().all()

        assert len(fields) == 6

        # Verify citation data on all fields
        for f in fields:
            assert f.citation_page >= 1
            assert f.citation_text is not None and len(f.citation_text) > 0
            assert f.confidence > 0
            assert f.status == FieldStatus.extracted

        # Check specific fields
        field_map = {f.field_name: f for f in fields}
        assert "equipment_tag" in field_map
        assert field_map["equipment_tag"].raw_value == "P-718"
        assert field_map["equipment_tag"].citation_page == 1

        assert "flow_nominal" in field_map
        assert field_map["flow_nominal"].raw_value == "335"
        assert field_map["flow_nominal"].unit == "m³/h"
        assert field_map["flow_nominal"].citation_page == 2

        assert "motor_power" in field_map
        assert field_map["motor_power"].raw_value == "250"
        assert field_map["motor_power"].unit == "kW"

        # Verify document marked as extracted
        await db.refresh(doc)
        assert doc.status == DocumentStatus.extracted

        # Verify equipment entity created
        entity_stmt = select(EquipmentEntity).where(
            EquipmentEntity.session_id == session.id,
            EquipmentEntity.tag == "P-718",
        )
        entity = (await db.execute(entity_stmt)).scalar_one_or_none()
        assert entity is not None
        assert entity.entity_type == "centrifugal_pump"
        assert entity.name == "Diesel Product Pump"

    @pytest.mark.asyncio
    async def test_fields_have_correct_data_types_and_sections(self, db: AsyncSession):
        """Verify that saved fields have correct data_type and section groupings."""
        session = await _create_session(db)
        doc = await _create_document(db, session.id)

        # Simulate saved fields across different sections
        test_fields = [
            ("equipment_tag", "P-718", "general_info", FieldDataType.text, None),
            ("flow_nominal", "335", "operating_conditions", FieldDataType.numeric, "m³/h"),
            ("pressure_suction", "2.5", "pressure_conditions", FieldDataType.numeric, "bar"),
            ("impeller_material", "CS", "construction_materials", FieldDataType.text, None),
            ("motor_power", "250", "motor_data", FieldDataType.numeric, "kW"),
        ]

        for fname, val, section, dtype, unit in test_fields:
            field = ExtractedField(
                document_id=doc.id,
                field_name=fname,
                display_name=fname.replace("_", " ").title(),
                raw_value=val,
                unit=unit,
                data_type=dtype,
                section=section,
                confidence=0.90,
                status=FieldStatus.extracted,
                citation_page=1,
                citation_text=f"{fname}: {val}",
            )
            db.add(field)
        await db.flush()

        # Verify section groupings via query
        section_stmt = (
            select(ExtractedField.section, func.count())
            .where(ExtractedField.document_id == doc.id)
            .group_by(ExtractedField.section)
        )
        result = await db.execute(section_stmt)
        sections = {row[0]: row[1] for row in result.all()}

        assert len(sections) == 5
        assert sections["general_info"] == 1
        assert sections["operating_conditions"] == 1
        assert sections["pressure_conditions"] == 1
        assert sections["construction_materials"] == 1
        assert sections["motor_data"] == 1

    @pytest.mark.asyncio
    async def test_sibling_pump_entity_relationship(self, db: AsyncSession, monkeypatch):
        """Verify that sibling pumps (P-718, P-818) get linked via entity relationships."""
        session = await _create_session(db)
        doc1 = await _create_document(db, session.id, "pds-P718.pdf", "P-718")
        doc2 = await _create_document(db, session.id, "pds-P818.pdf", "P-818")

        # Create entities
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

        from app.agent import runner
        from app.tools import create_validation_subagent_tools

        # Mark docs as extracted for validation
        doc1.status = DocumentStatus.extracted
        doc2.status = DocumentStatus.extracted
        await db.flush()

        call_count = 0

        async def mock_call_llm(**kwargs):
            nonlocal call_count
            call_count += 1

            if call_count == 1:
                # Search for entities to find potential siblings
                return _make_llm_response(
                    tool_calls=[_tool_call("search_entities", {})]
                )

            if call_count == 2:
                # Create sibling relationship
                return _make_llm_response(
                    tool_calls=[_tool_call("create_entity_relationship", {
                        "entity_a_id": str(entity1.id),
                        "entity_b_id": str(entity2.id),
                        "relationship_type": "sibling",
                    })]
                )

            return _make_llm_response(
                content="Validation complete. Created sibling relationship: P-718 ↔ P-818."
            )

        monkeypatch.setattr(runner, "call_llm", mock_call_llm)

        # Use validation sub-agent tools (which include entity tools)
        sub_tools = create_validation_subagent_tools(session.id, db)
        agent = AgentRunner(
            session_id=session.id, system_prompt="Validate.", tools=sub_tools, db=db,
        )
        await agent.run("Validate all extractions.")

        # Verify relationship in DB
        rel_stmt = select(EntityRelationship).where(
            EntityRelationship.entity_a_id == entity1.id,
            EntityRelationship.entity_b_id == entity2.id,
        )
        rel = (await db.execute(rel_stmt)).scalar_one_or_none()
        assert rel is not None
        assert rel.relationship_type == RelationshipType.sibling


# ──────────────────── 9.3: Context Management Under Load ────────────────────


class TestContextManagementUnderLoad:
    """Verify context window stays bounded and compaction fires after many messages."""

    @pytest.mark.asyncio
    async def test_context_token_count_stays_bounded(self, db: AsyncSession):
        """After many messages, context window stays within limits via compaction."""
        session = await _create_session(db)

        # Simulate 50+ messages (alternating user/assistant with tool calls)
        for i in range(60):
            role = MessageRole.user if i % 3 == 0 else (MessageRole.assistant if i % 3 == 1 else MessageRole.tool)
            content = f"{'User message ' * 20}{i}" if role == MessageRole.user else f"{'Agent response ' * 30}{i}"
            msg = Message(
                session_id=session.id,
                seq_number=i,
                role=role,
                content=content,
                tool_call_id=f"call_{i}" if role == MessageRole.tool else None,
            )
            db.add(msg)
        await db.flush()

        system_prompt = build_orchestrator_prompt()

        # Build context — it should be large at first
        context = await build_context(session.id, system_prompt, db)
        initial_tokens = context_token_count(context)

        # If context is too large, compaction should trigger
        if should_compact(context):
            await compact(session.id, db)
            context = await build_context(session.id, system_prompt, db)
            compacted_tokens = context_token_count(context)

            # After compaction, context should be smaller
            assert compacted_tokens < initial_tokens

            # Verify head_ptr advanced
            await db.refresh(session)
            assert session.head_ptr > 0

            # Verify compact_summary exists
            assert session.compact_summary is not None and len(session.compact_summary) > 0

    @pytest.mark.asyncio
    async def test_compacted_messages_excluded_from_context(self, db: AsyncSession):
        """Messages marked as compacted are not included in the LLM context."""
        session = await _create_session(db)
        session.head_ptr = 5
        session.compact_summary = "Earlier: user uploaded docs and extracted P-718."

        # Create messages: 0-4 compacted, 5-9 active
        for i in range(10):
            msg = Message(
                session_id=session.id,
                seq_number=i,
                role=MessageRole.user if i % 2 == 0 else MessageRole.assistant,
                content=f"Message {i}",
                is_compacted=(i < 5),
            )
            db.add(msg)
        await db.flush()

        context = await build_context(session.id, "System prompt.", db)

        # Context should include: system, summary, messages 5-9
        # System prompt + summary = 2 system messages
        # Active messages 5-9 = 5 messages
        # Total = 7
        assert len(context) == 7

        # First is system prompt
        assert context[0]["role"] == "system"
        assert "System prompt" in context[0]["content"]

        # Second is summary
        assert context[1]["role"] == "system"
        assert "Earlier" in context[1]["content"]

        # Rest are active messages starting from seq 5
        user_assistant_messages = [m for m in context[2:] if m["role"] in ("user", "assistant")]
        assert len(user_assistant_messages) == 5

    @pytest.mark.asyncio
    async def test_agent_can_query_old_data_after_compaction(self, db: AsyncSession, monkeypatch):
        """After compaction, agent can still access old data via tools (DB queries)."""
        session = await _create_session(db)
        doc = await _create_document(db, session.id, status=DocumentStatus.extracted)

        # Save a field early
        field = ExtractedField(
            document_id=doc.id,
            field_name="flow_nominal",
            display_name="Flow Nominal",
            raw_value="335",
            unit="m³/h",
            data_type=FieldDataType.numeric,
            section="operating_conditions",
            confidence=0.95,
            status=FieldStatus.extracted,
            citation_page=2,
            citation_text="Flow Nominal: 335 m³/h",
        )
        db.add(field)
        await db.flush()

        # Simulate compaction — old messages about extraction are summarized
        session.head_ptr = 20
        session.compact_summary = "P-718 extracted with 28 fields."

        # Create a few recent messages
        for i in range(20, 25):
            msg = Message(
                session_id=session.id, seq_number=i,
                role=MessageRole.user if i % 2 == 0 else MessageRole.assistant,
                content=f"Recent message {i}",
            )
            db.add(msg)
        await db.flush()
        await db.commit()

        from app.agent import runner

        call_count = 0

        async def mock_call_llm(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_llm_response(
                    tool_calls=[_tool_call("search_fields", {"field_name": "flow_nominal"})]
                )
            return _make_llm_response(
                content="The flow_nominal for P-718 is 335 m³/h (page 2, confidence: 0.95)."
            )

        monkeypatch.setattr(runner, "call_llm", mock_call_llm)

        tools = create_orchestrator_tools(session.id, db)
        agent = AgentRunner(session_id=session.id, system_prompt="System.", tools=tools, db=db)
        result = await agent.run("What is the flow rate for P-718?")

        assert "335" in result


# ──────────────────── 9.4: Budget Guard Validation ────────────────────


class TestBudgetGuardValidation:
    """Verify budget guards prevent runaway execution."""

    @pytest.mark.asyncio
    async def test_iteration_limit_stops_agent(self, db: AsyncSession, monkeypatch):
        """Agent stops after hitting iteration limit, preserving progress."""
        session = await _create_session(db)
        doc = await _create_document(db, session.id)
        await db.commit()

        from app.agent import runner

        async def mock_call_llm(**kwargs):
            # Always return a tool call — forces the loop to continue
            return _make_llm_response(
                tool_calls=[_tool_call("get_session_documents", {})]
            )

        monkeypatch.setattr(runner, "call_llm", mock_call_llm)

        tools = create_orchestrator_tools(session.id, db)
        agent = AgentRunner(
            session_id=session.id,
            system_prompt="System.",
            tools=tools,
            db=db,
            max_iterations=3,  # Very low limit
        )
        result = await agent.run("Extract everything")

        assert "Budget exceeded" in result
        assert agent.cost_tracker.iterations == 3

        # Verify messages were persisted even though budget was exceeded
        msg_stmt = select(func.count()).select_from(Message).where(Message.session_id == session.id)
        msg_count = (await db.execute(msg_stmt)).scalar()
        assert msg_count >= 4  # user + at least 3 iterations of (assistant+tool) + budget msg

    @pytest.mark.asyncio
    async def test_token_budget_stops_agent(self, db: AsyncSession, monkeypatch):
        """Agent stops when token budget is exceeded."""
        session = await _create_session(db)
        doc = await _create_document(db, session.id)
        await db.commit()

        from app.agent import runner

        async def mock_call_llm(**kwargs):
            return _make_llm_response(
                tool_calls=[_tool_call("get_session_documents", {})],
                input_tokens=20000,  # Large token usage per call
                output_tokens=10000,
            )

        monkeypatch.setattr(runner, "call_llm", mock_call_llm)

        tools = create_orchestrator_tools(session.id, db)
        agent = AgentRunner(
            session_id=session.id,
            system_prompt="System.",
            tools=tools,
            db=db,
            max_iterations=100,
            token_budget=50000,  # Will be exceeded after 2 calls (60k tokens)
        )
        result = await agent.run("Extract everything")

        assert "Budget exceeded" in result
        assert agent.cost_tracker.total_tokens >= 50000

    @pytest.mark.asyncio
    async def test_budget_warning_injected_at_80_percent(self, db: AsyncSession):
        """CostTracker returns a warning when approaching 80% of budget."""
        tracker = CostTracker(
            session_id=uuid.uuid4(),
            max_iterations=10,
            max_tokens=10000,
        )

        # At 80% of iterations
        tracker.iterations = 8
        tracker.input_tokens = 2000
        tracker.output_tokens = 500

        warning = tracker.get_run_warning()
        assert warning is not None
        assert "iteration limit" in warning.lower() or "Approaching" in warning

        # At 80% of tokens
        tracker2 = CostTracker(
            session_id=uuid.uuid4(),
            max_iterations=100,
            max_tokens=10000,
        )
        tracker2.input_tokens = 6000
        tracker2.output_tokens = 2500  # Total = 8500 > 80% of 10000

        warning2 = tracker2.get_run_warning()
        assert warning2 is not None
        assert "token" in warning2.lower() or "Approaching" in warning2

    @pytest.mark.asyncio
    async def test_session_budget_checked_against_db(self, db: AsyncSession):
        """Session-level budget checks aggregate cost records from DB."""
        session = await _create_session(db)

        # Add cost records totaling near the session limit
        for _ in range(50):
            cr = CostRecord(
                session_id=session.id,
                operation="extraction",
                model="test-model",
                input_tokens=8000,
                output_tokens=2000,
                cost_usd=0.09,
                duration_sec=1.0,
            )
            db.add(cr)
        await db.flush()

        tracker = CostTracker(session_id=session.id)

        exceeded, warning = await tracker.check_session_budget(db)
        # 50 * 10000 tokens = 500,000 >= MAX_TOKENS_PER_SESSION (500,000)
        # This should trigger at least a warning or exceeded
        assert exceeded or warning is not None

    @pytest.mark.asyncio
    async def test_subagent_has_own_budget_limits(self, db: AsyncSession, monkeypatch):
        """Sub-agents run with their own (lower) budget limits."""
        session = await _create_session(db)
        doc = await _create_document(db, session.id)
        await _create_document_pages(db, doc.id)
        await db.commit()

        from app.agent import runner

        async def mock_call_llm(**kwargs):
            # Always return tool calls to force iteration
            return _make_llm_response(
                tool_calls=[_tool_call("get_session_documents", {})]
            )

        monkeypatch.setattr(runner, "call_llm", mock_call_llm)

        from app.tools import create_extraction_subagent_tools
        sub_tools = create_extraction_subagent_tools(session.id, doc.id, db)

        # Sub-agent with its own lower limits
        agent = AgentRunner(
            session_id=session.id,
            system_prompt="Extract fields.",
            tools=sub_tools,
            db=db,
            max_iterations=settings.MAX_ITERATIONS_PER_SUBAGENT,
            token_budget=settings.MAX_TOKENS_PER_SUBAGENT,
            operation="extraction",
            document_id=doc.id,
        )

        result = await agent.run("Extract fields from this document.")

        assert "Budget exceeded" in result
        assert agent.cost_tracker.iterations == settings.MAX_ITERATIONS_PER_SUBAGENT
        assert agent.cost_tracker.max_iterations == settings.MAX_ITERATIONS_PER_SUBAGENT
        assert agent.cost_tracker.max_tokens == settings.MAX_TOKENS_PER_SUBAGENT


# ──────────────────── 9.x: API-Level E2E Tests ────────────────────


class TestAPIEndToEnd:
    """End-to-end tests hitting the actual HTTP endpoints."""

    @pytest.mark.asyncio
    async def test_full_flow_via_api(self, db: AsyncSession, monkeypatch):
        """Create session → upload → query → verify via API endpoints."""
        transport = ASGITransport(app=app)

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. Create session
            resp = await client.post("/api/v1/sessions")
            assert resp.status_code in (200, 201)
            session_id = resp.json()["id"]

            # Manually create a doc in DB for this session since PDF upload needs real files
            session_obj = await db.get(Session, uuid.UUID(session_id))
            doc = Document(
                session_id=uuid.UUID(session_id),
                filename="pds-P718.pdf",
                file_path="/tmp/pds-P718.pdf",
                pump_tag="P-718",
                format_type="english_tabular",
                status=DocumentStatus.extracted,
                num_pages=3,
            )
            db.add(doc)
            await db.flush()

            # Add fields
            for fname, val, section in [
                ("flow_nominal", "335", "operating_conditions"),
                ("motor_power", "250", "motor_data"),
            ]:
                f = ExtractedField(
                    document_id=doc.id,
                    field_name=fname,
                    display_name=fname.replace("_", " ").title(),
                    raw_value=val,
                    unit="m³/h" if "flow" in fname else "kW",
                    data_type=FieldDataType.numeric,
                    section=section,
                    confidence=0.95,
                    status=FieldStatus.extracted,
                    citation_page=1,
                    citation_text=f"{fname}: {val}",
                )
                db.add(f)
            await db.flush()
            await db.commit()

            # 2. List documents (returns a plain list)
            resp = await client.get(f"/api/v1/sessions/{session_id}/documents")
            assert resp.status_code == 200
            docs_data = resp.json()
            assert len(docs_data) == 1
            assert docs_data[0]["pump_tag"] == "P-718"

            # 3. Check fields
            resp = await client.get(f"/api/v1/sessions/{session_id}/fields")
            assert resp.status_code == 200
            fields_data = resp.json()
            assert fields_data["total"] == 2

            # 4. Field stats
            resp = await client.get(f"/api/v1/sessions/{session_id}/fields/stats")
            assert resp.status_code == 200
            stats = resp.json()
            assert stats["total_fields"] == 2
            assert "operating_conditions" in stats["by_section"]

            # 5. Chat query (mocked LLM)
            from app.agent import runner

            async def mock_call_llm(**kwargs):
                return _make_llm_response(
                    content="The flow rate for P-718 is 335 m³/h."
                )

            monkeypatch.setattr(runner, "call_llm", mock_call_llm)

            resp = await client.post(
                f"/api/v1/sessions/{session_id}/chat",
                json={"message": "What is the flow rate for P-718?"},
            )
            assert resp.status_code == 200
            chat_data = resp.json()
            assert chat_data["status"] == "completed"
            assert "335" in chat_data["response"]

    @pytest.mark.asyncio
    async def test_chat_with_tool_calls_and_db_verification(self, db: AsyncSession, monkeypatch):
        """Chat triggers tool calls; verify the entire message chain in DB."""
        session = await _create_session(db)
        doc = await _create_document(db, session.id)
        await db.commit()

        from app.agent import runner

        call_count = 0

        async def mock_call_llm(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_llm_response(
                    tool_calls=[_tool_call("get_session_documents", {})]
                )
            return _make_llm_response(
                content="You have 1 document: pds-P718.pdf (P-718, uploaded)."
            )

        monkeypatch.setattr(runner, "call_llm", mock_call_llm)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                f"/api/v1/sessions/{session.id}/chat",
                json={"message": "Show me my documents"},
            )

        assert resp.status_code == 200
        assert resp.json()["status"] == "completed"

        # Verify full message chain in DB via messages endpoint
        from app.database import async_session_factory
        async with async_session_factory() as fresh_db:
            stmt = (
                select(Message)
                .where(Message.session_id == session.id)
                .order_by(Message.seq_number)
            )
            result = await fresh_db.execute(stmt)
            msgs = result.scalars().all()

        roles = [m.role for m in msgs]
        assert MessageRole.user in roles
        assert MessageRole.assistant in roles
        assert MessageRole.tool in roles

        # Tool result should contain document data
        tool_msg = next(m for m in msgs if m.role == MessageRole.tool)
        assert tool_msg.tool_result is not None
        tool_data = tool_msg.tool_result
        assert "documents" in tool_data or "count" in tool_data


# ──────────────────── 9.x: Sequential Extraction Ordering ────────────────────


class TestSequentialExtraction:
    """Verify documents are extracted sequentially, not in parallel."""

    @pytest.mark.asyncio
    async def test_documents_extracted_in_sequence(self, db: AsyncSession, monkeypatch):
        """Orchestrator extracts doc1 fully before starting doc2."""
        session = await _create_session(db)
        doc1 = await _create_document(db, session.id, "pds-P718.pdf", "P-718")
        doc2 = await _create_document(db, session.id, "pds-P818.pdf", "P-818")
        await db.commit()

        from app.agent import runner

        extraction_order = []

        call_count = 0

        async def mock_call_llm(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_llm_response(tool_calls=[_tool_call("get_session_documents", {})])
            if call_count == 2:
                return _make_llm_response(tool_calls=[_tool_call("spawn_extraction_agent", {"document_id": str(doc1.id)})])
            if call_count == 3:
                return _make_llm_response(tool_calls=[_tool_call("spawn_extraction_agent", {"document_id": str(doc2.id)})])
            if call_count == 4:
                return _make_llm_response(tool_calls=[_tool_call("spawn_validation_agent", {})])
            return _make_llm_response(content="All done.")

        monkeypatch.setattr(runner, "call_llm", mock_call_llm)

        # Build registry manually with mock subagent tools
        from app.agent.tool_registry import ToolRegistry
        from app.tools.document_tools import register_document_tools
        from app.tools.extraction_tools import register_extraction_tools
        from app.tools.entity_tools import register_entity_tools
        from app.tools.query_tools import register_query_tools

        tools = ToolRegistry()
        register_document_tools(tools, session.id, db)
        register_extraction_tools(tools, db)
        register_entity_tools(tools, session.id, db)
        register_query_tools(tools, session.id, db)

        async def mock_spawn_extraction(document_id: str) -> dict:
            extraction_order.append(document_id)
            doc = await db.get(Document, uuid.UUID(document_id))
            doc.status = DocumentStatus.extracted
            await db.flush()
            return {
                "document_id": document_id,
                "filename": doc.filename,
                "summary": f"Extracted from {doc.filename}",
                "sub_session_id": str(uuid.uuid4()),
            }

        async def mock_spawn_validation() -> dict:
            return {"summary": "OK", "documents_validated": 2, "sub_session_id": str(uuid.uuid4())}

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
            fn=mock_spawn_validation,
        )
        agent = AgentRunner(
            session_id=session.id, system_prompt="System.", tools=tools, db=db, operation="extraction",
        )
        await agent.run("Extract all documents")

        # Verify sequential order
        assert len(extraction_order) == 2
        assert extraction_order[0] == str(doc1.id)
        assert extraction_order[1] == str(doc2.id)


# ──────────────────── 9.x: Correction-Aware Re-Extraction ────────────────────


class TestCorrectionAwareReExtraction:
    """Corrections from earlier documents inform subsequent extractions."""

    @pytest.mark.asyncio
    async def test_corrections_loaded_into_subagent_prompt(self, db: AsyncSession):
        """When spawning a sub-agent, corrections from sibling docs are loaded."""
        session = await _create_session(db)
        doc1 = await _create_document(db, session.id, "pds-P718.pdf", "P-718", status=DocumentStatus.extracted)
        doc2 = await _create_document(db, session.id, "pds-P818.pdf", "P-818")

        # Create a corrected field on doc1
        field = ExtractedField(
            document_id=doc1.id,
            field_name="impeller_material",
            display_name="Impeller Material",
            raw_value="SS 316",
            data_type=FieldDataType.text,
            section="construction_materials",
            confidence=0.88,
            status=FieldStatus.corrected,
            citation_page=3,
            citation_text="Impeller: CS",
        )
        db.add(field)
        await db.flush()

        correction = FieldCorrection(
            field_id=field.id,
            original_value="CS",
            corrected_value="SS 316",
            reason="Spec updated last month",
            corrected_by="user",
        )
        db.add(correction)
        await db.flush()

        # Load corrections for doc2 (should include doc1's corrections via session)
        from app.tools.subagent_tools import _load_corrections_for_document

        corrections = await _load_corrections_for_document(doc2.id, session.id, db)
        assert len(corrections) >= 1
        assert corrections[0]["field_name"] == "impeller_material"
        assert corrections[0]["original"] == "CS"
        assert corrections[0]["corrected"] == "SS 316"
        assert corrections[0]["reason"] == "Spec updated last month"

    @pytest.mark.asyncio
    async def test_field_correction_creates_audit_trail(self, db: AsyncSession, monkeypatch):
        """Correcting a field via agent creates both the correction record and updates the field."""
        session = await _create_session(db)
        doc = await _create_document(db, session.id, status=DocumentStatus.extracted)

        # Save an initial field
        field = ExtractedField(
            document_id=doc.id,
            field_name="impeller_material",
            display_name="Impeller Material",
            raw_value="CS",
            data_type=FieldDataType.text,
            section="construction_materials",
            confidence=0.88,
            status=FieldStatus.extracted,
            citation_page=3,
            citation_text="Impeller: CS",
        )
        db.add(field)
        await db.flush()
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
                        "reason": "User correction: spec updated",
                    })]
                )
            return _make_llm_response(
                content="Corrected impeller_material for P-718: CS → SS 316."
            )

        monkeypatch.setattr(runner, "call_llm", mock_call_llm)

        tools = create_orchestrator_tools(session.id, db)
        agent = AgentRunner(session_id=session.id, system_prompt="System.", tools=tools, db=db)
        result = await agent.run("The impeller material for P-718 is wrong. Should be SS 316.")

        assert "SS 316" in result

        # Verify field updated
        await db.refresh(field)
        assert field.raw_value == "SS 316"
        assert field.status == FieldStatus.corrected

        # Verify correction record exists
        corr_stmt = select(FieldCorrection).where(FieldCorrection.field_id == field_id)
        correction = (await db.execute(corr_stmt)).scalar_one()
        assert correction.original_value == "CS"
        assert correction.corrected_value == "SS 316"
        assert "spec updated" in correction.reason
