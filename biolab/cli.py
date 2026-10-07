"""Biolab CLI — query and explore the retrieval audit trail."""

import builtins
import json
import os
from pathlib import Path

import typer
from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table

from biolab import auth, db, retrieval_log, workspace
from biolab import evidence as evidence_service

app = typer.Typer(
    name="biolab",
    help="Biolab MCP Server CLI — query and explore the retrieval audit trail.",
    add_completion=False,
    pretty_exceptions_enable=False,  # main() prints one clean line for user errors
)
console = Console()

keys_app = typer.Typer(help="Manage API keys for the hosted server's per-caller rate-limit tiers.")
app.add_typer(keys_app, name="keys")
projects_app = typer.Typer(help="Manage scientist research projects.")
app.add_typer(projects_app, name="projects")
collections_app = typer.Typer(help="Manage reviewed evidence collections.")
app.add_typer(collections_app, name="collections")
watches_app = typer.Typer(help="Monitor evidence searches for changes.")
app.add_typer(watches_app, name="watches")
claims_app = typer.Typer(help="Trace scientific claims to preserved evidence.")
app.add_typer(claims_app, name="claims")


@keys_app.command("create")
def keys_create(
    label: str = typer.Argument(..., help="Human-readable owner, e.g. a name or team"),
    agent_id: str = typer.Option(None, "--agent-id", help="Identity to attribute traffic to (default: label)"),
    db_path: str = typer.Option("biolab.db", "--db", help="Path to SQLite database"),
):
    """Create a new API key. The raw key is shown once — it isn't stored anywhere retrievable."""
    if not label.strip():
        console.print("[red]label must not be empty[/red]")
        raise typer.Exit(1)
    conn = db.connect(db_path)
    raw_key = auth.create_api_key(conn, label.strip(), (agent_id or label).strip())
    console.print(f"[green]Created key for[/green] [bold]{label}[/bold]")
    console.print(f"[bold]{raw_key}[/bold]")
    console.print("[yellow]This is shown once — save it now.[/yellow]")


@keys_app.command("list")
def keys_list(
    db_path: str = typer.Option("biolab.db", "--db", help="Path to SQLite database"),
):
    """List all issued API keys (never shows the raw key)."""
    conn = db.connect(db_path)
    rows = auth.list_api_keys(conn)
    if not rows:
        console.print("[yellow]No keys issued[/yellow]")
        return

    table = Table(title="API Keys")
    table.add_column("Label")
    table.add_column("Agent ID")
    table.add_column("Created At")
    table.add_column("Status")
    for row in rows:
        status = "[red]revoked[/red]" if row["revoked"] else "[green]active[/green]"
        table.add_row(row["label"], row["agent_id"], row["created_at"][:19].replace("T", " "), status)
    console.print(table)


@keys_app.command("revoke")
def keys_revoke(
    label: str = typer.Argument(..., help="Label of the key(s) to revoke"),
    db_path: str = typer.Option("biolab.db", "--db", help="Path to SQLite database"),
):
    """Revoke all active keys for a label."""
    conn = db.connect(db_path)
    count = auth.revoke_api_key(conn, label)
    if count:
        console.print(f"[green]Revoked {count} key(s) for[/green] [bold]{label}[/bold]")
    else:
        console.print(f"[yellow]No active keys found for[/yellow] [bold]{label}[/bold]")
        raise typer.Exit(1)


def _require_text(value: str, what: str) -> str:
    if not value.strip():
        console.print(f"[red]{what} must not be empty[/red]")
        raise typer.Exit(1)
    return value


def _get_conn(db_path: str):
    return db.connect(db_path)


@projects_app.command("create")
def project_create(
    name: str,
    description: str = typer.Option("", "--description", "-d"),
    db_path: str = typer.Option("biolab.db", "--db"),
):
    """Create a research project."""
    conn = _get_conn(db_path)
    try:
        result = workspace.create_project(conn, name, description)
    finally:
        conn.close()
    console.print_json(data=result)


@projects_app.command("list")
def project_list(db_path: str = typer.Option("biolab.db", "--db")):
    """List research projects."""
    conn = _get_conn(db_path)
    try:
        result = workspace.list_projects(conn)
    finally:
        conn.close()
    console.print_json(data={"projects": result})


