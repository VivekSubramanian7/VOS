"""Shared fixtures.

Integration tests run against a real, ephemeral Neo4j. They are marked so the fast
offline suite stays the default:

    uv run pytest -m "not integration"    # seconds, no Docker, no API spend
    uv run pytest                         # everything
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

if TYPE_CHECKING:
    from vos.graph import Neo4jGraph

NEO4J_IMAGE = "neo4j:2026.06.0-community"  # must match docker-compose.yml
NEO4J_PASSWORD = "testpassword"


@pytest.fixture(scope="session")
def neo4j_uri() -> Iterator[str]:
    """A real Neo4j for the session.

    Honours VOS_TEST_NEO4J_URI so the suite can be pointed at an already-running
    instance (e.g. `docker compose up -d neo4j`) instead of paying container startup
    on every run.
    """
    if existing := os.environ.get("VOS_TEST_NEO4J_URI"):
        yield existing
        return

    from testcontainers.community.neo4j import Neo4jContainer

    container = Neo4jContainer(image=NEO4J_IMAGE, password=NEO4J_PASSWORD)
    container.start()
    try:
        yield container.get_connection_url()
    finally:
        container.stop()


@pytest.fixture(scope="session")
def neo4j_password() -> str:
    return os.environ.get("VOS_TEST_NEO4J_PASSWORD", NEO4J_PASSWORD)


@pytest_asyncio.fixture
async def graph(neo4j_uri: str, neo4j_password: str) -> AsyncIterator[Neo4jGraph]:
    """A schema-initialised, empty graph per test.

    Wiping between tests is cheap and it is exactly what `--rebuild` does in
    production, so the teardown path is itself under test.
    """
    from vos.graph import Neo4jGraph

    store = Neo4jGraph.connect(neo4j_uri, "neo4j", neo4j_password)
    await store.ensure_schema()
    await store.wipe()
    try:
        yield store
    finally:
        await store.close()
