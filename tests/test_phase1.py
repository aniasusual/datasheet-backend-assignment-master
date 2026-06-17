"""
Phase 1 smoke tests — verifies database models, relationships, and migrations.

Run with: pytest tests/test_phase1.py -v
Requires: PostgreSQL running (docker compose up -d)
"""

import uuid

import pytest
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Base,
    CorrectionPattern,
    CostRecord,
    Document,
    DocumentPage,
    DocumentStatus,
    EntityRelationship,
    EquipmentEntity,
    ExtractedField,
    FieldCorrection,
    FieldDataType,
    FieldStatus,
    Message,
    MessageRole,
    RelationshipType,
    Session,
    SessionStatus,
    entity_documents,
)


# ── Tests ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_table_count():
    """All 11 tables are registered in metadata."""
    assert len(Base.metadata.tables) == 11


@pytest.mark.asyncio
async def test_create_session(db: AsyncSession):
    """Can create a Session and read it back."""
    s = Session(id=uuid.uuid4(), status=SessionStatus.active)
    db.add(s)
    await db.commit()

    result = await db.get(Session, s.id)
    assert result is not None
    assert result.status == SessionStatus.active
    assert result.head_ptr == 0
    assert result.compact_summary is None
    assert result.created_at is not None
    assert result.updated_at is not None


@pytest.mark.asyncio
async def test_create_message_linked_to_session(db: AsyncSession):
    """Messages belong to sessions via FK."""
    s = Session(id=uuid.uuid4())
    db.add(s)
    await db.flush()

    msg = Message(
        id=uuid.uuid4(),
        session_id=s.id,
        seq_number=1,
        role=MessageRole.user,
        content="Extract all fields",
    )
    db.add(msg)
    await db.commit()

    result = await db.get(Message, msg.id)
    assert result.session_id == s.id
    assert result.role == MessageRole.user
    assert result.content == "Extract all fields"
    assert result.is_compacted is False
    assert result.tool_calls is None


@pytest.mark.asyncio
async def test_create_document_with_pages(db: AsyncSession):
    """Document → DocumentPage relationship works."""
    s = Session(id=uuid.uuid4())
    db.add(s)
    await db.flush()

    doc = Document(
        id=uuid.uuid4(),
        session_id=s.id,
        filename="pds-P718.pdf",
        file_path="/uploads/pds-P718.pdf",
        pump_tag="P-718",
        format_type="french_form",
        status=DocumentStatus.uploaded,
        num_pages=3,
    )
    db.add(doc)
    await db.flush()

    page = DocumentPage(
        id=uuid.uuid4(),
        document_id=doc.id,
        page_number=1,
        raw_text="POMPE CENTRIFUGE P-718",
        layout_text="POMPE    CENTRIFUGE    P-718",
        tables_json=[["Flow", "335", "m³/h"]],
        image_path="/rendered/p718_page1.png",
        width=612.0,
        height=792.0,
    )
    db.add(page)
    await db.commit()

    result = await db.get(Document, doc.id)
    assert result.pump_tag == "P-718"
    assert result.num_pages == 3
    assert result.status == DocumentStatus.uploaded

    page_result = await db.get(DocumentPage, page.id)
    assert page_result.document_id == doc.id
    assert page_result.page_number == 1
    assert page_result.raw_text == "POMPE CENTRIFUGE P-718"
    assert page_result.tables_json == [["Flow", "335", "m³/h"]]


@pytest.mark.asyncio
async def test_extracted_field_with_citation(db: AsyncSession):
    """ExtractedField stores value + citation metadata."""
    s = Session(id=uuid.uuid4())
    db.add(s)
    await db.flush()

    doc = Document(
        id=uuid.uuid4(), session_id=s.id, filename="test.pdf", file_path="/test.pdf"
    )
    db.add(doc)
    await db.flush()

    field = ExtractedField(
        id=uuid.uuid4(),
        document_id=doc.id,
        field_name="flow_nominal",
        display_name="Nominal Flow",
        raw_value="335",
        unit="m³/h",
        data_type=FieldDataType.numeric,
        section="operating_conditions",
        confidence=0.95,
        status=FieldStatus.extracted,
        citation_page=1,
        citation_bbox={"x0": 100, "y0": 200, "x1": 300, "y1": 220},
        citation_text="Débit nominal: 335 m³/h",
    )
    db.add(field)
    await db.commit()

    result = await db.get(ExtractedField, field.id)
    assert result.field_name == "flow_nominal"
    assert result.raw_value == "335"
    assert result.unit == "m³/h"
    assert result.confidence == 0.95
    assert result.citation_page == 1
    assert result.citation_bbox["x0"] == 100
    assert result.citation_text == "Débit nominal: 335 m³/h"
    assert result.status == FieldStatus.extracted