@projects_app.command("report")
def project_report(
    project_id: str,
    output: Path = typer.Option(..., "--output", "-o", help="Markdown output file"),
    db_path: str = typer.Option("biolab.db", "--db"),
):
    """Export a project as a human-readable evidence report."""
    conn = _get_conn(db_path)
    try:
        report = workspace.get_project_report(conn, project_id)
    finally:
        conn.close()
    if report is None:
        console.print("[red]Project not found[/red]")
        raise typer.Exit(1)
    output.write_text(workspace.render_project_markdown(report))
    console.print(f"[green]Wrote evidence report to {output}[/green]")


@collections_app.command("create")
def collection_create(
    project_id: str,
    name: str,
    description: str = typer.Option("", "--description", "-d"),
    db_path: str = typer.Option("biolab.db", "--db"),
):
    """Create an evidence collection inside a project."""
    conn = _get_conn(db_path)
    try:
        result = workspace.create_collection(conn, project_id, name, description)
    finally:
        conn.close()
    console.print_json(data=result)


@collections_app.command("list")
def collection_list(
    project_id: str = typer.Option(None, "--project"),
    db_path: str = typer.Option("biolab.db", "--db"),
):
    """List evidence collections, optionally within one project."""
    conn = _get_conn(db_path)
    try:
        result = workspace.list_collections(conn, project_id)
    finally:
        conn.close()
    console.print_json(data={"collections": result})


@collections_app.command("add")
def collection_add(
    collection_id: str,
    retrieval_id: str,
    relevance: str = typer.Option("unreviewed", "--relevance"),
    note: str = typer.Option("", "--note"),
    db_path: str = typer.Option("biolab.db", "--db"),
):
    """Add or review a retrieval in an evidence collection."""
    conn = _get_conn(db_path)
    try:
        result = workspace.add_collection_item(
            conn, collection_id, retrieval_id, note, relevance
        )
    finally:
        conn.close()
    console.print_json(data=result)


@collections_app.command("show")
def collection_show(
    collection_id: str,
    db_path: str = typer.Option("biolab.db", "--db"),
):
    """Show a collection with its reviewed evidence."""
    conn = _get_conn(db_path)
    try:
        result = workspace.get_collection(conn, collection_id)
    finally:
        conn.close()
    if result is None:
        console.print("[red]Collection not found[/red]")
        raise typer.Exit(1)
    console.print_json(data=result)


@watches_app.command("create")
def watch_create(
    project_id: str,
    source: str,
    query: str,
    agent_id: str = typer.Option("cli:watch", "--agent", "-a"),
    max_results: int = typer.Option(10, "--max", "-n"),
    db_path: str = typer.Option("biolab.db", "--db"),
):
    """Create a saved evidence watch."""
    conn = _get_conn(db_path)
    try:
        result = workspace.create_watch(
            conn, project_id, source, query, agent_id, max_results
        )
    finally:
        conn.close()
    console.print_json(data=result)


@watches_app.command("run")
def watch_run(watch_id: str, db_path: str = typer.Option("biolab.db", "--db")):
    """Run one saved watch and record changes."""
    conn = _get_conn(db_path)
    try:
        result = workspace.run_watch(conn, watch_id)
    finally:
        conn.close()
    console.print_json(data=result)


@watches_app.command("events")
def watch_events(watch_id: str, db_path: str = typer.Option("biolab.db", "--db")):
    """List recorded change events for a watch."""
    conn = _get_conn(db_path)
    try:
        result = workspace.list_watch_events(conn, watch_id)
    finally:
        conn.close()
    console.print_json(data={"events": result})


@watches_app.command("run-all")
def watch_run_all(db_path: str = typer.Option("biolab.db", "--db")):
    """Run every active watch; suitable for cron or a scheduled job."""
    conn = _get_conn(db_path)
    try:
        result = workspace.run_active_watches(conn)
    finally:
        conn.close()
    console.print_json(data=result)
    if result["errors"]:
        raise typer.Exit(1)


@claims_app.command("create")
def claim_create(
    project_id: str,
    claim: str,
    db_path: str = typer.Option("biolab.db", "--db"),
):
    """Create a claim that can be assessed against preserved evidence."""
    conn = _get_conn(db_path)
    try:
        result = workspace.create_claim(conn, project_id, claim)
    finally:
        conn.close()
    console.print_json(data=result)


@claims_app.command("link")
def claim_link(
    claim_id: str,
    retrieval_id: str,
    stance: str = typer.Option(..., "--stance"),
    assessment: str = typer.Option("unreviewed", "--assessment"),
    note: str = typer.Option("", "--note"),
    db_path: str = typer.Option("biolab.db", "--db"),
):
    """Link a claim to evidence with an explicit stance and review state."""
    conn = _get_conn(db_path)
    try:
        result = workspace.link_claim_evidence(
            conn, claim_id, retrieval_id, stance, assessment, note
        )
    finally:
        conn.close()
    console.print_json(data=result)


