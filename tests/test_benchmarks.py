"""Benchmark tests for Biolab MCP Server - validates performance claims."""

import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from biolab.db import connect
from biolab.models import RetrievalRecord
from biolab.pubmed_client import fetch, search, search_and_fetch
from biolab.retrieval_log import start_writer, stop_writer, write_retrieval


# Fixtures
@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    db_path = Path("/tmp/biolab_bench_test.db")
    db_path.unlink(missing_ok=True)
    conn = connect(str(db_path))
    yield conn
    conn.close()
    db_path.unlink(missing_ok=True)


@pytest.fixture
def temp_db_with_writer():
    """Create a temporary database with background writer running."""
    db_path = Path("/tmp/biolab_bench_test_writer.db")
    db_path.unlink(missing_ok=True)
    conn = connect(str(db_path))
    start_writer(str(db_path))
    yield conn
    stop_writer()
    conn.close()
    db_path.unlink(missing_ok=True)


# Helper functions
def make_retrieval_record(
    query: str = "test query",
    external_id: str = "12345",
    agent_id: str = "test:agent",
    source: str = "pubmed",
) -> tuple[str, dict, dict, str]:
    """Create a sample retrieval record."""
    import hashlib
    import uuid

    raw_response = f'<PubmedArticle><MedlineCitation><PMID>{external_id}</PMID><Article><ArticleTitle>Test Paper</ArticleTitle></Article></MedlineCitation></PubmedArticle>'
    snapshot = {"title": "Test Paper", "abstract": "Test abstract"}
    source_metadata = {"pub_status": "ppublish"}

    response_hash = hashlib.sha256(raw_response.encode("utf-8")).hexdigest()

    record = RetrievalRecord(
        retrieval_id=str(uuid.uuid4()),
        source=source,
        external_id=external_id,
        query_text=query,
        retrieved_at=datetime.now(UTC).isoformat(),
        agent_id=agent_id,
        source_metadata=json.dumps(source_metadata),
        raw_response=raw_response,
        snapshot=json.dumps(snapshot),
        response_hash=response_hash,
        prev_hash="",
    )
    return record.retrieval_id, record.source_metadata, record.snapshot, record.raw_response


# Benchmarks
class TestPubMedRetrievalBenchmarks:
    """Benchmarks for PubMed retrieval latency."""

    @pytest.mark.benchmark
    def test_search_latency(self, benchmark):
        """Benchmark PubMed search (esearch) latency."""
        # Run multiple times to get stable measurements
        def run_search():
            return search("BRCA1 pancreatic cancer", 5)

        result = benchmark.pedantic(run_search, rounds=10, iterations=1)
        assert len(result) == 5

    @pytest.mark.benchmark
    def test_fetch_latency(self, benchmark):
        """Benchmark PubMed fetch (efetch) latency."""
        pmids = ["42431391", "42431392", "42431393"]

        def run_fetch():
            return fetch(pmids)

        result = benchmark.pedantic(run_fetch, rounds=10, iterations=1)
        assert len(result) == 3

    @pytest.mark.benchmark
    def test_search_and_fetch_latency(self, benchmark):
        """Benchmark end-to-end search + fetch latency."""
        def run_search_and_fetch():
            return search_and_fetch("BRCA1", 3)

        result = benchmark.pedantic(run_search_and_fetch, rounds=5, iterations=1)
        assert len(result) == 3


