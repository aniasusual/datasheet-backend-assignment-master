"""Phase 4 tests: document tools and extraction tools."""

import json
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.tool_registry import ToolRegistry
from app.models.document import Document, DocumentStatus
from app.models.document_page import DocumentPage
from app.models.extracted_field import ExtractedField, FieldDataType, FieldStatus
from app.models.field_correction import FieldCorrection
from app.models.session import Session, SessionStatus
from app.tools import create_all_phase4_tools
from app.tools.document_tools import register_document_tools
from app.tools.extraction_tools import register_extraction_tools


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


async def _create_page(
    db: AsyncSession,
    document_id: uuid.UUID,
    page_number: int = 0,
    raw_text: str = "Pump Data Sheet\nFlow: 335 m³/h",
) -> DocumentPage:
    page = DocumentPage(
        document_id=document_id,
        page_number=page_number,
        raw_text=raw_text,
        layout_text=raw_text,
        tables_json=[["Flow", "335", "m³/h"]],
        image_path="/tmp/nonexistent.png",
        width=612.0,
        height=792.0,
    )
    db.add(page)
    await db.flush()
    return page


async def _create_field(
    db: AsyncSession,
    document_id: uuid.UUID,
    field_name: str = "flow_nominal",
    raw_value: str = "335",
    section: str = "operating_conditions",
    confidence: float = 0.95,
) -> ExtractedField:
    field = ExtractedField(
        document_id=document_id,
        field_name=field_name,
        display_name=field_name.replace("_", " ").title(),
        raw_value=raw_value,
        unit="m³/h",
        data_type=FieldDataType.numeric,
        section=section,
        confidence=confidence,
        status=FieldStatus.extracted,
        citation_page=0,
        citation_text=f"{field_name}: {raw_value}",
    )
    db.add(field)
    await db.flush()
    return field


# ──────────────────── Document Tools Tests ────────────────────


class TestDocumentTools:
    @pytest.mark.asyncio
    async def test_get_session_documents(self, db: AsyncSession):
        session = await _create_session(db)
        doc1 = await _create_document(db, session.id, "pds-P718.pdf", "P-718")
        doc2 = await _create_document(db, session.id, "pds-P818.pdf", "P-818")

        registry = ToolRegistry()
        register_document_tools(registry, session.id, db)

        result_str = await registry.execute_tool("get_session_documents", {})
        result = json.loads(result_str)

        assert result["count"] == 2
        assert len(result["documents"]) == 2
        filenames = {d["filename"] for d in result["documents"]}
        assert "pds-P718.pdf" in filenames
        assert "pds-P818.pdf" in filenames

    @pytest.mark.asyncio
    async def test_get_session_documents_empty(self, db: AsyncSession):
        session = await _create_session(db)
        registry = ToolRegistry()
        register_document_tools(registry, session.id, db)

        result_str = await registry.execute_tool("get_session_documents", {})
        result = json.loads(result_str)

        assert result["count"] == 0
        assert result["documents"] == []

    @pytest.mark.asyncio
    async def test_get_document_info(self, db: AsyncSession):
        session = await _create_session(db)
        doc = await _create_document(db, session.id)

        registry = ToolRegistry()
        register_document_tools(registry, session.id, db)

        result_str = await registry.execute_tool(
            "get_document_info", {"document_id": str(doc.id)}
        )
        result = json.loads(result_str)

        assert result["filename"] == "pds-P718.pdf"
        assert result["pump_tag"] == "P-718"
        assert result["num_pages"] == 3
        assert result["status"] == "uploaded"

    @pytest.mark.asyncio
    async def test_get_document_info_not_found(self, db: AsyncSession):
        session = await _create_session(db)
        registry = ToolRegistry()
        register_document_tools(registry, session.id, db)

        fake_id = str(uuid.uuid4())
        result_str = await registry.execute_tool(
            "get_document_info", {"document_id": fake_id}
        )
        result = json.loads(result_str)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_get_document_info_wrong_session(self, db: AsyncSession):
        session1 = await _create_session(db)
        session2 = await _create_session(db)
        doc = await _create_document(db, session1.id)

        # Register tools for session2 — doc belongs to session1
        registry = ToolRegistry()
        register_document_tools(registry, session2.id, db)

        result_str = await registry.execute_tool(
            "get_document_info", {"document_id": str(doc.id)}
        )
        result = json.loads(result_str)
        assert "error" in result
        assert "does not belong" in result["error"]

    @pytest.mark.asyncio
    async def test_get_page_content(self, db: AsyncSession):
        session = await _create_session(db)
        doc = await _create_document(db, session.id)
        page = await _create_page(db, doc.id, page_number=0)

        registry = ToolRegistry()
        register_document_tools(registry, session.id, db)

        result_str = await registry.execute_tool(
            "get_page_content",
            {"document_id": str(doc.id), "page_number": 0},
        )
        result = json.loads(result_str)

        assert result["page_number"] == 0
        assert "Pump Data Sheet" in result["raw_text"]
        assert result["tables_json"] is not None
        # Image file doesn't exist in test, so base64 should be None
        assert result["image_base64"] is None
        assert result["width"] == 612.0

    @pytest.mark.asyncio
    async def test_get_page_content_not_found(self, db: AsyncSession):
        session = await _create_session(db)
        doc = await _create_document(db, session.id)

        registry = ToolRegistry()
        register_document_tools(registry, session.id, db)

        result_str = await registry.execute_tool(
            "get_page_content",
            {"document_id": str(doc.id), "page_number": 99},
        )
        result = json.loads(result_str)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_get_document_text(self, db: AsyncSession):
        session = await _create_session(db)
        doc = await _create_document(db, session.id, num_pages=2)
        await _create_page(db, doc.id, 0, "Page 0 text")
        await _create_page(db, doc.id, 1, "Page 1 text")

        registry = ToolRegistry()
        register_document_tools(registry, session.id, db)

        result_str = await registry.execute_tool(
            "get_document_text", {"document_id": str(doc.id)}
        )
        result = json.loads(result_str)

        assert result["num_pages"] == 2
        assert "Page 0 text" in result["text"]
        assert "Page 1 text" in result["text"]
        assert "--- Page 0 ---" in result["text"]