@claims_app.command("show")
def claim_show(claim_id: str, db_path: str = typer.Option("biolab.db", "--db")):
    """Show a claim and every supporting, conflicting, or contextual record."""
    conn = _get_conn(db_path)
    try:
        result = workspace.trace_claim(conn, claim_id)
    finally:
        conn.close()
    if result is None:
        console.print("[red]Claim not found[/red]")
        raise typer.Exit(1)
    console.print_json(data=result)


@app.command()
def evidence(
    query: str = typer.Argument(..., help="Scientific evidence query"),
    sources: str = typer.Option(
        "pubmed,europepmc,clinicaltrials,uniprot,opentargets",
        "--sources",
        help="Comma-separated sources",
    ),
    agent_id: str = typer.Option("cli:user", "--agent", "-a", help="Agent identifier"),
    max_results: int = typer.Option(5, "--max", "-n", help="Max results per source (1-50)"),
    db_path: str = typer.Option("biolab.db", "--db", help="Path to SQLite database"),
):
    """Search multiple evidence sources and persist an auditable result set."""
    selected_sources = [source.strip().lower() for source in sources.split(",") if source.strip()]
    conn = _get_conn(db_path)
    try:
        result = evidence_service.search_evidence(
            conn,
            query=query,
            agent_id=agent_id,
            sources=selected_sources,
            max_results=max_results,
        )
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    finally:
        conn.close()

    console.print_json(data=result)
    if result["errors"]:
        console.print("[yellow]Completed with explicit source errors; see errors above.[/yellow]")
        if result["retrieval_count"] == 0:
            raise typer.Exit(1)  # nothing was preserved: scripts must not read this as success


@app.command()
def web(
    host: str = typer.Option("127.0.0.1", "--host", help="Interface to bind"),
    port: int = typer.Option(8000, "--port", help="Port to bind"),
    db_path: str = typer.Option("biolab.db", "--db", help="Path to SQLite database"),
    require_auth: bool = typer.Option(False, "--require-auth", help="Require a Biolab API key"),
):
    """Run the local evidence dashboard and REST API."""
    import uvicorn

    from biolab.web import create_app

    try:
        auth.check_exposure(
            host, {**os.environ, "BIOLAB_REQUIRE_AUTH": "true" if require_auth else ""}
        )
    except SystemExit as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"[green]Biolab dashboard:[/green] http://{host}:{port}")
    uvicorn.run(create_app(db_path, require_auth=require_auth), host=host, port=port)


@app.command()
def search(
    query: str = typer.Argument(..., help="Search query (sent to PubMed verbatim)"),
    agent_id: str = typer.Option("cli:user", "--agent", "-a", help="Agent identifier"),
    max_results: int = typer.Option(5, "--max", "-n", help="Max papers to retrieve (1-50)"),
    db_path: str = typer.Option("biolab.db", "--db", help="Path to SQLite database"),
):
    """Search PubMed and log retrievals to the audit trail."""
    from biolab.pubmed_client import paper_to_retrieval_input, search_and_fetch

    _require_text(query, "query")
    if not 1 <= max_results <= 50:
        console.print("[red]max_results must be between 1 and 50[/red]")
        raise typer.Exit(1)

    conn = _get_conn(db_path)
    console.print(f"[cyan]Searching PubMed:[/cyan] {query}")

    papers = search_and_fetch(query, max_results)
    if not papers:
        console.print("[yellow]No results found[/yellow]")
        return

    console.print(f"[green]Found {len(papers)} papers[/green]")

    for paper in papers:
        retrieval_input = paper_to_retrieval_input(paper)
        record = retrieval_log.write_retrieval(
            conn,
            query_text=query,
            external_id=retrieval_input["external_id"],
            agent_id=agent_id,
            source=retrieval_input["source"],
            source_metadata=retrieval_input["source_metadata"],
            raw_response=retrieval_input["raw_response"],
            snapshot=retrieval_input["snapshot"],
        )
        console.print(f"  [bold]{paper.pmid}[/bold] → retrieval_id: [dim]{record.retrieval_id}[/dim]")
        console.print(f"    Title: {paper.title[:80]}...")


