"""Phase 8 tests: Read-only data endpoints for fields, entities, messages, costs, corrections."""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import app
from app.models.correction_pattern import CorrectionPattern
from app.models.cost_record import CostRecord
from app.models.document import Document, DocumentStatus
from app.models.entity_relationship import EntityRelationship, RelationshipType
from app.models.equipment_entity import EquipmentEntity
from app.models.extracted_field import ExtractedField, FieldDataType, FieldStatus
from app.models.field_correction import FieldCorrection
from app.models.message import Message, MessageRole
from app.models.entity_document import entity_documents
from app.models.session import Session, SessionStatus


# ──────────────────── Fixtures ────────────────────


async def _create_full_session(db: AsyncSession) -> dict:
    """Create a session with documents, fields, entities, messages, corrections, costs."""
    session = Session(status=SessionStatus.active)
    db.add(session)
    await db.flush()

    # Documents
    doc1 = Document(
        session_id=session.id, filename="pds-p718.pdf",
        file_path="/tmp/p718.pdf", pump_tag="P-718",
        format_type="english_tabular", status=DocumentStatus.extracted, num_pages=3,
    )
    doc2 = Document(
        session_id=session.id, filename="pds-p818.pdf",
        file_path="/tmp/p818.pdf", pump_tag="P-818",
        format_type="english_tabular", status=DocumentStatus.extracted, num_pages=3,
    )
    db.add_all([doc1, doc2])
    await db.flush()

    # Entities
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

    # Link entities to documents via association table (avoid lazy-load)
    await db.execute(entity_documents.insert().values(entity_id=entity1.id, document_id=doc1.id))
    await db.execute(entity_documents.insert().values(entity_id=entity2.id, document_id=doc2.id))
    await db.flush()

    # Entity relationship
    rel = EntityRelationship(
        entity_a_id=entity1.id, entity_b_id=entity2.id,
        relationship_type=RelationshipType.sibling,
    )
    db.add(rel)
    await db.flush()

    # Fields for doc1
    fields1 = []
    for i, (name, section, conf) in enumerate([
        ("flow_nominal", "operating_conditions", 0.95),
        ("pressure_suction", "pressure_conditions", 0.88),
        ("impeller_material", "construction_materials", 0.72),
        ("motor_power", "motor_data", 0.45),
    ]):
        f = ExtractedField(
            document_id=doc1.id, entity_id=entity1.id,
            field_name=name, display_name=name.replace("_", " ").title(),
            raw_value=f"value_{i}", unit="m³/h" if "flow" in name else None,
            data_type=FieldDataType.numeric if "flow" in name else FieldDataType.text,
            section=section, confidence=conf,
            status=FieldStatus.extracted,
            citation_page=1, citation_text=f"source text {i}",
        )
        fields1.append(f)
    db.add_all(fields1)
    await db.flush()

    # Fields for doc2
    f_doc2 = ExtractedField(
        document_id=doc2.id, entity_id=entity2.id,
        field_name="flow_nominal", display_name="Flow Nominal",
        raw_value="400", unit="m³/h",
        data_type=FieldDataType.numeric,
        section="operating_conditions", confidence=0.9,
        status=FieldStatus.extracted,
        citation_page=1, citation_text="400 m³/h",
    )
    db.add(f_doc2)
    await db.flush()

    # Correction on impeller_material
    correction = FieldCorrection(
        field_id=fields1[2].id,
        original_value="CS",
        corrected_value="SS 316",
        reason="Spec updated last month",
        corrected_by="user",
    )
    db.add(correction)
    fields1[2].raw_value = "SS 316"
    fields1[2].status = FieldStatus.corrected
    await db.flush()

    # Messages
    for i in range(8):
        role = MessageRole.user if i % 2 == 0 else MessageRole.assistant
        msg = Message(
            session_id=session.id, seq_number=i, role=role,
            content=f"Message {i}",
            is_compacted=(i < 2),
        )
        db.add(msg)
    await db.flush()

    # Cost records
    for op, cost, doc_id in [
        ("extraction", 0.20, doc1.id),
        ("extraction", 0.18, doc2.id),
        ("validation", 0.05, None),
        ("query", 0.01, None),
    ]:
        cr = CostRecord(
            session_id=session.id, document_id=doc_id,
            operation=op, model="gemini/gemini-2.0-flash",
            input_tokens=2000, output_tokens=500,
            cost_usd=cost, duration_sec=2.0,
        )
        db.add(cr)
    await db.flush()

    # Global correction pattern
    pattern = CorrectionPattern(
        description="French suction pressure label",
        guidance_text="In French-form datasheets, PRESSION ASPIRATION means suction pressure.",
        frequency=5, is_active=True,
    )
    db.add(pattern)
    await db.flush()
    await db.commit()

    return {
        "session_id": session.id,
        "doc1_id": doc1.id,
        "doc2_id": doc2.id,
        "entity1_id": entity1.id,
        "entity2_id": entity2.id,
        "field_ids": [f.id for f in fields1],
        "correction_id": correction.id,
    }