# ──────────────────── Extraction Tools Tests ────────────────────


class TestExtractionTools:
    @pytest.mark.asyncio
    async def test_save_extracted_field(self, db: AsyncSession):
        session = await _create_session(db)
        doc = await _create_document(db, session.id)

        registry = ToolRegistry()
        register_extraction_tools(registry, db)

        result_str = await registry.execute_tool(
            "save_extracted_field",
            {
                "document_id": str(doc.id),
                "field_name": "Flow Nominal",
                "display_name": "Nominal Flow",
                "raw_value": "335",
                "unit": "m³/h",
                "data_type": "numeric",
                "section": "operating_conditions",
                "confidence": 0.95,
                "citation_page": 0,
                "citation_text": "FLOW: 335 m³/h",
                "citation_bbox": {"x0": 100, "y0": 200, "x1": 300, "y1": 220},
            },
        )
        result = json.loads(result_str)

        assert "id" in result
        assert result["field_name"] == "flow_nominal"  # snake_case normalized
        assert result["raw_value"] == "335"
        assert result["confidence"] == 0.95
        assert result["status"] == "extracted"

        # Verify in DB
        field = await db.get(ExtractedField, uuid.UUID(result["id"]))
        assert field is not None
        assert field.field_name == "flow_nominal"
        assert field.unit == "m³/h"
        assert field.data_type == FieldDataType.numeric
        assert field.citation_bbox == {"x0": 100, "y0": 200, "x1": 300, "y1": 220}

    @pytest.mark.asyncio
    async def test_save_field_snake_case_normalization(self, db: AsyncSession):
        session = await _create_session(db)
        doc = await _create_document(db, session.id)

        registry = ToolRegistry()
        register_extraction_tools(registry, db)

        result_str = await registry.execute_tool(
            "save_extracted_field",
            {
                "document_id": str(doc.id),
                "field_name": "Impeller Material",
                "display_name": "Impeller Material",
                "raw_value": "CS",
                "section": "construction_materials",
                "confidence": 0.90,
                "citation_page": 1,
                "citation_text": "Impeller: CS",
            },
        )
        result = json.loads(result_str)
        assert result["field_name"] == "impeller_material"

    @pytest.mark.asyncio
    async def test_save_field_invalid_document(self, db: AsyncSession):
        registry = ToolRegistry()
        register_extraction_tools(registry, db)

        fake_id = str(uuid.uuid4())
        result_str = await registry.execute_tool(
            "save_extracted_field",
            {
                "document_id": fake_id,
                "field_name": "test",
                "display_name": "Test",
                "raw_value": "val",
                "section": "general_info",
                "confidence": 0.5,
                "citation_page": 0,
                "citation_text": "test",
            },
        )
        result = json.loads(result_str)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_update_extracted_field(self, db: AsyncSession):
        session = await _create_session(db)
        doc = await _create_document(db, session.id)
        field = await _create_field(db, doc.id, "impeller_material", "CS")

        registry = ToolRegistry()
        register_extraction_tools(registry, db)

        result_str = await registry.execute_tool(
            "update_extracted_field",
            {
                "field_id": str(field.id),
                "corrected_value": "SS 316",
                "reason": "User correction: spec updated last month",
            },
        )
        result = json.loads(result_str)

        assert result["original_value"] == "CS"
        assert result["corrected_value"] == "SS 316"
        assert result["status"] == "corrected"

        # Verify field updated in DB
        await db.refresh(field)
        assert field.raw_value == "SS 316"
        assert field.status == FieldStatus.corrected

        # Verify correction record created
        stmt = select(FieldCorrection).where(FieldCorrection.field_id == field.id)
        corrections = (await db.execute(stmt)).scalars().all()
        assert len(corrections) == 1
        assert corrections[0].original_value == "CS"
        assert corrections[0].corrected_value == "SS 316"

    @pytest.mark.asyncio
    async def test_update_field_not_found(self, db: AsyncSession):
        registry = ToolRegistry()
        register_extraction_tools(registry, db)

        result_str = await registry.execute_tool(
            "update_extracted_field",
            {"field_id": str(uuid.uuid4()), "corrected_value": "X"},
        )
        result = json.loads(result_str)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_delete_extracted_field(self, db: AsyncSession):
        session = await _create_session(db)
        doc = await _create_document(db, session.id)
        field = await _create_field(db, doc.id)
        field_id = field.id

        registry = ToolRegistry()
        register_extraction_tools(registry, db)

        result_str = await registry.execute_tool(
            "delete_extracted_field", {"field_id": str(field_id)}
        )
        result = json.loads(result_str)

        assert result["deleted"] is True
        assert result["field_name"] == "flow_nominal"

        # Verify deleted from DB
        deleted = await db.get(ExtractedField, field_id)
        assert deleted is None

    @pytest.mark.asyncio
    async def test_get_extraction_progress(self, db: AsyncSession):
        session = await _create_session(db)
        doc = await _create_document(db, session.id)

        # Create fields in different sections/confidences
        await _create_field(db, doc.id, "flow_nominal", "335", "operating_conditions", 0.95)
        await _create_field(db, doc.id, "temperature_pumping", "40", "operating_conditions", 0.85)
        await _create_field(db, doc.id, "impeller_material", "CS", "construction_materials", 0.60)
        await _create_field(db, doc.id, "shaft_material", "??", "construction_materials", 0.30)

        registry = ToolRegistry()
        register_extraction_tools(registry, db)

        result_str = await registry.execute_tool(
            "get_extraction_progress", {"document_id": str(doc.id)}
        )
        result = json.loads(result_str)

        assert result["total_fields"] == 4
        assert result["by_section"]["operating_conditions"] == 2
        assert result["by_section"]["construction_materials"] == 2
        assert result["by_confidence_tier"]["high"] == 2    # >= 0.8
        assert result["by_confidence_tier"]["medium"] == 1  # 0.5-0.8
        assert result["by_confidence_tier"]["low"] == 1     # < 0.5
        assert result["by_status"]["extracted"] == 4

    @pytest.mark.asyncio
    async def test_mark_extraction_complete(self, db: AsyncSession):
        session = await _create_session(db)
        doc = await _create_document(db, session.id, status=DocumentStatus.uploaded)

        registry = ToolRegistry()
        register_extraction_tools(registry, db)

        result_str = await registry.execute_tool(
            "mark_extraction_complete", {"document_id": str(doc.id)}
        )
        result = json.loads(result_str)

        assert result["status"] == "extracted"

        # Verify in DB
        await db.refresh(doc)
        assert doc.status == DocumentStatus.extracted


# ──────────────────── Factory Tests ────────────────────


class TestToolFactories:
    @pytest.mark.asyncio
    async def test_create_all_phase4_tools(self, db: AsyncSession):
        session = await _create_session(db)
        registry = create_all_phase4_tools(session.id, db)

        expected_tools = {
            "get_session_documents",
            "get_document_info",
            "get_page_content",
            "get_document_text",
            "save_extracted_field",
            "update_extracted_field",
            "delete_extracted_field",
            "get_extraction_progress",
            "mark_extraction_complete",
        }
        assert set(registry.tool_names) == expected_tools
        assert len(registry) == 9

    @pytest.mark.asyncio
    async def test_tool_schemas_valid(self, db: AsyncSession):
        session = await _create_session(db)
        registry = create_all_phase4_tools(session.id, db)

        tools_for_llm = registry.get_tools_for_llm()
        assert len(tools_for_llm) == 9

        for tool in tools_for_llm:
            assert tool["type"] == "function"
            func_def = tool["function"]
            assert "name" in func_def
            assert "description" in func_def
            assert "parameters" in func_def
            params = func_def["parameters"]
            assert params["type"] == "object"
            assert "properties" in params
            assert "required" in params