@app.command(name="search-europepmc")
def search_europepmc(
    query: str = typer.Argument(..., help="Search query (sent to Europe PMC verbatim)"),
    agent_id: str = typer.Option("cli:user", "--agent", "-a", help="Agent identifier"),
    max_results: int = typer.Option(5, "--max", "-n", help="Max articles to retrieve (1-50)"),
    db_path: str = typer.Option("biolab.db", "--db", help="Path to SQLite database"),
):
    """Search Europe PMC and log retrievals to the audit trail."""
    from biolab import europepmc_client

    _require_text(query, "query")
    if not 1 <= max_results <= 50:
        console.print("[red]max_results must be between 1 and 50[/red]")
        raise typer.Exit(1)

    conn = _get_conn(db_path)
    console.print(f"[cyan]Searching Europe PMC:[/cyan] {query}")

    articles = europepmc_client.search_and_fetch(query, max_results)
    if not articles:
        console.print("[yellow]No results found[/yellow]")
        return

    console.print(f"[green]Found {len(articles)} articles[/green]")

    for article in articles:
        retrieval_input = europepmc_client.paper_to_retrieval_input(article)
        record = retrieval_log.write_retrieval(
            conn,
            query_text=query,
            external_id=retrieval_input["external_id"],
            agent_id=agent_id,
            source=retrieval_input["source"],
            source_metadata=retrieval_input["source_metadata"],
            raw_response=retrieval_input["raw_response"],
            snapshot=retrieval_input["snapshot"],
        )
        console.print(f"  [bold]{article.id}[/bold] → retrieval_id: [dim]{record.retrieval_id}[/dim]")
        console.print(f"    Title: {article.title[:80]}...")


@app.command(name="search-clinicaltrials")
def search_clinicaltrials(
    query: str = typer.Argument(..., help="Condition/disease search query"),
    agent_id: str = typer.Option("cli:user", "--agent", "-a", help="Agent identifier"),
    max_results: int = typer.Option(5, "--max", "-n", help="Max studies to retrieve (1-50)"),
    db_path: str = typer.Option("biolab.db", "--db", help="Path to SQLite database"),
):
    """Search ClinicalTrials.gov and log retrievals to the audit trail."""
    from biolab import clinicaltrials_client

    _require_text(query, "query")
    if not 1 <= max_results <= 50:
        console.print("[red]max_results must be between 1 and 50[/red]")
        raise typer.Exit(1)

    conn = _get_conn(db_path)
    console.print(f"[cyan]Searching ClinicalTrials.gov:[/cyan] {query}")

    studies = clinicaltrials_client.search_and_fetch(query, max_results)
    if not studies:
        console.print("[yellow]No results found[/yellow]")
        return

    console.print(f"[green]Found {len(studies)} studies[/green]")

    for study in studies:
        retrieval_input = clinicaltrials_client.paper_to_retrieval_input(study)
        record = retrieval_log.write_retrieval(
            conn,
            query_text=query,
            external_id=retrieval_input["external_id"],
            agent_id=agent_id,
            source=retrieval_input["source"],
            source_metadata=retrieval_input["source_metadata"],
            raw_response=retrieval_input["raw_response"],
            snapshot=retrieval_input["snapshot"],
        )
        console.print(f"  [bold]{study.nct_id}[/bold] → retrieval_id: [dim]{record.retrieval_id}[/dim]")
        console.print(f"    Title: {study.brief_title[:80]}...")
        console.print(f"    Status: {study.overall_status} | Phase: {study.phase}")


@app.command(name="search-biorxiv")
def search_biorxiv(
    category: str = typer.Argument(..., help='Category (e.g. "neuroscience") or "all"'),
    agent_id: str = typer.Option("cli:user", "--agent", "-a", help="Agent identifier"),
    max_results: int = typer.Option(5, "--max", "-n", help="Max preprints to retrieve (1-50)"),
    server: str = typer.Option("biorxiv", "--server", help='"biorxiv" or "medrxiv"'),
    db_path: str = typer.Option("biolab.db", "--db", help="Path to SQLite database"),
):
    """List bioRxiv/medRxiv preprints by category and log retrievals to the audit trail.

    No free-text search exists on this API — only category listing (last 30 days).
    """
    from biolab import biorxiv_client

    _require_text(category, "category")
    if not 1 <= max_results <= 50:
        console.print("[red]max_results must be between 1 and 50[/red]")
        raise typer.Exit(1)
    if server not in ("biorxiv", "medrxiv"):
        console.print('[red]server must be "biorxiv" or "medrxiv"[/red]')
        raise typer.Exit(1)

    conn = _get_conn(db_path)
    console.print(f"[cyan]Searching {server} (category: {category}):[/cyan]")

    preprints = biorxiv_client.list_and_fetch(server, category, max_results)
    if not preprints:
        console.print("[yellow]No results found[/yellow]")
        return

    console.print(f"[green]Found {len(preprints)} preprints[/green]")

    query_text = f"category:{category}"
    for preprint in preprints:
        retrieval_input = biorxiv_client.paper_to_retrieval_input(preprint, server)
        record = retrieval_log.write_retrieval(
            conn,
            query_text=query_text,
            external_id=retrieval_input["external_id"],
            agent_id=agent_id,
            source=retrieval_input["source"],
            source_metadata=retrieval_input["source_metadata"],
            raw_response=retrieval_input["raw_response"],
            snapshot=retrieval_input["snapshot"],
        )
        console.print(f"  [bold]{preprint.doi}[/bold] → retrieval_id: [dim]{record.retrieval_id}[/dim]")
        console.print(f"    Title: {preprint.title[:80]}...")
        console.print(f"    Category: {preprint.category} | Date: {preprint.date}")


