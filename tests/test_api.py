"""
Tests for the document API endpoints (v2.0).

Covers upload, retrieval, listing, export, and error scenarios.
"""

import io
import os

import pytest
import pytest_asyncio
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


class TestHealthEndpoint:
    """Tests for the health check endpoint."""

    async def test_health_check(self, client: AsyncClient):
        response = await client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert "version" in data

    async def test_root_endpoint(self, client: AsyncClient):
        response = await client.get("/")
        assert response.status_code == 200
        data = response.json()
        assert "service" in data
        assert "docs" in data

    async def test_metrics_endpoint(self, client: AsyncClient):
        response = await client.get("/metrics")
        assert response.status_code == 200
        assert response.headers["content-type"] == "text/plain; charset=utf-8"
        text = response.text
        assert "http_requests_total" in text


class TestDocumentUpload:
    """Tests for POST /api/v1/documents/upload."""

    async def test_upload_valid_pdf(self, client: AsyncClient, sample_pdf_path: str):
        with open(sample_pdf_path, "rb") as f:
            response = await client.post(
                "/api/v1/documents/upload",
                files={"file": ("test_invoice.pdf", f, "application/pdf")},
            )
        assert response.status_code == 202
        data = response.json()
        assert "id" in data
        assert data["status"] == "pending"
        assert data["filename"] == "test_invoice.pdf"
        assert "content_hash" in data  # v2: SHA-256 hash returned

    async def test_upload_non_pdf_rejected(self, client: AsyncClient):
        content = b"This is not a PDF"
        response = await client.post(
            "/api/v1/documents/upload",
            files={"file": ("document.txt", io.BytesIO(content), "text/plain")},
        )
        assert response.status_code == 400
        assert "PDF" in response.json()["detail"]

    async def test_upload_empty_file_rejected(self, client: AsyncClient):
        response = await client.post(
            "/api/v1/documents/upload",
            files={"file": ("empty.pdf", io.BytesIO(b""), "application/pdf")},
        )
        assert response.status_code == 400


class TestDocumentRetrieval:
    """Tests for GET /api/v1/documents/{id} and GET /api/v1/documents."""

    async def test_get_nonexistent_document(self, client: AsyncClient):
        fake_id = "00000000-0000-0000-0000-000000000000"
        response = await client.get(f"/api/v1/documents/{fake_id}")
        assert response.status_code == 404

    async def test_list_documents_empty(self, client: AsyncClient):
        response = await client.get("/api/v1/documents")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 0
        assert data["documents"] == []

    async def test_list_documents_with_pagination(self, client: AsyncClient, sample_pdf_path: str):
        # Upload a document first
        with open(sample_pdf_path, "rb") as f:
            await client.post(
                "/api/v1/documents/upload",
                files={"file": ("test.pdf", f, "application/pdf")},
            )

        response = await client.get("/api/v1/documents?skip=0&limit=10")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] >= 1

    async def test_upload_and_retrieve_document(self, client: AsyncClient, sample_pdf_path: str):
        # Upload
        with open(sample_pdf_path, "rb") as f:
            upload_response = await client.post(
                "/api/v1/documents/upload",
                files={"file": ("invoice.pdf", f, "application/pdf")},
            )
        doc_id = upload_response.json()["id"]

        # Retrieve
        response = await client.get(f"/api/v1/documents/{doc_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == doc_id
        assert data["original_filename"] == "invoice.pdf"


class TestDocumentExport:
    """Tests for POST /api/v1/documents/{id}/export."""

    async def test_export_nonexistent_document(self, client: AsyncClient):
        fake_id = "00000000-0000-0000-0000-000000000000"
        response = await client.post(f"/api/v1/documents/{fake_id}/export")
        assert response.status_code == 404

    async def test_export_pending_document_rejected(self, client: AsyncClient, sample_pdf_path: str):
        # Upload (stays in pending since background task doesn't fully run in test)
        with open(sample_pdf_path, "rb") as f:
            upload_response = await client.post(
                "/api/v1/documents/upload",
                files={"file": ("test.pdf", f, "application/pdf")},
            )
        doc_id = upload_response.json()["id"]

        response = await client.post(f"/api/v1/documents/{doc_id}/export")
        assert response.status_code == 400