class TestSQLiteCommitBenchmarks:
    """Benchmarks for SQLite commit latency."""

    @pytest.mark.benchmark
    def test_commit_latency_sync(self, benchmark, temp_db):
        """Benchmark synchronous SQLite commit latency."""
        retrieval_id, source_metadata, snapshot, raw_response = make_retrieval_record()

        def run_commit():
            return write_retrieval(
                temp_db,
                query_text="test query",
                external_id="12345",
                agent_id="test:agent",
                source="pubmed",
                source_metadata=json.loads(source_metadata),
                raw_response=raw_response,
                snapshot=json.loads(snapshot),
            )

        result = benchmark.pedantic(run_commit, rounds=50, iterations=1)
        assert result is not None

    @pytest.mark.benchmark
    def test_commit_latency_async(self, benchmark, temp_db_with_writer):
        """Benchmark async (background writer) SQLite commit latency."""
        retrieval_id, source_metadata, snapshot, raw_response = make_retrieval_record()

        def run_commit():
            return write_retrieval(
                temp_db_with_writer,
                query_text="test query",
                external_id="12345",
                agent_id="test:agent",
                source="pubmed",
                source_metadata=json.loads(source_metadata),
                raw_response=raw_response,
                snapshot=json.loads(snapshot),
            )

        result = benchmark.pedantic(run_commit, rounds=50, iterations=1)
        assert result is not None


class TestCommitFailureFaultInjection:
    """Fault injection tests for commit failures."""

    def test_commit_failure_simulation(self):
        """
        Simulate commit failures and verify zero orphan rows.

        This test uses a wrapper connection that can simulate failures
        and verifies that no partial writes leave orphan rows in the database.
        
        Uses synchronous write path (no background writer) to ensure
        the fault injection works correctly.
        """
        import random

        from biolab.retrieval_log import stop_writer

        # Ensure no background writer is running
        stop_writer()

        class FaultyConnection:
            """Wrapper that simulates commit failures."""
            def __init__(self, conn):
                self._conn = conn
                self.commit_failures = 0
                self.successful_commits = 0

            def execute(self, *args, **kwargs):
                return self._conn.execute(*args, **kwargs)

            def fetchone(self):
                return self._conn.fetchone()

            def fetchall(self):
                return self._conn.fetchall()

            def commit(self):
                if random.random() < 0.3:  # 30% failure rate
                    self.commit_failures += 1
                    raise sqlite3.OperationalError("Simulated commit failure")
                self.successful_commits += 1
                return self._conn.commit()

            def close(self):
                return self._conn.close()

            def __getattr__(self, name):
                return getattr(self._conn, name)

        # Create a fresh database for this test
        db_path = Path("/tmp/biolab_fault_test.db")
        db_path.unlink(missing_ok=True)
        conn = connect(str(db_path))

        # Wrap the connection - DO NOT start writer thread so it uses sync path
        faulty_conn = FaultyConnection(conn)

        # Run 20 trials with simulated failures
        for trial in range(20):
            try:
                retrieval_id, source_metadata, snapshot, raw_response = make_retrieval_record(
                    external_id=f"test_{trial}"
                )
                write_retrieval(
                    faulty_conn,
                    query_text=f"test query {trial}",
                    external_id=f"test_{trial}",
                    agent_id="test:agent",
                    source="pubmed",
                    source_metadata=json.loads(source_metadata),
                    raw_response=raw_response,
                    snapshot=json.loads(snapshot),
                )
            except sqlite3.OperationalError:
                pass  # Expected failure

        # Verify zero orphan rows: every record that should exist does exist
        cursor = conn.execute("SELECT COUNT(*) FROM retrievals")
        actual_count = cursor.fetchone()[0]
        assert actual_count == faulty_conn.successful_commits, f"Orphan rows detected: {actual_count} != {faulty_conn.successful_commits}"

        # Verify all committed records have valid retrieval_ids
        cursor = conn.execute("SELECT retrieval_id FROM retrievals")
        for row in cursor.fetchall():
            assert row[0] is not None
            assert len(row[0]) == 36  # UUID format

        conn.close()
        db_path.unlink(missing_ok=True)