@app.command()
def get(
    retrieval_id: str = typer.Argument(..., help="Retrieval ID to look up"),
    db_path: str = typer.Option("biolab.db", "--db", help="Path to SQLite database"),
    raw: bool = typer.Option(False, "--raw", help="Show raw XML response"),
    snapshot: bool = typer.Option(True, "--snapshot/--no-snapshot", help="Show parsed snapshot"),
):
    """Retrieve a full retrieval record by its retrieval_id."""
    conn = _get_conn(db_path)
    record = retrieval_log.get_retrieval(conn, retrieval_id)

    if record is None:
        console.print(f"[red]No retrieval found for id: {retrieval_id}[/red]")
        raise typer.Exit(1)

    console.print(f"[bold]Retrieval ID:[/bold] {record.retrieval_id}")
    console.print(f"[bold]Source:[/bold] {record.source}")
    console.print(f"[bold]External ID:[/bold] {record.external_id}")
    console.print(f"[bold]Query:[/bold] {record.query_text}")
    console.print(f"[bold]Retrieved at:[/bold] {record.retrieved_at}")
    console.print(f"[bold]Agent:[/bold] {record.agent_id}")

    if snapshot:
        snap = json.loads(record.snapshot)
        console.print("\n[bold cyan]Snapshot:[/bold cyan]")
        console.print(f"  Title: {snap.get('title', 'N/A')}")
        console.print(f"  Abstract: {snap.get('abstract', 'N/A')[:200]}...")
        console.print(f"  DOI: {snap.get('doi', 'N/A')}")
        console.print(f"  Journal: {snap.get('journal', {}).get('title', 'N/A')}")
        console.print(f"  Authors: {len(snap.get('authors', []))} authors")
        console.print(f"  Pub Types: {', '.join(snap.get('publication_types', [])) or 'N/A'}")
        console.print(f"  MeSH Terms: {len(snap.get('mesh_terms', []))} terms")

    source_meta = json.loads(record.source_metadata)
    console.print(f"\n[bold]Source Metadata:[/bold] {json.dumps(source_meta)}")
    console.print(f"[bold]Response Hash:[/bold] {record.response_hash}")

    if raw:
        console.print("\n[bold cyan]Raw XML:[/bold cyan]")
        syntax = Syntax(record.raw_response, "xml", theme="monokai", line_numbers=True)
        console.print(syntax)


@app.command()
def list(
    agent_id: str = typer.Option(None, "--agent", "-a", help="Filter by agent ID"),
    source: str = typer.Option(None, "--source", "-s", help="Filter by source (pubmed, etc.)"),
    limit: int = typer.Option(20, "--limit", "-n", help="Max records to show"),
    db_path: str = typer.Option("biolab.db", "--db", help="Path to SQLite database"),
):
    """List recent retrieval records."""
    conn = _get_conn(db_path)

    where_clauses = []
    params: builtins.list[str | int] = []
    if agent_id:
        where_clauses.append("agent_id = ?")
        params.append(agent_id)
    if source:
        where_clauses.append("source = ?")
        params.append(source)

    where = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""
    params.append(limit)

    rows = conn.execute(
        f"""
        SELECT retrieval_id, source, external_id, query_text, retrieved_at, agent_id
        FROM retrievals {where}
        ORDER BY retrieved_at DESC LIMIT ?
        """,
        params,
    ).fetchall()

    if not rows:
        console.print("[yellow]No records found[/yellow]")
        return

    table = Table(title="Retrieval Records")
    table.add_column("Retrieval ID", style="dim")
    table.add_column("Source")
    table.add_column("External ID")
    table.add_column("Query", max_width=40)
    table.add_column("Retrieved At")
    table.add_column("Agent")

    for row in rows:
        table.add_row(
            row[0][:8] + "...",
            row[1],
            row[2],
            row[3][:40] + ("..." if len(row[3]) > 40 else ""),
            row[4][:19].replace("T", " "),
            row[5],
        )

    console.print(table)


