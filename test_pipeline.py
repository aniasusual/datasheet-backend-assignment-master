"""End-to-end test of the extraction pipeline against actual PDFs."""

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


async def main():
    from app.database import async_session_factory
    from app.models.session import Session, SessionStatus
    from app.models.document import Document, DocumentStatus
    from app.models.extracted_field import ExtractedField
    from app.models.document_page import DocumentPage
    from app.services.document_processor import process_document
    from app.services.extraction import extract_document
    from app.services.post_processing import post_process_document
    from app.config import settings
    from sqlalchemy import select, func

    pdfs = sorted(Path(".").glob("pds-*.pdf"))
    print(f"\nFound {len(pdfs)} PDFs: {[p.name for p in pdfs]}\n")

    async with async_session_factory() as db:
        # 1. Create session
        session = Session(status=SessionStatus.active, title="Full Pipeline Test")
        db.add(session)
        await db.flush()
        await db.refresh(session)
        print(f"Session: {session.id}\n")

        # 2. Ingest all PDFs
        print("=" * 60)
        print("STEP 1: INGESTION")
        print("=" * 60)

        docs = []
        for pdf_path in pdfs:
            upload_dir = settings.UPLOAD_DIR / str(session.id)
            upload_dir.mkdir(parents=True, exist_ok=True)
            dest = upload_dir / pdf_path.name
            dest.write_bytes(pdf_path.read_bytes())

            doc = Document(
                session_id=session.id,
                filename=pdf_path.name,
                file_path=str(dest),
                status=DocumentStatus.uploading,
            )
            db.add(doc)
            await db.flush()

            t0 = time.time()
            doc = await process_document(dest, doc.id, db)
            elapsed = time.time() - t0

            pages_q = select(DocumentPage).where(DocumentPage.document_id == doc.id).order_by(DocumentPage.page_number)
            pages = (await db.execute(pages_q)).scalars().all()

            content_pages = sum(1 for p in pages if p.page_type == "content")
            print(f"  {doc.filename}: tag={doc.pump_tag}, format={doc.format_type}, "
                  f"{doc.num_pages} pages ({content_pages} content), {elapsed:.1f}s")

            docs.append(doc)

        await db.commit()

        # 3. Extract all documents
        print(f"\n{'=' * 60}")
        print("STEP 2: EXTRACTION")
        print("=" * 60)

        total_fields = 0
        total_time = 0

        for doc in docs:
            print(f"\n  Extracting {doc.filename}...")
            t0 = time.time()
            fields = await extract_document(doc.id, db)
            elapsed = time.time() - t0
            total_fields += len(fields)
            total_time += elapsed

            # Group by section
            sections = {}
            for f in fields:
                sections.setdefault(f.section, []).append(f)

            print(f"  → {len(fields)} fields in {elapsed:.1f}s")
            for section, sf in sorted(sections.items()):
                print(f"    [{section}]: {len(sf)} fields")

        await db.commit()

        # 4. Post-process all
        print(f"\n{'=' * 60}")
        print("STEP 3: POST-PROCESSING")
        print("=" * 60)

        for doc in docs:
            t0 = time.time()
            entity = await post_process_document(doc.id, session.id, db)
            elapsed = time.time() - t0

            if entity:
                footnotes = (entity.metadata_json or {}).get("footnotes", {})
                print(f"  {doc.filename}: entity={entity.tag} ({entity.name}), "
                      f"{len(footnotes)} footnotes resolved, {elapsed:.1f}s")
            else:
                print(f"  {doc.filename}: no entity created, {elapsed:.1f}s")

        await db.commit()

        # 5. Final summary
        print(f"\n{'=' * 60}")
        print("FINAL SUMMARY")
        print("=" * 60)

        for doc in docs:
            field_count = (await db.execute(
                select(func.count(ExtractedField.id)).where(ExtractedField.document_id == doc.id)
            )).scalar()

            # Refresh to get latest status
            await db.refresh(doc)

            print(f"\n  {doc.filename} (tag: {doc.pump_tag})")
            print(f"    Status: {doc.status}")
            print(f"    Fields: {field_count}")

            # Show sample fields
            sample_q = (
                select(ExtractedField)
                .where(ExtractedField.document_id == doc.id)
                .order_by(ExtractedField.section)
                .limit(5)
            )
            samples = (await db.execute(sample_q)).scalars().all()
            for f in samples:
                unit_str = f" {f.unit}" if f.unit else ""
                print(f"    [{f.section}] {f.display_name}: {f.raw_value}{unit_str}")

        total_field_count = (await db.execute(
            select(func.count(ExtractedField.id))
            .join(Document, ExtractedField.document_id == Document.id)
            .where(Document.session_id == session.id)
        )).scalar()

        print(f"\n  TOTAL: {total_field_count} fields across {len(docs)} documents")
        print(f"  Extraction time: {total_time:.1f}s total")
        print(f"  Avg per document: {total_time/len(docs):.1f}s")


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("litellm").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    asyncio.run(main())