# ──────────────────── Field Endpoints ────────────────────


class TestFieldEndpoints:
    @pytest.mark.asyncio
    async def test_list_fields(self, db: AsyncSession):
        data = await _create_full_session(db)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/api/v1/sessions/{data['session_id']}/fields")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 5  # 4 from doc1 + 1 from doc2
        assert len(body["fields"]) == 5

    @pytest.mark.asyncio
    async def test_list_fields_filter_by_section(self, db: AsyncSession):
        data = await _create_full_session(db)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                f"/api/v1/sessions/{data['session_id']}/fields",
                params={"section": "operating_conditions"},
            )

        body = resp.json()
        assert body["total"] == 2  # flow_nominal from both docs

    @pytest.mark.asyncio
    async def test_list_fields_filter_by_document(self, db: AsyncSession):
        data = await _create_full_session(db)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                f"/api/v1/sessions/{data['session_id']}/fields",
                params={"document_id": str(data["doc1_id"])},
            )

        body = resp.json()
        assert body["total"] == 4

    @pytest.mark.asyncio
    async def test_list_fields_filter_by_min_confidence(self, db: AsyncSession):
        data = await _create_full_session(db)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                f"/api/v1/sessions/{data['session_id']}/fields",
                params={"min_confidence": 0.8},
            )

        body = resp.json()
        # flow_nominal(0.95), pressure_suction(0.88), flow_nominal_doc2(0.9) = 3
        assert body["total"] == 3

    @pytest.mark.asyncio
    async def test_list_fields_filter_by_status(self, db: AsyncSession):
        data = await _create_full_session(db)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                f"/api/v1/sessions/{data['session_id']}/fields",
                params={"status": "corrected"},
            )

        body = resp.json()
        assert body["total"] == 1
        assert body["fields"][0]["field_name"] == "impeller_material"

    @pytest.mark.asyncio
    async def test_list_fields_filter_by_name(self, db: AsyncSession):
        data = await _create_full_session(db)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                f"/api/v1/sessions/{data['session_id']}/fields",
                params={"field_name": "flow"},
            )

        body = resp.json()
        assert body["total"] == 2

    @pytest.mark.asyncio
    async def test_list_fields_pagination(self, db: AsyncSession):
        data = await _create_full_session(db)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                f"/api/v1/sessions/{data['session_id']}/fields",
                params={"limit": 2, "offset": 0},
            )

        body = resp.json()
        assert body["total"] == 5
        assert len(body["fields"]) == 2
        assert body["offset"] == 0
        assert body["limit"] == 2

    @pytest.mark.asyncio
    async def test_field_detail_with_corrections(self, db: AsyncSession):
        data = await _create_full_session(db)
        field_id = data["field_ids"][2]  # impeller_material (corrected)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                f"/api/v1/sessions/{data['session_id']}/fields/{field_id}"
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["field_name"] == "impeller_material"
        assert body["raw_value"] == "SS 316"
        assert body["status"] == "corrected"
        assert len(body["corrections"]) == 1
        assert body["corrections"][0]["original_value"] == "CS"
        assert body["corrections"][0]["corrected_value"] == "SS 316"

    @pytest.mark.asyncio
    async def test_field_detail_not_found(self, db: AsyncSession):
        data = await _create_full_session(db)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                f"/api/v1/sessions/{data['session_id']}/fields/{uuid.uuid4()}"
            )

        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_field_stats(self, db: AsyncSession):
        data = await _create_full_session(db)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                f"/api/v1/sessions/{data['session_id']}/fields/stats"
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["total_fields"] == 5
        assert "operating_conditions" in body["by_section"]
        assert body["by_section"]["operating_conditions"] == 2
        assert "extracted" in body["by_status"]
        assert "high" in body["by_confidence_tier"]
        assert len(body["per_document"]) == 2

    @pytest.mark.asyncio
    async def test_fields_session_not_found(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/api/v1/sessions/{uuid.uuid4()}/fields")
        assert resp.status_code == 404


# ──────────────────── Entity Endpoints ────────────────────


class TestEntityEndpoints:
    @pytest.mark.asyncio
    async def test_list_entities(self, db: AsyncSession):
        data = await _create_full_session(db)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/api/v1/sessions/{data['session_id']}/entities")

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["entities"]) == 2
        tags = {e["tag"] for e in body["entities"]}
        assert tags == {"P-718", "P-818"}
        # P-718 has 4 fields, P-818 has 1
        e718 = next(e for e in body["entities"] if e["tag"] == "P-718")
        assert e718["field_count"] == 4
        assert e718["document_count"] == 1

    @pytest.mark.asyncio
    async def test_entity_detail(self, db: AsyncSession):
        data = await _create_full_session(db)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                f"/api/v1/sessions/{data['session_id']}/entities/{data['entity1_id']}"
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["tag"] == "P-718"
        assert len(body["documents"]) == 1
        assert body["documents"][0]["filename"] == "pds-p718.pdf"
        assert len(body["fields"]) == 4
        assert len(body["relationships"]) == 1
        assert body["relationships"][0]["relationship_type"] == "sibling"
        assert body["relationships"][0]["related_entity"]["tag"] == "P-818"

    @pytest.mark.asyncio
    async def test_entity_not_found(self, db: AsyncSession):
        data = await _create_full_session(db)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                f"/api/v1/sessions/{data['session_id']}/entities/{uuid.uuid4()}"
            )
        assert resp.status_code == 404