@app.command()
def export(
    output: Path = typer.Argument(..., help="Output JSONL file"),
    agent_id: str = typer.Option(None, "--agent", "-a", help="Filter by agent ID"),
    source: str = typer.Option(None, "--source", "-s", help="Filter by source"),
    db_path: str = typer.Option("biolab.db", "--db", help="Path to SQLite database"),
):
    """Export retrieval records to JSONL for analysis."""
    conn = _get_conn(db_path)

    where_clauses = []
    params = []
    if agent_id:
        where_clauses.append("agent_id = ?")
        params.append(agent_id)
    if source:
        where_clauses.append("source = ?")
        params.append(source)

    where = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

    rows = conn.execute(
        f"""
        SELECT retrieval_id, source, external_id, query_text, retrieved_at,
               agent_id, source_metadata, raw_response, snapshot, response_hash, prev_hash
        FROM retrievals {where}
        ORDER BY rowid ASC
        """,
        params,
    ).fetchall()

    with output.open("w") as f:
        for row in rows:
            record = {
                "retrieval_id": row[0],
                "source": row[1],
                "external_id": row[2],
                "query_text": row[3],
                "retrieved_at": row[4],
                "agent_id": row[5],
                "source_metadata": json.loads(row[6]),
                "snapshot": json.loads(row[8]),
                "response_hash": row[9],
                "prev_hash": row[10],
                "raw_response": row[7],
            }
            f.write(json.dumps(record) + "\n")

    console.print(f"[green]Exported {len(rows)} records to {output}[/green]")


@app.command()
def demo(
    query: str = typer.Option("BRCA1 pancreatic cancer", "--query", "-q", help="Demo query"),
    agent_id: str = typer.Option("demo:user", "--agent", "-a", help="Agent ID for demo"),
    db_path: str = typer.Option("biolab.db", "--db", help="Path to SQLite database"),
):
    """Run the full demo: search → show retrieval_id → get full record."""
    from biolab.pubmed_client import paper_to_retrieval_input, search_and_fetch

    console.print("[bold cyan]═══ BIOLAB DEMO ═══[/bold cyan]")
    console.print(f"Query: [bold]{query}[/bold]")
    console.print(f"Agent: [bold]{agent_id}[/bold]\n")

    conn = _get_conn(db_path)

    # Step 1: Search
    console.print("[cyan]Step 1: Search PubMed[/cyan]")
    papers = search_and_fetch(query, 2)
    if not papers:
        console.print("[yellow]No results[/yellow]")
        return

    console.print(f"Found [bold]{len(papers)}[/bold] papers\n")

    # Step 2: Store and show retrieval_ids
    console.print("[cyan]Step 2: Store in audit trail (each paper gets a retrieval_id)[/cyan]")
    retrieval_ids = []
    for paper in papers:
        retrieval_input = paper_to_retrieval_input(paper)
        record = retrieval_log.write_retrieval(
            conn,
            query_text=query,
            external_id=retrieval_input["external_id"],
            agent_id=agent_id,
            source=retrieval_input["source"],
            source_metadata=retrieval_input["source_metadata"],
            raw_response=retrieval_input["raw_response"],
            snapshot=retrieval_input["snapshot"],
        )
        retrieval_ids.append(record.retrieval_id)
        console.print(f"  PMID {paper.pmid} → [bold green]{record.retrieval_id}[/bold green]")
        console.print(f"    Title: {paper.title[:70]}...")

    # Step 3: Retrieve full record by retrieval_id
    console.print("\n[cyan]Step 3: Retrieve full audit record by retrieval_id[/cyan]")
    for rid in retrieval_ids:
        console.print(f"\n  [bold]Retrieval ID:[/bold] {rid}")
        fetched_record = retrieval_log.get_retrieval(conn, rid)
        if fetched_record:
            snap = json.loads(fetched_record.snapshot)
            console.print(f"  Title: {snap.get('title')}")
            console.print(f"  DOI: {snap.get('doi')}")
            console.print(f"  Journal: {snap.get('journal', {}).get('title')}")
            console.print(f"  Retrieved: {fetched_record.retrieved_at}")
            console.print(f"  Source: {fetched_record.source}")
            console.print(f"  Hash: {fetched_record.response_hash[:16]}...")

    console.print("\n[bold green]✓ Demo complete[/bold green]")
    console.print("Each retrieval_id creates an inspectable link from conclusion → raw source.")


