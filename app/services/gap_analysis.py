"""Extraction gap analysis.

Uses cross-document comparison instead of hardcoded expected fields.
If 3 out of 4 documents have a field but one doesn't, that's a gap.
Also detects: failed pages, low-confidence fields.
"""

import uuid
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import Document
from app.models.extracted_field import ExtractedField

LOW_CONFIDENCE_THRESHOLD = 0.7
# A field is "expected" if it appears in at least this fraction of documents
FIELD_PREVALENCE_THRESHOLD = 0.5


async def analyze_extraction_gaps(
    session_id: uuid.UUID,
    document_ids: list[uuid.UUID] | None,
    db: AsyncSession,
) -> dict:
    """Analyze extraction gaps using cross-document comparison.

    Instead of hardcoded expected fields, we look at what fields exist
    across all documents. If most documents have a field but one doesn't,
    that's a gap.
    """
    # Load documents
    doc_stmt = select(Document).where(Document.session_id == session_id)
    if document_ids:
        doc_stmt = doc_stmt.where(Document.id.in_(document_ids))
    doc_stmt = doc_stmt.order_by(Document.filename)
    docs = (await db.execute(doc_stmt)).scalars().all()

    if not docs:
        return {"documents": [], "total_fields": 0, "total_gaps": 0,
                "total_failed_pages": 0, "total_low_confidence": 0}

    # Load all fields grouped by document
    doc_fields: dict[uuid.UUID, list[ExtractedField]] = {}
    for doc in docs:
        fields_stmt = (
            select(ExtractedField)
            .where(ExtractedField.document_id == doc.id)
            .order_by(ExtractedField.section)
        )
        fields = (await db.execute(fields_stmt)).scalars().all()
        doc_fields[doc.id] = fields

    # Build cross-document field prevalence map
    # field_key = (section, normalized_field_name)
    field_presence: dict[tuple[str, str], set[uuid.UUID]] = defaultdict(set)
    for doc_id, fields in doc_fields.items():
        for f in fields:
            key = (f.section, f.field_name.lower().strip())
            field_presence[key].add(doc_id)

    # A field is "common" if it appears in > FIELD_PREVALENCE_THRESHOLD of documents
    num_docs = len(docs)
    common_fields: dict[tuple[str, str], int] = {}
    for key, present_in in field_presence.items():
        if len(present_in) / num_docs >= FIELD_PREVALENCE_THRESHOLD:
            common_fields[key] = len(present_in)

    report = {
        "documents": [],
        "total_fields": 0,
        "total_gaps": 0,
        "total_failed_pages": 0,
        "total_low_confidence": 0,
        "common_fields_count": len(common_fields),
    }

    for doc in docs:
        fields = doc_fields[doc.id]
        doc_field_keys = {(f.section, f.field_name.lower().strip()) for f in fields}

        doc_report = {
            "document_id": str(doc.id),
            "filename": doc.filename,
            "pump_tag": doc.pump_tag,
            "format_type": doc.format_type,
            "status": doc.status.value,
            "failed_pages": [],
            "missing_fields": [],
            "low_confidence_fields": [],
            "field_count": len(fields),
        }
        report["total_fields"] += len(fields)

        # Check for pages with 0 fields extracted
        all_citation_pages = {f.citation_page for f in fields}
        for page_num in range(1, (doc.num_pages or 1) + 1):
            if page_num not in all_citation_pages:
                doc_report["failed_pages"].append({
                    "page_number": page_num,
                    "reason": "No fields extracted from this page",
                })
                report["total_failed_pages"] += 1

        # Check for missing common fields (cross-document comparison)
        if num_docs > 1:
            for (section, field_name), prevalence in common_fields.items():
                if (section, field_name) not in doc_field_keys:
                    doc_report["missing_fields"].append({
                        "field_name": field_name,
                        "section": section,
                        "present_in": f"{prevalence}/{num_docs} other documents",
                    })
                    report["total_gaps"] += 1

        # Check for low-confidence fields
        for f in fields:
            if f.confidence < LOW_CONFIDENCE_THRESHOLD:
                doc_report["low_confidence_fields"].append({
                    "field_id": str(f.id),
                    "field_name": f.field_name,
                    "display_name": f.display_name,
                    "raw_value": f.raw_value,
                    "unit": f.unit,
                    "confidence": f.confidence,
                    "citation_page": f.citation_page,
                    "section": f.section,
                })
                report["total_low_confidence"] += 1

        report["documents"].append(doc_report)

    return report


def format_extraction_report(report: dict) -> str:
    """Format a gap analysis report as human-readable text."""
    lines = ["## Extraction Report\n"]

    total = report["total_fields"]
    failed = report["total_failed_pages"]
    gaps = report["total_gaps"]
    low_conf = report["total_low_confidence"]

    lines.append(f"**Total fields extracted:** {total}")

    if failed == 0 and gaps == 0 and low_conf == 0:
        lines.append("\nAll pages extracted successfully with no missing fields or low-confidence values.")
    else:
        if failed > 0:
            lines.append(f"**Failed pages:** {failed}")
        if gaps > 0:
            lines.append(f"**Missing fields (cross-document comparison):** {gaps}")
        if low_conf > 0:
            lines.append(f"**Low-confidence fields (<70%):** {low_conf}")

    for doc in report["documents"]:
        lines.append(f"\n---\n### {doc['filename']} ({doc['pump_tag'] or 'no tag'})")
        lines.append(f"Fields: {doc['field_count']}")

        if doc["failed_pages"]:
            lines.append("\n**Failed Pages:**")
            for fp in doc["failed_pages"]:
                lines.append(f"  - Page {fp['page_number']}: {fp['reason']}")

        if doc["missing_fields"]:
            lines.append("\n**Missing Fields** (present in other documents but not here):")
            by_section: dict[str, list[str]] = {}
            for mf in doc["missing_fields"]:
                detail = f"{mf['field_name']} ({mf['present_in']})"
                by_section.setdefault(mf["section"], []).append(detail)
            for section, names in by_section.items():
                lines.append(f"  - [{section}]: {', '.join(names)}")

        if doc["low_confidence_fields"]:
            lines.append("\n**Low-Confidence Fields** (need human review):")
            for lf in doc["low_confidence_fields"]:
                unit = f" {lf['unit']}" if lf.get("unit") else ""
                lines.append(
                    f"  - {lf['display_name']}: {lf['raw_value']}{unit} "
                    f"(confidence: {lf['confidence']:.0%}, page {lf['citation_page']})"
                )

        if not doc["failed_pages"] and not doc["missing_fields"] and not doc["low_confidence_fields"]:
            lines.append("No issues found.")

    if failed > 0 or gaps > 0 or low_conf > 0:
        lines.append("\n---\n**What would you like to do?** I can help you:")
        if failed > 0:
            lines.append("- Re-extract failed pages")
        if gaps > 0:
            lines.append("- Search for the missing fields or manually provide values")
        if low_conf > 0:
            lines.append("- Review and verify/correct low-confidence fields")

    return "\n".join(lines)
