"""Compare extraction quality with resized images.

Tests P718 (previously 95 fields) and P818 (previously 0 fields)
to verify resizing doesn't hurt quality and fixes reliability.
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


async def main():
    from app.database import async_session_factory
    from app.models.session import Session, SessionStatus
    from app.models.document import Document, DocumentStatus
    from app.models.document_page import DocumentPage
    from app.services.document_processor import process_document
    from app.services.extraction import extract_document
    from app.config import settings
    from sqlalchemy import select

    # Baseline from original run (no resize, no retry)
    BASELINE = {
        "pds-P718.pdf": 95,
        "pds-P818.pdf": 0,   # failed originally
        "pds-P300228.pdf": 78,
        "pds-P600173.pdf": 42,
    }

    test_pdfs = ["pds-P718.pdf", "pds-P818.pdf", "pds-P300228.pdf", "pds-P600173.pdf"]

    async with async_session_factory() as db:
        session = Session(status=SessionStatus.active, title="Resize Comparison")
        db.add(session)
        await db.flush()
        await db.refresh(session)

        for pdf_name in test_pdfs:
            pdf_path = Path(pdf_name)
            if not pdf_path.exists():
                print(f"  SKIP {pdf_name} (not found)")
                continue

            print(f"\n{'=' * 50}")
            print(f"  {pdf_name}")
            print(f"  Baseline: {BASELINE.get(pdf_name, '?')} fields")
            print(f"{'=' * 50}")

            # Ingest
            upload_dir = settings.UPLOAD_DIR / str(session.id)
            upload_dir.mkdir(parents=True, exist_ok=True)
            dest = upload_dir / pdf_name
            dest.write_bytes(pdf_path.read_bytes())

            doc = Document(
                session_id=session.id,
                filename=pdf_name,
                file_path=str(dest),
                status=DocumentStatus.uploading,
            )
            db.add(doc)
            await db.flush()
            doc = await process_document(dest, doc.id, db)

            # Show page info
            pages = (await db.execute(
                select(DocumentPage).where(DocumentPage.document_id == doc.id).order_by(DocumentPage.page_number)
            )).scalars().all()

            for p in pages:
                print(f"  Page {p.page_number}: {p.page_type} | {p.extraction_quality}")

            # Extract
            print(f"\n  Extracting with resized images (max 1600px)...")
            t0 = time.time()
            fields = await extract_document(doc.id, db)
            elapsed = time.time() - t0

            # Results
            baseline = BASELINE.get(pdf_name, 0)
            new_count = len(fields)
            delta = new_count - baseline
            delta_str = f"+{delta}" if delta > 0 else str(delta)

            print(f"\n  Results:")
            print(f"    Fields: {new_count} (baseline: {baseline}, delta: {delta_str})")
            print(f"    Time: {elapsed:.1f}s")

            # Group by section
            sections = {}
            for f in fields:
                sections.setdefault(f.section, []).append(f)

            for section, sf in sorted(sections.items()):
                print(f"    [{section}]: {len(sf)} fields")

            # Show a few sample fields for quality check
            print(f"\n  Sample fields:")
            for f in fields[:8]:
                unit_str = f" {f.unit}" if f.unit else ""
                print(f"    {f.display_name}: {f.raw_value}{unit_str} (conf: {f.confidence})")

            if new_count > 8:
                print(f"    ... and {new_count - 8} more")

            # Quality verdict
            if baseline == 0 and new_count > 0:
                print(f"\n  VERDICT: FIXED (was broken, now {new_count} fields)")
            elif new_count >= baseline * 0.9:
                print(f"\n  VERDICT: PASS (≥90% of baseline)")
            else:
                print(f"\n  VERDICT: REGRESSION ({new_count}/{baseline} = {new_count/max(baseline,1)*100:.0f}%)")

        await db.commit()

        print(f"\n{'=' * 50}")
        print("DONE")
        print(f"{'=' * 50}")


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("litellm").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    asyncio.run(main())
