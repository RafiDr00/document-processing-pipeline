"""
PDF text extraction service.

Handles both native (text-based) and scanned (OCR) PDF documents.
Uses PyMuPDF (fitz) for native text extraction and pytesseract for OCR.
Implements retry logic for resilient processing.
"""

import os
import re
import tempfile
import time
from typing import Any

import fitz  # PyMuPDF

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)
settings = get_settings()


class PDFExtractionError(Exception):
    """Raised when PDF text extraction fails."""
    pass


class PDFExtractor:
    """
    Extracts text and structured data from PDF documents.

    Supports:
    - Native text extraction via PyMuPDF
    - OCR for scanned documents via pytesseract + pdf2image
    - Automatic detection of scanned vs. native PDFs
    - Structured field parsing from extracted text
    """

    def __init__(self, ocr_lang: str | None = None, max_retries: int | None = None):
        self.ocr_lang = ocr_lang or settings.OCR_LANG
        self.max_retries = max_retries or settings.MAX_RETRIES
        self.retry_delay = settings.RETRY_DELAY_SECONDS

    def extract(self, file_path: str) -> dict[str, Any]:
        """
        Extract text and structured data from a PDF file.

        Returns a dict with:
        - text: Full extracted text
        - page_count: Number of pages
        - records: List of parsed structured records
        - method: 'native' or 'ocr'
        """
        if not os.path.isfile(file_path):
            raise PDFExtractionError(f"File not found: {file_path}")

        logger.info("Starting PDF extraction", extra={"file_path": file_path})

        for attempt in range(1, self.max_retries + 1):
            try:
                is_scanned = self._is_scanned_pdf(file_path)
                method = "ocr" if is_scanned else "native"

                if is_scanned:
                    text = self._extract_ocr(file_path)
                else:
                    text = self._extract_native(file_path)

                page_count = self._get_page_count(file_path)
                records = self._parse_structured_data(text)

                logger.info(
                    "PDF extraction completed",
                    extra={
                        "file_path": file_path,
                        "method": method,
                        "page_count": page_count,
                        "record_count": len(records),
                        "text_length": len(text),
                    },
                )

                return {
                    "text": text,
                    "page_count": page_count,
                    "records": records,
                    "method": method,
                }

            except PDFExtractionError:
                raise
            except Exception as e:
                logger.warning(
                    f"Extraction attempt {attempt}/{self.max_retries} failed: {e}",
                    extra={"file_path": file_path, "attempt": attempt},
                )
                if attempt == self.max_retries:
                    raise PDFExtractionError(
                        f"Failed to extract PDF after {self.max_retries} attempts: {e}"
                    ) from e
                time.sleep(self.retry_delay * attempt)

        # Should never reach here, but for type safety
        raise PDFExtractionError("Extraction failed unexpectedly")

    def _is_scanned_pdf(self, file_path: str) -> bool:
        """Detect if a PDF is scanned (image-only) or has embedded text."""
        try:
            with fitz.open(file_path) as doc:
                for page in doc:
                    if page.get_text("text").strip():
                        return False
            return True
        except Exception as e:
            logger.warning(f"Could not determine PDF type, assuming native: {e}")
            return False

    def _extract_native(self, file_path: str) -> str:
        """Extract text from a native (text-based) PDF using PyMuPDF."""
        try:
            with fitz.open(file_path) as doc:
                pages_text = []
                for page_num, page in enumerate(doc, start=1):
                    text = page.get_text("text")
                    if text.strip():
                        pages_text.append(text)
                        logger.debug(
                            f"Page {page_num}: extracted {len(text)} chars"
                        )
                full_text = "\n\n".join(pages_text)
                logger.info(f"Native extraction: {len(full_text)} total chars")
                return full_text
        except Exception as e:
            raise PDFExtractionError(f"Native text extraction failed: {e}") from e

    def _extract_ocr(self, file_path: str) -> str:
        """Extract text from a scanned PDF using OCR."""
        try:
            import cv2
            import numpy as np
            from pdf2image import convert_from_path
            import pytesseract
        except ImportError as e:
            raise PDFExtractionError(
                f"OCR dependencies not installed ({e}). "
                "Install: pip install pdf2image pytesseract opencv-python"
            ) from e

        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                images = convert_from_path(file_path, output_folder=tmpdir)
                pages_text = []

                for i, img in enumerate(images, start=1):
                    # Preprocess image for better OCR accuracy
                    img_array = np.array(img)
                    gray = cv2.cvtColor(img_array, cv2.COLOR_RGB2GRAY)
                    blur = cv2.GaussianBlur(gray, (3, 3), 0)
                    _, thresh = cv2.threshold(
                        blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
                    )

                    page_text = pytesseract.image_to_string(
                        thresh, lang=self.ocr_lang
                    )
                    pages_text.append(page_text)
                    logger.debug(f"OCR page {i}: {len(page_text)} chars")

                full_text = "\n\n".join(pages_text)
                logger.info(f"OCR extraction: {len(full_text)} total chars")
                return full_text
        except PDFExtractionError:
            raise
        except Exception as e:
            raise PDFExtractionError(f"OCR extraction failed: {e}") from e

    def _get_page_count(self, file_path: str) -> int:
        """Return the number of pages in a PDF."""
        try:
            with fitz.open(file_path) as doc:
                return len(doc)
        except Exception:
            return 0

    def _parse_structured_data(self, text: str) -> list[dict[str, Any]]:
        """
        Parse structured records from extracted text.

        Extracts common document fields using regex patterns.
        Returns a list of record dicts.
        """
        if not text or not text.strip():
            return []

        records: list[dict[str, Any]] = []
        record: dict[str, Any] = {}

        # Extract common fields
        record["Name"] = self._extract_field(
            text, r"(?:Client\s*Name|Name|Customer)\s*[:]\s*(.+?)(?:\n|$)"
        )
        record["ID"] = self._extract_field(
            text, r"(?:ID|Invoice\s*(?:No|Number|#))\s*[:#]?\s*(\S+)"
        )
        record["Email"] = self._extract_field(
            text, r"[\w.+-]+@[\w-]+\.[\w.-]+"
        )
        record["Date"] = self._extract_date(text)
        record["Total"] = self._extract_field(
            text, r"Total\s*[:]*\s*\$?([\d,]+\.?\d*)"
        )
        record["Notes"] = self._extract_field(
            text, r"(?:Notes?|Remarks?|Comments?)\s*[:]\s*(.+?)(?:\n|$)"
        )

        # Only add if we extracted at least one meaningful field
        has_data = any(
            v and v != "N/A"
            for k, v in record.items()
            if k != "Notes"
        )

        if has_data:
            records.append(record)
        else:
            # Return a generic record with the raw text summary
            records.append({
                "Name": "N/A",
                "ID": "N/A",
                "Email": "N/A",
                "Date": "N/A",
                "Total": "N/A",
                "Notes": text[:500] if text else "No text extracted",
            })

        return records

    @staticmethod
    def _extract_field(text: str, pattern: str) -> str:
        """Extract a single field value using a regex pattern."""
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip() if match.lastindex else match.group(0).strip()
        return "N/A"

    @staticmethod
    def _extract_date(text: str) -> str:
        """Extract a date string from text."""
        date_patterns = [
            r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b",
            r"\b(\d{4}[/-]\d{1,2}[/-]\d{1,2})\b",
            r"\b(\w+\s+\d{1,2},?\s+\d{4})\b",
        ]
        for pattern in date_patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(1).strip()
        return "N/A"
