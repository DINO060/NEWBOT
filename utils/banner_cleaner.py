"""
Banner cleaning utilities for PDF Bot
Identifies and removes likely banner pages from PDFs.
"""
import os
import tempfile
import logging
from typing import List, Optional

import pikepdf
import fitz  # PyMuPDF

logger = logging.getLogger(__name__)


def _identify_banner_pages(pdf_path: str) -> List[int]:
    """Heuristically identify banner pages (1-based indices)."""
    banner_pages: List[int] = []
    try:
        doc = fitz.open(pdf_path)
        total = len(doc)

        keywords = [
            'processed', 'verified', 'banner', 'watermark',
            '@', 'telegram', 'bot', 'copyright', '©',
            'document processed', 'pdf processing', 'scanned by', 'converted by'
        ]

        for i, page in enumerate(doc):
            text = (page.get_text() or '').lower()
            count = sum(1 for k in keywords if k in text)

            # Heuristics: multiple keywords OR very low text on first/last pages
            if count >= 3:
                banner_pages.append(i + 1)
            elif len(text.strip()) < 120 and (i == 0 or i == total - 1):
                banner_pages.append(i + 1)

        doc.close()
    except Exception as e:
        logger.error(f"identify_banner_pages error: {e}")

    # Deduplicate and keep sorted
    return sorted(set(banner_pages))


def clean_pdf_banners(pdf_bytes: bytes, user_id: Optional[int] = None) -> bytes:
    """
    Remove detected banner pages from a PDF represented as bytes.

    Returns the possibly-modified PDF bytes. If cleaning fails or nothing to
    remove is detected, returns the original bytes.
    """
    try:
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp_in:
            tmp_in.write(pdf_bytes)
            in_path = tmp_in.name

        out_path = in_path.replace('.pdf', '_cleaned.pdf')

        pages = _identify_banner_pages(in_path)
        if not pages:
            try:
                os.remove(in_path)
            except Exception:
                pass
            return pdf_bytes

        with pikepdf.open(in_path) as pdf:
            for p in sorted(pages, reverse=True):
                if 1 <= p <= len(pdf.pages):
                    del pdf.pages[p - 1]

            if len(pdf.pages) == 0:
                # Avoid returning an empty document
                try:
                    os.remove(in_path)
                except Exception:
                    pass
                return pdf_bytes

            pdf.save(out_path)

        with open(out_path, 'rb') as f:
            cleaned = f.read()

        # Cleanup
        for p in (in_path, out_path):
            try:
                os.remove(p)
            except Exception:
                pass

        logger.info(f"Banner pages cleaned: {len(pages)}")
        return cleaned
    except Exception as e:
        logger.error(f"clean_pdf_banners error: {e}")
        return pdf_bytes


__all__ = [
    'clean_pdf_banners',
]