# ──────────────────── Message Endpoints ────────────────────


class TestMessageEndpoints:
    @pytest.mark.asyncio
    async def test_list_messages(self, db: AsyncSession):
        data = await _create_full_session(db)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/api/v1/sessions/{data['session_id']}/messages")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 8
        assert len(body["messages"]) == 8
        # Ordered by seq_number
        assert body["messages"][0]["seq_number"] == 0
        assert body["messages"][7]["seq_number"] == 7

    @pytest.mark.asyncio
    async def test_compacted_messages_hide_content(self, db: AsyncSession):
        data = await _create_full_session(db)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/api/v1/sessions/{data['session_id']}/messages")

        body = resp.json()
        # First 2 messages are compacted
        assert body["messages"][0]["is_compacted"] is True
        assert body["messages"][0]["content"] is None
        # Later messages have content
        assert body["messages"][2]["is_compacted"] is False
        assert body["messages"][2]["content"] is not None

    @pytest.mark.asyncio
    async def test_messages_pagination(self, db: AsyncSession):
        data = await _create_full_session(db)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                f"/api/v1/sessions/{data['session_id']}/messages",
                params={"limit": 3, "offset": 2},
            )

        body = resp.json()
        assert body["total"] == 8
        assert len(body["messages"]) == 3
        assert body["messages"][0]["seq_number"] == 2

    @pytest.mark.asyncio
    async def test_messages_session_not_found(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/api/v1/sessions/{uuid.uuid4()}/messages")
        assert resp.status_code == 404


# ──────────────────── Cost Endpoints ────────────────────


class TestCostEndpoints:
    @pytest.mark.asyncio
    async def test_cost_breakdown(self, db: AsyncSession):
        data = await _create_full_session(db)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/api/v1/sessions/{data['session_id']}/costs")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total_cost_usd"] == pytest.approx(0.44, abs=0.01)
        assert body["total_calls"] == 4
        assert body["total_input_tokens"] == 8000
        assert body["total_output_tokens"] == 2000

        # By operation
        assert "extraction" in body["by_operation"]
        assert body["by_operation"]["extraction"]["call_count"] == 2
        assert "validation" in body["by_operation"]
        assert "query" in body["by_operation"]

        # Per document
        assert len(body["per_document"]) == 2

        # By model
        assert "gemini/gemini-2.0-flash" in body["by_model"]

    @pytest.mark.asyncio
    async def test_cost_empty_session(self, db: AsyncSession):
        session = Session(status=SessionStatus.active)
        db.add(session)
        await db.flush()
        await db.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/api/v1/sessions/{session.id}/costs")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total_cost_usd"] == 0.0
        assert body["total_calls"] == 0

    @pytest.mark.asyncio
    async def test_cost_session_not_found(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/api/v1/sessions/{uuid.uuid4()}/costs")
        assert resp.status_code == 404


# ──────────────────── Correction Endpoints ────────────────────


class TestCorrectionEndpoints:
    @pytest.mark.asyncio
    async def test_list_corrections(self, db: AsyncSession):
        data = await _create_full_session(db)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/api/v1/sessions/{data['session_id']}/corrections")

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["corrections"]) == 1
        c = body["corrections"][0]
        assert c["field_name"] == "impeller_material"
        assert c["original_value"] == "CS"
        assert c["corrected_value"] == "SS 316"
        assert c["reason"] == "Spec updated last month"
        assert c["document_filename"] == "pds-p718.pdf"

    @pytest.mark.asyncio
    async def test_list_corrections_empty(self, db: AsyncSession):
        session = Session(status=SessionStatus.active)
        db.add(session)
        await db.flush()
        await db.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/api/v1/sessions/{session.id}/corrections")

        assert resp.status_code == 200
        assert resp.json()["corrections"] == []

    @pytest.mark.asyncio
    async def test_global_correction_patterns(self, db: AsyncSession):
        await _create_full_session(db)  # creates a pattern
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/corrections/patterns")

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["patterns"]) >= 1
        # Find our pattern (other tests may have created patterns too)
        our_pattern = next(
            (p for p in body["patterns"] if p["description"] == "French suction pressure label"),
            None,
        )
        assert our_pattern is not None
        assert our_pattern["frequency"] == 5
        assert our_pattern["is_active"] is True