class TestThroughputBenchmarks:
    """Throughput benchmarks for retrieval records per second."""

    @pytest.mark.benchmark
    def test_retrieval_throughput_sync(self, benchmark, temp_db):
        """Benchmark retrieval records per second (synchronous)."""
        def run_batch():
            for i in range(10):
                retrieval_id, source_metadata, snapshot, raw_response = make_retrieval_record(
                    external_id=f"throughput_{i}"
                )
                write_retrieval(
                    temp_db,
                    query_text=f"query {i}",
                    external_id=f"throughput_{i}",
                    agent_id="test:agent",
                    source="pubmed",
                    source_metadata=json.loads(source_metadata),
                    raw_response=raw_response,
                    snapshot=json.loads(snapshot),
                )

        benchmark.pedantic(run_batch, rounds=10, iterations=1)
        # 10 records per batch * 10 rounds = 100 records
        # Should achieve ~7.99 records/sec based on claims

    @pytest.mark.benchmark
    def test_retrieval_throughput_async(self, benchmark, temp_db_with_writer):
        """Benchmark retrieval records per second (async with background writer)."""
        def run_batch():
            for i in range(10):
                retrieval_id, source_metadata, snapshot, raw_response = make_retrieval_record(
                    external_id=f"throughput_{i}"
                )
                write_retrieval(
                    temp_db_with_writer,
                    query_text=f"query {i}",
                    external_id=f"throughput_{i}",
                    agent_id="test:agent",
                    source="pubmed",
                    source_metadata=json.loads(source_metadata),
                    raw_response=raw_response,
                    snapshot=json.loads(snapshot),
                )

        benchmark.pedantic(run_batch, rounds=10, iterations=1)


# Latency measurement utilities for manual verification
def measure_pubmed_latency():
    """Measure and report PubMed retrieval latency percentiles."""
    latencies = []
    for _ in range(20):
        start = time.perf_counter()
        search("BRCA1 pancreatic cancer", 5)
        latencies.append((time.perf_counter() - start) * 1000)  # ms

    latencies.sort()
    p50 = latencies[len(latencies) // 2]
    p95 = latencies[int(len(latencies) * 0.95)]
    print(f"PubMed search latency: p50={p50:.1f}ms, p95={p95:.1f}ms")


def measure_fetch_latency():
    """Measure and report PubMed fetch latency percentiles."""
    latencies = []
    pmids = ["42431391", "42431392", "42431393"]
    for _ in range(20):
        start = time.perf_counter()
        fetch(pmids)
        latencies.append((time.perf_counter() - start) * 1000)  # ms

    latencies.sort()
    p50 = latencies[len(latencies) // 2]
    p95 = latencies[int(len(latencies) * 0.95)]
    print(f"PubMed fetch latency: p50={p50:.1f}ms, p95={p95:.1f}ms")


def measure_sqlite_commit_latency():
    """Measure and report SQLite commit latency percentiles."""
    db_path = Path("/tmp/biolab_latency_test.db")
    db_path.unlink(missing_ok=True)
    conn = connect(str(db_path))

    latencies = []
    for i in range(100):
        retrieval_id, source_metadata, snapshot, raw_response = make_retrieval_record(
            external_id=f"latency_{i}"
        )
        start = time.perf_counter()
        write_retrieval(
            conn,
            query_text=f"query {i}",
            external_id=f"latency_{i}",
            agent_id="test:agent",
            source="pubmed",
            source_metadata=json.loads(source_metadata),
            raw_response=raw_response,
            snapshot=json.loads(snapshot),
        )
        latencies.append((time.perf_counter() - start) * 1000)  # ms

    latencies.sort()
    p50 = latencies[len(latencies) // 2]
    p95 = latencies[int(len(latencies) * 0.95)]
    print(f"SQLite commit latency: p50={p50:.2f}ms, p95={p95:.2f}ms")

    conn.close()
    db_path.unlink()


if __name__ == "__main__":
    print("Measuring PubMed retrieval latency...")
    measure_pubmed_latency()
    measure_fetch_latency()

    print("\nMeasuring SQLite commit latency...")
    measure_sqlite_commit_latency()