@pytest.mark.asyncio
async def test_field_correction_preserves_original(db: AsyncSession):
    """Corrections create a separate record; original is preserved."""
    s = Session(id=uuid.uuid4())
    db.add(s)
    await db.flush()

    doc = Document(
        id=uuid.uuid4(), session_id=s.id, filename="test.pdf", file_path="/test.pdf"
    )
    db.add(doc)
    await db.flush()

    field = ExtractedField(
        id=uuid.uuid4(),
        document_id=doc.id,
        field_name="impeller_material",
        display_name="Impeller Material",
        raw_value="CS",
        data_type=FieldDataType.text,
        section="construction_materials",
        confidence=0.92,
        citation_page=2,
        citation_text="Impeller: CS",
    )
    db.add(field)
    await db.flush()

    correction = FieldCorrection(
        id=uuid.uuid4(),
        field_id=field.id,
        original_value="CS",
        corrected_value="SS 316",
        reason="Spec updated last month",
        corrected_by="user",
    )
    db.add(correction)

    # Update field status
    field.raw_value = "SS 316"
    field.status = FieldStatus.corrected
    await db.commit()

    # Verify correction record
    corr = await db.get(FieldCorrection, correction.id)
    assert corr.original_value == "CS"
    assert corr.corrected_value == "SS 316"
    assert corr.reason == "Spec updated last month"

    # Verify field was updated
    f = await db.get(ExtractedField, field.id)
    assert f.raw_value == "SS 316"
    assert f.status == FieldStatus.corrected


@pytest.mark.asyncio
async def test_equipment_entity_with_relationships(db: AsyncSession):
    """Entities link to documents and to each other."""
    s = Session(id=uuid.uuid4())
    db.add(s)
    await db.flush()

    doc1 = Document(
        id=uuid.uuid4(), session_id=s.id, filename="pds-P718.pdf", file_path="/p718.pdf"
    )
    doc2 = Document(
        id=uuid.uuid4(), session_id=s.id, filename="pds-P818.pdf", file_path="/p818.pdf"
    )
    db.add_all([doc1, doc2])
    await db.flush()

    entity1 = EquipmentEntity(
        id=uuid.uuid4(),
        session_id=s.id,
        tag="P-718",
        entity_type="centrifugal_pump",
        name="Diesel Product Pump",
    )
    entity2 = EquipmentEntity(
        id=uuid.uuid4(),
        session_id=s.id,
        tag="P-818",
        entity_type="centrifugal_pump",
        name="Diesel Product Pump (Spare)",
    )
    db.add_all([entity1, entity2])
    await db.flush()

    # Link entities to documents
    await db.execute(
        entity_documents.insert().values(entity_id=entity1.id, document_id=doc1.id)
    )
    await db.execute(
        entity_documents.insert().values(entity_id=entity2.id, document_id=doc2.id)
    )

    # Create sibling relationship
    rel = EntityRelationship(
        id=uuid.uuid4(),
        entity_a_id=entity1.id,
        entity_b_id=entity2.id,
        relationship_type=RelationshipType.sibling,
        metadata_json={"unit": "Hydrocracking Unit 032"},
    )
    db.add(rel)
    await db.commit()

    # Verify relationship
    r = await db.get(EntityRelationship, rel.id)
    assert r.entity_a_id == entity1.id
    assert r.entity_b_id == entity2.id
    assert r.relationship_type == RelationshipType.sibling
    assert r.metadata_json["unit"] == "Hydrocracking Unit 032"

    # Verify entity-document link
    stmt = select(entity_documents).where(entity_documents.c.entity_id == entity1.id)
    rows = (await db.execute(stmt)).fetchall()
    assert len(rows) == 1
    assert rows[0].document_id == doc1.id


@pytest.mark.asyncio
async def test_correction_pattern(db: AsyncSession):
    """Global correction patterns are stored and queryable."""
    pattern = CorrectionPattern(
        id=uuid.uuid4(),
        description="PRESSION ASPIRATION means suction pressure in French-form datasheets",
        guidance_text="In French-form datasheets, PRESSION ASPIRATION means suction pressure, not discharge pressure. Extract as suction_pressure.",
        frequency=5,
        is_active=True,
    )
    db.add(pattern)
    await db.commit()

    stmt = select(CorrectionPattern).where(CorrectionPattern.is_active == True)
    results = (await db.execute(stmt)).scalars().all()
    assert len(results) >= 1
    found = [p for p in results if p.id == pattern.id][0]
    assert found.frequency == 5
    assert "suction pressure" in found.guidance_text


@pytest.mark.asyncio
async def test_cost_record(db: AsyncSession):
    """Cost records track LLM usage per session."""
    s = Session(id=uuid.uuid4())
    db.add(s)
    await db.flush()

    record = CostRecord(
        id=uuid.uuid4(),
        session_id=s.id,
        operation="extraction",
        model="claude-sonnet-4-20250514",
        input_tokens=4500,
        output_tokens=1200,
        cost_usd=0.042,
        duration_sec=3.7,
    )
    db.add(record)
    await db.commit()

    # Query total cost for session
    stmt = select(func.sum(CostRecord.cost_usd)).where(CostRecord.session_id == s.id)
    total = (await db.execute(stmt)).scalar()
    assert total == pytest.approx(0.042)


