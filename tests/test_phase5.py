"""Phase 5 tests: entity tools, query tools, sub-agent tools, and factory functions."""

import json
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.tool_registry import ToolRegistry
from app.models.correction_pattern import CorrectionPattern
from app.models.document import Document, DocumentStatus
from app.models.entity_relationship import EntityRelationship, RelationshipType
from app.models.equipment_entity import EquipmentEntity
from app.models.extracted_field import ExtractedField, FieldDataType, FieldStatus
from app.models.field_correction import FieldCorrection
from app.models.session import Session, SessionStatus
from app.tools import (
    create_extraction_subagent_tools,
    create_orchestrator_tools,
    create_validation_subagent_tools,
)
from app.tools.entity_tools import register_entity_tools
from app.tools.query_tools import register_query_tools


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


async def _create_entity(
    db: AsyncSession,
    session_id: uuid.UUID,
    tag: str = "P-718",
    entity_type: str = "centrifugal_pump",
    name: str = "Diesel Product Pump",
) -> EquipmentEntity:
    entity = EquipmentEntity(
        session_id=session_id,
        tag=tag,
        entity_type=entity_type,
        name=name,
    )
    db.add(entity)
    await db.flush()
    return entity


# ──────────────────── Entity Tools Tests ────────────────────


class TestEntityTools:
    @pytest.mark.asyncio
    async def test_create_equipment_entity(self, db: AsyncSession):
        session = await _create_session(db)
        registry = ToolRegistry()
        register_entity_tools(registry, session.id, db)

        result_str = await registry.execute_tool(
            "create_equipment_entity",
            {
                "tag": "P-718",
                "entity_type": "centrifugal_pump",
                "name": "Diesel Product Pump",
                "metadata": {"unit": "032"},
            },
        )
        result = json.loads(result_str)

        assert "id" in result
        assert result["tag"] == "P-718"
        assert result["entity_type"] == "centrifugal_pump"
        assert result["name"] == "Diesel Product Pump"
        assert result["metadata"] == {"unit": "032"}

        # Verify in DB
        entity = await db.get(EquipmentEntity, uuid.UUID(result["id"]))
        assert entity is not None
        assert entity.session_id == session.id

    @pytest.mark.asyncio
    async def test_link_entity_to_document(self, db: AsyncSession):
        session = await _create_session(db)
        doc = await _create_document(db, session.id)
        entity = await _create_entity(db, session.id)

        registry = ToolRegistry()
        register_entity_tools(registry, session.id, db)

        result_str = await registry.execute_tool(
            "link_entity_to_document",
            {"entity_id": str(entity.id), "document_id": str(doc.id)},
        )
        result = json.loads(result_str)
        assert result["linked"] is True

    @pytest.mark.asyncio
    async def test_link_entity_to_document_wrong_session(self, db: AsyncSession):
        session1 = await _create_session(db)
        session2 = await _create_session(db)
        doc = await _create_document(db, session1.id)
        entity = await _create_entity(db, session1.id)

        # Register for session2
        registry = ToolRegistry()
        register_entity_tools(registry, session2.id, db)

        result_str = await registry.execute_tool(
            "link_entity_to_document",
            {"entity_id": str(entity.id), "document_id": str(doc.id)},
        )
        result = json.loads(result_str)
        assert "error" in result
        assert "does not belong" in result["error"]

    @pytest.mark.asyncio
    async def test_link_entity_to_fields(self, db: AsyncSession):
        session = await _create_session(db)
        doc = await _create_document(db, session.id)
        entity = await _create_entity(db, session.id)
        field1 = await _create_field(db, doc.id, "flow_nominal", "335")
        field2 = await _create_field(db, doc.id, "temperature_pumping", "40")

        registry = ToolRegistry()
        register_entity_tools(registry, session.id, db)

        result_str = await registry.execute_tool(
            "link_entity_to_fields",
            {
                "entity_id": str(entity.id),
                "field_ids": [str(field1.id), str(field2.id)],
            },
        )
        result = json.loads(result_str)
        assert result["linked_count"] == 2

        # Verify in DB
        await db.refresh(field1)
        await db.refresh(field2)
        assert field1.entity_id == entity.id
        assert field2.entity_id == entity.id

    @pytest.mark.asyncio
    async def test_link_entity_to_fields_partial(self, db: AsyncSession):
        session = await _create_session(db)
        doc = await _create_document(db, session.id)
        entity = await _create_entity(db, session.id)
        field1 = await _create_field(db, doc.id)

        registry = ToolRegistry()
        register_entity_tools(registry, session.id, db)

        fake_id = str(uuid.uuid4())
        result_str = await registry.execute_tool(
            "link_entity_to_fields",
            {
                "entity_id": str(entity.id),
                "field_ids": [str(field1.id), fake_id],
            },
        )
        result = json.loads(result_str)
        assert result["linked_count"] == 1
        assert fake_id in result["not_found"]

    @pytest.mark.asyncio
    async def test_create_entity_relationship(self, db: AsyncSession):
        session = await _create_session(db)
        entity_a = await _create_entity(db, session.id, "P-718", "centrifugal_pump", "Pump A")
        entity_b = await _create_entity(db, session.id, "P-818", "centrifugal_pump", "Pump B")

        registry = ToolRegistry()
        register_entity_tools(registry, session.id, db)

        result_str = await registry.execute_tool(
            "create_entity_relationship",
            {
                "entity_a_id": str(entity_a.id),
                "entity_b_id": str(entity_b.id),
                "relationship_type": "sibling",
            },
        )
        result = json.loads(result_str)

        assert "id" in result
        assert result["relationship_type"] == "sibling"
        assert result["entity_a"]["tag"] == "P-718"
        assert result["entity_b"]["tag"] == "P-818"

        # Verify in DB
        rel = await db.get(EntityRelationship, uuid.UUID(result["id"]))
        assert rel is not None
        assert rel.relationship_type == RelationshipType.sibling

    @pytest.mark.asyncio
    async def test_create_entity_relationship_invalid_type(self, db: AsyncSession):
        session = await _create_session(db)
        entity_a = await _create_entity(db, session.id, "P-718")
        entity_b = await _create_entity(db, session.id, "P-818")

        registry = ToolRegistry()
        register_entity_tools(registry, session.id, db)

        result_str = await registry.execute_tool(
            "create_entity_relationship",
            {
                "entity_a_id": str(entity_a.id),
                "entity_b_id": str(entity_b.id),
                "relationship_type": "unknown_type",
            },
        )
        result = json.loads(result_str)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_search_entities(self, db: AsyncSession):
        session = await _create_session(db)
        await _create_entity(db, session.id, "P-718", "centrifugal_pump", "Diesel Pump")
        await _create_entity(db, session.id, "P-818", "centrifugal_pump", "Product Pump")
        await _create_entity(db, session.id, "M-100", "motor", "Drive Motor")

        registry = ToolRegistry()
        register_entity_tools(registry, session.id, db)

        # Search all
        result = json.loads(await registry.execute_tool("search_entities", {}))
        assert result["count"] == 3

        # Search by tag
        result = json.loads(
            await registry.execute_tool("search_entities", {"tag": "718"})
        )
        assert result["count"] == 1
        assert result["entities"][0]["tag"] == "P-718"

        # Search by type
        result = json.loads(
            await registry.execute_tool("search_entities", {"entity_type": "pump"})
        )
        assert result["count"] == 2

        # Search by name
        result = json.loads(
            await registry.execute_tool("search_entities", {"name": "Motor"})
        )
        assert result["count"] == 1

    @pytest.mark.asyncio
    async def test_search_entities_session_isolation(self, db: AsyncSession):
        session1 = await _create_session(db)
        session2 = await _create_session(db)
        await _create_entity(db, session1.id, "P-718")
        await _create_entity(db, session2.id, "P-818")

        registry = ToolRegistry()
        register_entity_tools(registry, session1.id, db)

        result = json.loads(await registry.execute_tool("search_entities", {}))
        assert result["count"] == 1
        assert result["entities"][0]["tag"] == "P-718"

    @pytest.mark.asyncio
    async def test_get_entity_detail(self, db: AsyncSession):
        session = await _create_session(db)
        doc = await _create_document(db, session.id)
        entity = await _create_entity(db, session.id, "P-718")
        field = await _create_field(db, doc.id)
        field.entity_id = entity.id
        await db.flush()

        # Create a related entity and relationship
        entity2 = await _create_entity(db, session.id, "P-818", "centrifugal_pump", "Sibling Pump")
        rel = EntityRelationship(
            entity_a_id=entity.id,
            entity_b_id=entity2.id,
            relationship_type=RelationshipType.sibling,
        )
        db.add(rel)
        await db.flush()

        registry = ToolRegistry()
        register_entity_tools(registry, session.id, db)

        result_str = await registry.execute_tool(
            "get_entity_detail", {"entity_id": str(entity.id)}
        )
        result = json.loads(result_str)

        assert result["tag"] == "P-718"
        assert result["field_count"] == 1
        assert result["fields"][0]["field_name"] == "flow_nominal"
        assert len(result["relationships"]) == 1
        assert result["relationships"][0]["type"] == "sibling"
        assert result["relationships"][0]["other_entity"]["tag"] == "P-818"

    @pytest.mark.asyncio
    async def test_get_entity_detail_not_found(self, db: AsyncSession):
        session = await _create_session(db)
        registry = ToolRegistry()
        register_entity_tools(registry, session.id, db)

        result = json.loads(
            await registry.execute_tool(
                "get_entity_detail", {"entity_id": str(uuid.uuid4())}
            )
        )
        assert "error" in result


