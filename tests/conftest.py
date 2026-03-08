"""
Pytest configuration and fixtures.

Provides test database, test client, and sample data fixtures
for all test modules.  Overrides security, storage, and DB
dependencies so tests run with zero infrastructure.
"""

import asyncio
import os
import tempfile
from collections.abc import AsyncGenerator, Generator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.database import Base, get_db
from app.main import app

# Use SQLite for testing (no PostgreSQL required in CI)
TEST_DATABASE_URL = "sqlite+aiosqlite:///./test.db"

test_engine = create_async_engine(TEST_DATABASE_URL, echo=False)
test_session_factory = async_sessionmaker(
    test_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


@pytest.fixture(scope="session")
def event_loop():
    """Create a single event loop for all tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(autouse=True)
async def setup_database():
    """Create and tear down test database for each test."""
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
    """Override database dependency for testing."""
    async with test_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


# Override the DB dependency
app.dependency_overrides[get_db] = override_get_db

# Ensure API_KEY is empty so auth is disabled during tests
os.environ.setdefault("API_KEY", "")
# Ensure storage backend is local
os.environ.setdefault("STORAGE_BACKEND", "local")


@pytest_asyncio.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    """Async HTTP test client."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def sample_pdf_path() -> Generator[str, None, None]:
    """Create a minimal valid PDF for testing."""
    import fitz

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        doc = fitz.open()
        page = doc.new_page()
        text = (
            "Invoice No: INV-2025-001\n"
            "Client Name: Acme Corporation\n"
            "Date: 2025-01-15\n"
            "Email: contact@acme.com\n"
            "Total: $1,250.00\n"
            "Notes: Payment due in 30 days.\n"
        )
        page.insert_text((72, 72), text, fontsize=12)
        doc.save(f.name)
        doc.close()
        path = f.name

    yield path
    os.unlink(path)


@pytest.fixture
def empty_pdf_path() -> Generator[str, None, None]:
    """Create an empty PDF for testing edge cases."""
    import fitz

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        doc = fitz.open()
        doc.new_page()
        doc.save(f.name)
        doc.close()
        path = f.name

    yield path
    os.unlink(path)