@app.command()
def verify(
    db_path: str = typer.Option("biolab.db", "--db", help="Database to verify"),
):
    """Verify the stored payload hash chain; exit nonzero on corruption."""
    conn = db.connect(db_path)
    try:
        ok, broken_id = retrieval_log.verify_chain(conn)
        count = conn.execute("SELECT count(*) FROM retrievals").fetchone()[0]
    finally:
        conn.close()
    if not ok:
        console.print(f"[red]Chain broken at {broken_id}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Chain valid: {count} records[/green]")


def _anchor_key() -> bytes:
    import os

    key = os.environ.get("BIOLAB_ANCHOR_KEY", "")
    if not key:
        console.print("[red]Set BIOLAB_ANCHOR_KEY to sign and check anchors.[/red]")
        raise typer.Exit(2)
    return key.encode("utf-8")


@app.command()
def anchor(
    out: Path = typer.Argument(..., help="File to write the signed chain-head checkpoint to"),
    db_path: str = typer.Option("biolab.db", "--db", help="Database to checkpoint"),
):
    """Write a signed checkpoint of the chain head. Store it outside the database host."""
    key = _anchor_key()
    conn = db.connect(db_path)
    try:
        ok, broken_id = retrieval_log.verify_chain(conn)
        if not ok:
            console.print(f"[red]Refusing to anchor a broken chain (at {broken_id})[/red]")
            raise typer.Exit(1)
        checkpoint = retrieval_log.make_anchor(conn, key)
    finally:
        conn.close()
    out.write_text(json.dumps(checkpoint, indent=2) + "\n")
    console.print(f"[green]Anchored {checkpoint['count']} records -> {out}[/green]")


@app.command(name="verify-anchor")
def verify_anchor_cmd(
    anchor_file: Path = typer.Argument(..., help="Checkpoint written by `biolab anchor`"),
    db_path: str = typer.Option("biolab.db", "--db", help="Database to check"),
):
    """Check the chain, and that it still extends a previously saved checkpoint."""
    key = _anchor_key()
    checkpoint = json.loads(anchor_file.read_text())
    conn = db.connect(db_path)
    try:
        chain_ok, broken_id = retrieval_log.verify_chain(conn)
        anchor_ok, reason = retrieval_log.verify_anchor(conn, checkpoint, key)
    finally:
        conn.close()
    if not chain_ok:
        console.print(f"[red]Chain broken at {broken_id}[/red]")
    if not anchor_ok:
        console.print(f"[red]Anchor check failed: {reason}[/red]")
    if not (chain_ok and anchor_ok):
        raise typer.Exit(1)
    console.print(f"[green]OK: {reason}[/green]")


@app.command(name="portfolio-demo")
def portfolio_demo():
    """Run an isolated, offline provenance and tamper-detection demonstration."""
    from biolab.demo import run_demo

    result = run_demo()
    console.print("[bold cyan]Biolab / evidence provenance[/bold cyan]")
    console.print("Synthetic teaching data; no scientific claims or network requests.")
    console.print_json(data=result)
    console.print("[green]PASS: records persisted, retrieved, and verified; tampering detected.[/green]")


executions_app = typer.Typer(help="Inspect, verify and replay recorded tool executions.")
app.add_typer(executions_app, name="executions")


def _artifact_store(path: str | None):
    from biolab.artifacts import ArtifactStore

    default = os.environ.get("BIOLAB_ARTIFACT_DIR") or "biolab_artifacts"
    return ArtifactStore(path if path else default)


ARTIFACTS_OPTION = typer.Option(
    None, "--artifacts", help="Artifact directory (default: $BIOLAB_ARTIFACT_DIR or ./biolab_artifacts)"
)


@app.command(name="tools")
def list_tools():
    """List runnable execution tools with pinned versions."""
    from biolab import tools

    registry = tools.default_registry()
    table = Table(title="Execution tools")
    for column in ("Name", "Version", "Container", "Device", "Deterministic"):
        table.add_column(column)
    for spec in registry.tools.values():
        table.add_row(spec.name, spec.version, spec.container, spec.device, str(spec.deterministic))
    console.print(table)