# ──────────────────── Query Tools Tests ────────────────────


class TestQueryTools:
    @pytest.mark.asyncio
    async def test_search_fields_all(self, db: AsyncSession):
        session = await _create_session(db)
        doc = await _create_document(db, session.id)
        await _create_field(db, doc.id, "flow_nominal", "335", "operating_conditions", 0.95)
        await _create_field(db, doc.id, "impeller_material", "CS", "construction_materials", 0.70)

        registry = ToolRegistry()
        register_query_tools(registry, session.id, db)

        result = json.loads(await registry.execute_tool("search_fields", {}))
        assert result["count"] == 2

    @pytest.mark.asyncio
    async def test_search_fields_by_name(self, db: AsyncSession):
        session = await _create_session(db)
        doc = await _create_document(db, session.id)
        await _create_field(db, doc.id, "flow_nominal", "335")
        await _create_field(db, doc.id, "flow_rated", "350")
        await _create_field(db, doc.id, "impeller_material", "CS")

        registry = ToolRegistry()
        register_query_tools(registry, session.id, db)

        result = json.loads(
            await registry.execute_tool("search_fields", {"field_name": "flow"})
        )
        assert result["count"] == 2

    @pytest.mark.asyncio
    async def test_search_fields_by_section(self, db: AsyncSession):
        session = await _create_session(db)
        doc = await _create_document(db, session.id)
        await _create_field(db, doc.id, "flow_nominal", "335", "operating_conditions")
        await _create_field(db, doc.id, "impeller_material", "CS", "construction_materials")

        registry = ToolRegistry()
        register_query_tools(registry, session.id, db)

        result = json.loads(
            await registry.execute_tool(
                "search_fields", {"section": "construction_materials"}
            )
        )
        assert result["count"] == 1
        assert result["fields"][0]["field_name"] == "impeller_material"

    @pytest.mark.asyncio
    async def test_search_fields_by_min_confidence(self, db: AsyncSession):
        session = await _create_session(db)
        doc = await _create_document(db, session.id)
        await _create_field(db, doc.id, "flow_nominal", "335", confidence=0.95)
        await _create_field(db, doc.id, "shaft_material", "??", confidence=0.30)

        registry = ToolRegistry()
        register_query_tools(registry, session.id, db)

        result = json.loads(
            await registry.execute_tool("search_fields", {"min_confidence": 0.8})
        )
        assert result["count"] == 1
        assert result["fields"][0]["field_name"] == "flow_nominal"

    @pytest.mark.asyncio
    async def test_search_fields_by_document(self, db: AsyncSession):
        session = await _create_session(db)
        doc1 = await _create_document(db, session.id, "pds-P718.pdf", "P-718")
        doc2 = await _create_document(db, session.id, "pds-P818.pdf", "P-818")
        await _create_field(db, doc1.id, "flow_nominal", "335")
        await _create_field(db, doc2.id, "flow_nominal", "350")

        registry = ToolRegistry()
        register_query_tools(registry, session.id, db)

        result = json.loads(
            await registry.execute_tool(
                "search_fields", {"document_id": str(doc1.id)}
            )
        )
        assert result["count"] == 1
        assert result["fields"][0]["raw_value"] == "335"

    @pytest.mark.asyncio
    async def test_search_fields_session_isolation(self, db: AsyncSession):
        session1 = await _create_session(db)
        session2 = await _create_session(db)
        doc1 = await _create_document(db, session1.id, "pds-P718.pdf")
        doc2 = await _create_document(db, session2.id, "pds-P818.pdf")
        await _create_field(db, doc1.id, "flow_nominal", "335")
        await _create_field(db, doc2.id, "flow_nominal", "350")

        registry = ToolRegistry()
        register_query_tools(registry, session1.id, db)

        result = json.loads(await registry.execute_tool("search_fields", {}))
        assert result["count"] == 1
        assert result["fields"][0]["raw_value"] == "335"

    @pytest.mark.asyncio
    async def test_search_fields_includes_document_info(self, db: AsyncSession):
        session = await _create_session(db)
        doc = await _create_document(db, session.id, "pds-P718.pdf", "P-718")
        await _create_field(db, doc.id, "flow_nominal", "335")

        registry = ToolRegistry()
        register_query_tools(registry, session.id, db)

        result = json.loads(await registry.execute_tool("search_fields", {}))
        field = result["fields"][0]
        assert field["document_filename"] == "pds-P718.pdf"
        assert field["document_pump_tag"] == "P-718"
        assert "citation_page" in field
        assert "citation_text" in field

    @pytest.mark.asyncio
    async def test_get_correction_history(self, db: AsyncSession):
        session = await _create_session(db)
        doc = await _create_document(db, session.id)
        field = await _create_field(db, doc.id, "impeller_material", "CS")

        # Create a correction
        correction = FieldCorrection(
            field_id=field.id,
            original_value="CS",
            corrected_value="SS 316",
            reason="Spec updated",
            corrected_by="user",
        )
        db.add(correction)
        await db.flush()

        registry = ToolRegistry()
        register_query_tools(registry, session.id, db)

        result = json.loads(
            await registry.execute_tool("get_correction_history", {})
        )
        assert result["count"] == 1
        c = result["corrections"][0]
        assert c["original_value"] == "CS"
        assert c["corrected_value"] == "SS 316"
        assert c["reason"] == "Spec updated"
        assert c["field_name"] == "impeller_material"

    @pytest.mark.asyncio
    async def test_get_correction_history_by_document(self, db: AsyncSession):
        session = await _create_session(db)
        doc1 = await _create_document(db, session.id, "pds-P718.pdf")
        doc2 = await _create_document(db, session.id, "pds-P818.pdf")
        field1 = await _create_field(db, doc1.id, "impeller_material", "CS")
        field2 = await _create_field(db, doc2.id, "impeller_material", "CI")

        db.add(FieldCorrection(field_id=field1.id, original_value="CS", corrected_value="SS 316"))
        db.add(FieldCorrection(field_id=field2.id, original_value="CI", corrected_value="SS 304"))
        await db.flush()

        registry = ToolRegistry()
        register_query_tools(registry, session.id, db)

        result = json.loads(
            await registry.execute_tool(
                "get_correction_history", {"document_id": str(doc1.id)}
            )
        )
        assert result["count"] == 1
        assert result["corrections"][0]["corrected_value"] == "SS 316"

    @pytest.mark.asyncio
    async def test_get_correction_history_by_field(self, db: AsyncSession):
        session = await _create_session(db)
        doc = await _create_document(db, session.id)
        field1 = await _create_field(db, doc.id, "impeller_material", "CS")
        field2 = await _create_field(db, doc.id, "shaft_material", "CS")

        db.add(FieldCorrection(field_id=field1.id, original_value="CS", corrected_value="SS 316"))
        db.add(FieldCorrection(field_id=field2.id, original_value="CS", corrected_value="SS 304"))
        await db.flush()

        registry = ToolRegistry()
        register_query_tools(registry, session.id, db)

        result = json.loads(
            await registry.execute_tool(
                "get_correction_history", {"field_id": str(field1.id)}
            )
        )
        assert result["count"] == 1
        assert result["corrections"][0]["corrected_value"] == "SS 316"

    @pytest.mark.asyncio
    async def test_get_global_correction_patterns(self, db: AsyncSession):
        session = await _create_session(db)

        # Clean up any pre-existing patterns from other tests
        existing = (await db.execute(select(CorrectionPattern))).scalars().all()
        for p in existing:
            await db.delete(p)
        await db.flush()

        # Create some patterns
        p1 = CorrectionPattern(
            description="French suction pressure",
            guidance_text="PRESSION ASPIRATION means suction pressure",
            frequency=5,
            is_active=True,
        )
        p2 = CorrectionPattern(
            description="Decimal misread",
            guidance_text="Watch for decimal points in flow values",
            frequency=3,
            is_active=True,
        )
        p3 = CorrectionPattern(
            description="Inactive pattern",
            guidance_text="This one is disabled",
            frequency=2,
            is_active=False,
        )
        db.add_all([p1, p2, p3])
        await db.flush()

        registry = ToolRegistry()
        register_query_tools(registry, session.id, db)

        result = json.loads(
            await registry.execute_tool("get_global_correction_patterns", {})
        )
        # Only active patterns returned, ordered by frequency desc
        assert result["count"] == 2
        assert result["patterns"][0]["frequency"] == 5
        assert result["patterns"][1]["frequency"] == 3

    @pytest.mark.asyncio
    async def test_get_global_correction_patterns_empty(self, db: AsyncSession):
        session = await _create_session(db)

        # Clean up any pre-existing patterns
        existing = (await db.execute(select(CorrectionPattern))).scalars().all()
        for p in existing:
            await db.delete(p)
        await db.flush()

        registry = ToolRegistry()
        register_query_tools(registry, session.id, db)

        result = json.loads(
            await registry.execute_tool("get_global_correction_patterns", {})
        )
        assert result["count"] == 0
        assert result["patterns"] == []