@pytest.mark.asyncio
async def test_session_message_ordering(db: AsyncSession):
    """Messages are ordered by seq_number within a session."""
    s = Session(id=uuid.uuid4())
    db.add(s)
    await db.flush()

    messages = [
        Message(id=uuid.uuid4(), session_id=s.id, seq_number=i, role=role, content=content)
        for i, (role, content) in enumerate(
            [
                (MessageRole.user, "Extract all fields"),
                (MessageRole.assistant, "I'll start extraction now."),
                (MessageRole.system, "Budget warning: 80% used"),
            ],
            start=1,
        )
    ]
    db.add_all(messages)
    await db.commit()

    stmt = (
        select(Message)
        .where(Message.session_id == s.id)
        .order_by(Message.seq_number)
    )
    results = (await db.execute(stmt)).scalars().all()
    assert len(results) == 3
    assert results[0].role == MessageRole.user
    assert results[1].role == MessageRole.assistant
    assert results[2].role == MessageRole.system
    assert results[0].seq_number < results[1].seq_number < results[2].seq_number


@pytest.mark.asyncio
async def test_message_tool_call_jsonb(db: AsyncSession):
    """Tool calls and results are stored as JSONB."""
    s = Session(id=uuid.uuid4())
    db.add(s)
    await db.flush()

    tool_call_data = [
        {
            "id": "call_abc123",
            "type": "function",
            "function": {
                "name": "get_session_documents",
                "arguments": '{"session_id": "xxx"}',
            },
        }
    ]

    msg = Message(
        id=uuid.uuid4(),
        session_id=s.id,
        seq_number=1,
        role=MessageRole.assistant,
        content=None,
        tool_calls=tool_call_data,
    )
    db.add(msg)
    await db.flush()

    tool_result_msg = Message(
        id=uuid.uuid4(),
        session_id=s.id,
        seq_number=2,
        role=MessageRole.tool,
        tool_call_id="call_abc123",
        content='[{"id": "doc1", "filename": "pds-P718.pdf"}]',
    )
    db.add(tool_result_msg)
    await db.commit()

    result = await db.get(Message, msg.id)
    assert result.tool_calls[0]["function"]["name"] == "get_session_documents"

    tool_msg = await db.get(Message, tool_result_msg.id)
    assert tool_msg.tool_call_id == "call_abc123"
    assert tool_msg.role == MessageRole.tool


@pytest.mark.asyncio
async def test_field_linked_to_entity(db: AsyncSession):
    """Fields can optionally link to an equipment entity."""
    s = Session(id=uuid.uuid4())
    db.add(s)
    await db.flush()

    doc = Document(
        id=uuid.uuid4(), session_id=s.id, filename="test.pdf", file_path="/t.pdf"
    )
    entity = EquipmentEntity(
        id=uuid.uuid4(),
        session_id=s.id,
        tag="P-718",
        entity_type="centrifugal_pump",
        name="Test Pump",
    )
    db.add_all([doc, entity])
    await db.flush()

    field = ExtractedField(
        id=uuid.uuid4(),
        document_id=doc.id,
        entity_id=entity.id,
        field_name="flow_nominal",
        display_name="Nominal Flow",
        raw_value="335",
        unit="m³/h",
        data_type=FieldDataType.numeric,
        section="operating_conditions",
        confidence=0.95,
        citation_page=1,
        citation_text="335 m³/h",
    )
    db.add(field)
    await db.commit()

    result = await db.get(ExtractedField, field.id)
    assert result.entity_id == entity.id


@pytest.mark.asyncio
async def test_compaction_flag(db: AsyncSession):
    """Messages can be marked as compacted."""
    s = Session(id=uuid.uuid4(), head_ptr=0, compact_summary=None)
    db.add(s)
    await db.flush()

    msg1 = Message(id=uuid.uuid4(), session_id=s.id, seq_number=1, role=MessageRole.user, content="old msg")
    msg2 = Message(id=uuid.uuid4(), session_id=s.id, seq_number=2, role=MessageRole.assistant, content="old reply")
    msg3 = Message(id=uuid.uuid4(), session_id=s.id, seq_number=3, role=MessageRole.user, content="new msg")
    db.add_all([msg1, msg2, msg3])
    await db.flush()

    # Simulate compaction: mark old messages, advance head_ptr, set summary
    msg1.is_compacted = True
    msg2.is_compacted = True
    s.head_ptr = 3
    s.compact_summary = "User uploaded docs. Agent extracted 28 fields from P-718."
    await db.commit()

    # Query active (non-compacted) messages
    stmt = (
        select(Message)
        .where(Message.session_id == s.id, Message.seq_number >= s.head_ptr, Message.is_compacted == False)
        .order_by(Message.seq_number)
    )
    active = (await db.execute(stmt)).scalars().all()
    assert len(active) == 1
    assert active[0].content == "new msg"

    session = await db.get(Session, s.id)
    assert session.compact_summary == "User uploaded docs. Agent extracted 28 fields from P-718."
    assert session.head_ptr == 3