@app.command(name="run-tool")
def run_tool(
    tool: str = typer.Argument(..., help="Tool name (see `biolab tools`)"),
    input_json: str = typer.Option("{}", "--input", "-i", help="Tool inputs as a JSON object"),
    input_file: Path = typer.Option(None, "--input-file", help="Read the JSON inputs from a file"),
    agent_id: str = typer.Option("cli:user", "--agent", "-a", help="Agent identifier"),
    retrieval_ids: builtins.list[str] = typer.Option(
        [], "--retrieval", "-r", help="Retrieval id this run derives from (repeatable)"
    ),
    db_path: str = typer.Option("biolab.db", "--db", help="Path to SQLite database"),
    artifacts: str = ARTIFACTS_OPTION,
):
    """Run a tool and seal an ExecutionRecord (inputs, versions, output hashes)."""
    from biolab import executions, tools

    try:
        inputs = json.loads(input_file.read_text() if input_file else input_json)
    except json.JSONDecodeError as exc:
        console.print(f"[red]inputs are not valid JSON: {exc}[/red]")
        raise typer.Exit(1) from exc
    if not isinstance(inputs, dict):
        console.print("[red]inputs must be a JSON object[/red]")
        raise typer.Exit(1)
    conn = db.connect(db_path)
    try:
        record = executions.execute(
            conn, _artifact_store(artifacts), tools.default_registry(), tool, inputs,
            agent_id, retrieval_ids,
        )
    finally:
        conn.close()
    console.print_json(data=record)


@executions_app.command("list")
def executions_list(
    tool: str = typer.Option(None, "--tool", help="Filter by tool name"),
    limit: int = typer.Option(20, "--limit", "-n"),
    db_path: str = typer.Option("biolab.db", "--db"),
):
    """List recorded executions, newest first."""
    from biolab import executions

    conn = db.connect(db_path)
    try:
        rows = executions.list_executions(conn, tool, limit)
    finally:
        conn.close()
    if not rows:
        console.print("[yellow]No executions recorded[/yellow]")
        return
    table = Table(title="Executions")
    for column in ("Execution", "Tool", "Version", "Status", "Agent", "Finished"):
        table.add_column(column)
    for row in rows:
        table.add_row(
            row["execution_id"][:8] + "...", row["tool"], row["tool_version"], row["status"],
            row["agent_id"], row["finished_at"][:19].replace("T", " "),
        )
    console.print(table)


@executions_app.command("show")
def executions_show(
    execution_id: str,
    db_path: str = typer.Option("biolab.db", "--db"),
):
    """Show one execution record in full."""
    from biolab import executions

    conn = db.connect(db_path)
    try:
        record = executions.get_execution(conn, execution_id)
    finally:
        conn.close()
    if record is None:
        console.print(f"[red]No execution found for id: {execution_id}[/red]")
        raise typer.Exit(1)
    console.print_json(data=record)


@executions_app.command("verify")
def executions_verify(
    db_path: str = typer.Option("biolab.db", "--db"),
    artifacts: str = ARTIFACTS_OPTION,
):
    """Verify the execution chain and every output artifact; exit nonzero on corruption."""
    from biolab import executions

    conn = db.connect(db_path)
    try:
        ok, broken = executions.verify_executions(conn, _artifact_store(artifacts))
        count = conn.execute("SELECT count(*) FROM executions").fetchone()[0]
    finally:
        conn.close()
    if not ok:
        console.print(f"[red]Execution {broken} failed verification (record or artifact)[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Executions valid: {count} records[/green]")


@executions_app.command("replay")
def executions_replay(
    execution_id: str,
    db_path: str = typer.Option("biolab.db", "--db"),
    artifacts: str = ARTIFACTS_OPTION,
):
    """Re-run a recorded execution and report whether its outputs reproduced."""
    from biolab import executions, tools

    conn = db.connect(db_path)
    try:
        result = executions.replay(
            conn, _artifact_store(artifacts), tools.default_registry(), execution_id
        )
    finally:
        conn.close()
    console.print_json(data=result)
    if not result["reproduced"]:
        raise typer.Exit(1)


@executions_app.command("artifact")
def executions_artifact(
    sha256: str,
    out: Path = typer.Option(None, "--out", "-o", help="Write bytes to a file instead of stdout"),
    artifacts: str = ARTIFACTS_OPTION,
):
    """Fetch a stored output artifact by sha256, verifying it on read."""
    try:
        data = _artifact_store(artifacts).get(sha256)
    except (OSError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    if out:
        out.write_bytes(data)
        console.print(f"[green]Wrote {len(data)} bytes to {out}[/green]")
    else:
        console.print(data.decode("utf-8", errors="replace"), markup=False)


def main() -> None:
    """Console entry point: user mistakes print one clean line instead of a traceback."""
    try:
        app()
    except (ValueError, KeyError) as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