# ──────────────────── Factory Tests ────────────────────


class TestPhase5Factories:
    @pytest.mark.asyncio
    async def test_create_orchestrator_tools(self, db: AsyncSession):
        session = await _create_session(db)
        registry = create_orchestrator_tools(session.id, db)

        expected_tools = {
            # Document tools
            "get_session_documents",
            "get_document_info",
            "get_page_content",
            "get_document_text",
            # Extraction tools
            "save_extracted_field",
            "update_extracted_field",
            "delete_extracted_field",
            "get_extraction_progress",
            "mark_extraction_complete",
            # Entity tools
            "create_equipment_entity",
            "link_entity_to_document",
            "link_entity_to_fields",
            "create_entity_relationship",
            "search_entities",
            "get_entity_detail",
            # Query tools
            "search_fields",
            "get_correction_history",
            "get_global_correction_patterns",
            # Sub-agent tools
            "spawn_extraction_agent",
            "spawn_validation_agent",
        }
        assert set(registry.tool_names) == expected_tools
        assert len(registry) == 20

    @pytest.mark.asyncio
    async def test_create_extraction_subagent_tools(self, db: AsyncSession):
        session = await _create_session(db)
        doc = await _create_document(db, session.id)
        registry = create_extraction_subagent_tools(session.id, doc.id, db)

        expected_tools = {
            # Document tools (read-only)
            "get_session_documents",
            "get_document_info",
            "get_page_content",
            "get_document_text",
            # Extraction tools
            "save_extracted_field",
            "update_extracted_field",
            "delete_extracted_field",
            "get_extraction_progress",
            "mark_extraction_complete",
            # Entity tools
            "create_equipment_entity",
            "link_entity_to_document",
            "link_entity_to_fields",
            "create_entity_relationship",
            "search_entities",
            "get_entity_detail",
        }
        assert set(registry.tool_names) == expected_tools
        assert len(registry) == 15

        # Sub-agent tools should NOT be present
        assert "spawn_extraction_agent" not in registry.tool_names
        assert "spawn_validation_agent" not in registry.tool_names

    @pytest.mark.asyncio
    async def test_create_validation_subagent_tools(self, db: AsyncSession):
        session = await _create_session(db)
        registry = create_validation_subagent_tools(session.id, db)

        expected_tools = {
            # Query tools
            "search_fields",
            "get_correction_history",
            "get_global_correction_patterns",
            # Extraction tools (for updates)
            "save_extracted_field",
            "update_extracted_field",
            "delete_extracted_field",
            "get_extraction_progress",
            "mark_extraction_complete",
            # Entity tools
            "create_equipment_entity",
            "link_entity_to_document",
            "link_entity_to_fields",
            "create_entity_relationship",
            "search_entities",
            "get_entity_detail",
        }
        assert set(registry.tool_names) == expected_tools
        assert len(registry) == 14

        # Document and sub-agent tools should NOT be present
        assert "get_session_documents" not in registry.tool_names
        assert "spawn_extraction_agent" not in registry.tool_names

    @pytest.mark.asyncio
    async def test_all_tool_schemas_valid(self, db: AsyncSession):
        session = await _create_session(db)
        registry = create_orchestrator_tools(session.id, db)

        tools_for_llm = registry.get_tools_for_llm()
        assert len(tools_for_llm) == 20

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
