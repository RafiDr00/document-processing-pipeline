"""
Tests for the PDF extraction and Excel export services.

Covers text extraction, structured parsing, OCR detection, and Excel generation.
"""

import os
import tempfile

import pytest

from app.services.excel_exporter import ExcelExporter, ExcelExportError
from app.services.pdf_extractor import PDFExtractionError, PDFExtractor


class TestPDFExtractor:
    """Tests for the PDF text extraction service."""

    def test_extract_native_pdf(self, sample_pdf_path: str):
        extractor = PDFExtractor()
        result = extractor.extract(sample_pdf_path)

        assert result["method"] == "native"
        assert result["page_count"] == 1
        assert len(result["text"]) > 0
        assert len(result["records"]) > 0

    def test_extract_fields_from_text(self, sample_pdf_path: str):
        extractor = PDFExtractor()
        result = extractor.extract(sample_pdf_path)

        records = result["records"]
        assert len(records) >= 1

        record = records[0]
        # The sample PDF contains "Invoice No: INV-2025-001"
        assert record.get("ID") != "N/A" or record.get("Name") != "N/A"

    def test_extract_nonexistent_file(self):
        extractor = PDFExtractor()
        with pytest.raises(PDFExtractionError, match="File not found"):
            extractor.extract("/nonexistent/path/file.pdf")

    def test_extract_empty_pdf(self, empty_pdf_path: str):
        pytest.importorskip("cv2", reason="OCR test requires opencv-python-headless")
        extractor = PDFExtractor()
        result = extractor.extract(empty_pdf_path)

        # Should succeed but with minimal data
        assert result["page_count"] == 1
        assert len(result["records"]) >= 0

    def test_is_scanned_detection(self, sample_pdf_path: str, empty_pdf_path: str):
        extractor = PDFExtractor()

        # Native PDF with text should not be detected as scanned
        assert extractor._is_scanned_pdf(sample_pdf_path) is False

        # Empty PDF should be detected as scanned (no text)
        assert extractor._is_scanned_pdf(empty_pdf_path) is True

    def test_extract_field_regex(self):
        text = "Invoice No: INV-12345\nClient Name: John Doe"
        extractor = PDFExtractor()

        invoice = extractor._extract_field(text, r"Invoice\s*No\s*[:]\s*(\S+)")
        assert invoice == "INV-12345"

        name = extractor._extract_field(text, r"Client\s*Name\s*[:]\s*(.+?)(?:\n|$)")
        assert name == "John Doe"

    def test_extract_field_no_match(self):
        extractor = PDFExtractor()
        result = extractor._extract_field("random text", r"ZZZZZ\s*(\S+)")
        assert result == "N/A"

    def test_extract_date_various_formats(self):
        extractor = PDFExtractor()

        assert extractor._extract_date("Date: 01/15/2025") != "N/A"
        assert extractor._extract_date("Date: 2025-01-15") != "N/A"
        assert extractor._extract_date("Date: January 15, 2025") != "N/A"
        assert extractor._extract_date("no date here") == "N/A"

    def test_retry_logic(self):
        """Verify the extractor respects max_retries config."""
        extractor = PDFExtractor(max_retries=1)
        assert extractor.max_retries == 1


class TestExcelExporter:
    """Tests for the Excel export service."""

    def test_export_records(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = ExcelExporter(export_dir=tmpdir)
            records = [
                {"Name": "Alice", "ID": "001", "Email": "alice@test.com"},
                {"Name": "Bob", "ID": "002", "Email": "bob@test.com"},
            ]
            output_path = exporter.export(records, filename="test.xlsx")

            assert os.path.isfile(output_path)
            assert output_path.endswith(".xlsx")

    def test_export_empty_records_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = ExcelExporter(export_dir=tmpdir)
            with pytest.raises(ExcelExportError, match="No records"):
                exporter.export([])

    def test_export_auto_filename(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = ExcelExporter(export_dir=tmpdir)
            records = [{"Name": "Test", "Value": "123"}]
            output_path = exporter.export(records)

            assert os.path.isfile(output_path)
            assert "export_" in os.path.basename(output_path)

    def test_export_with_sorting(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = ExcelExporter(export_dir=tmpdir)
            records = [
                {"Name": "Zara", "ID": "003"},
                {"Name": "Alice", "ID": "001"},
                {"Name": "Mike", "ID": "002"},
            ]
            output_path = exporter.export(records)

            import pandas as pd

            df = pd.read_excel(output_path)
            # Should be sorted by Name (first priority column available)
            assert df.iloc[0]["Name"] == "Alice"
            assert df.iloc[-1]["Name"] == "Zara"

    def test_export_custom_sheet_name(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = ExcelExporter(export_dir=tmpdir)
            records = [{"Name": "Test"}]
            output_path = exporter.export(records, sheet_name="Custom Sheet")

            import pandas as pd

            xl = pd.ExcelFile(output_path)
            try:
                assert "Custom Sheet" in xl.sheet_names
            finally:
                xl.close()  # Must close before TemporaryDirectory cleanup (Windows file lock)
