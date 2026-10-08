package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"

	"github.com/jmoiron/sqlx"
	"github.com/spf13/cobra"
	"github.com/srikarjy/biolab-mcp/go-biolab/internal/biorxiv"
	"github.com/srikarjy/biolab-mcp/go-biolab/internal/clinicaltrials"
	"github.com/srikarjy/biolab-mcp/go-biolab/internal/db"
	"github.com/srikarjy/biolab-mcp/go-biolab/internal/europepmc"
	"github.com/srikarjy/biolab-mcp/go-biolab/internal/models"
	"github.com/srikarjy/biolab-mcp/go-biolab/internal/pubmed"
	"github.com/srikarjy/biolab-mcp/go-biolab/internal/retrieval"
)

var (
	dbPath     string
	agentID    string
	maxResults int
)

func main() {
	rootCmd := &cobra.Command{
		Use:   "biolab",
		Short: "Biolab MCP Server CLI — query and explore the retrieval audit trail",
		PersistentPreRunE: func(cmd *cobra.Command, args []string) error {
			return nil
		},
	}

	rootCmd.PersistentFlags().StringVar(&dbPath, "db", "biolab.db", "Path to SQLite database")
	rootCmd.PersistentFlags().StringVar(&agentID, "agent", "cli:user", "Agent identifier")

	// search command
	searchCmd := &cobra.Command{
		Use:   "search [query]",
		Short: "Search PubMed and log retrievals to the audit trail",
		Args:  cobra.ExactArgs(1),
		RunE:  runSearch,
	}
	searchCmd.Flags().IntVarP(&maxResults, "max", "n", 5, "Max papers to retrieve (1-50)")
	rootCmd.AddCommand(searchCmd)

	// search-europepmc command
	searchEPMCCmd := &cobra.Command{
		Use:   "search-europepmc [query]",
		Short: "Search Europe PMC and log retrievals to the audit trail",
		Args:  cobra.ExactArgs(1),
		RunE:  runSearchEuropePMC,
	}
	searchEPMCCmd.Flags().IntVarP(&maxResults, "max", "n", 5, "Max papers to retrieve (1-50)")
	rootCmd.AddCommand(searchEPMCCmd)

	// search-clinicaltrials command
	searchCTCmd := &cobra.Command{
		Use:   "search-clinicaltrials [query]",
		Short: "Search ClinicalTrials.gov and log retrievals to the audit trail",
		Args:  cobra.ExactArgs(1),
		RunE:  runSearchClinicalTrials,
	}
	searchCTCmd.Flags().IntVarP(&maxResults, "max", "n", 5, "Max studies to retrieve (1-50)")
	rootCmd.AddCommand(searchCTCmd)

	// search-biorxiv command
	searchBioRxivCmd := &cobra.Command{
		Use:   "search-biorxiv [category]",
		Short: "List bioRxiv/medRxiv preprints by category (no free-text search)",
		Args:  cobra.ExactArgs(1),
		RunE:  runSearchBioRxiv,
	}
	searchBioRxivCmd.Flags().String("server", "biorxiv", "Server: biorxiv or medrxiv")
	searchBioRxivCmd.Flags().IntVarP(&maxResults, "max", "n", 5, "Max preprints to retrieve (1-50)")
	rootCmd.AddCommand(searchBioRxivCmd)

	// get command
	getCmd := &cobra.Command{
		Use:   "get [retrieval_id]",
		Short: "Retrieve a full retrieval record by its retrieval_id",
		Args:  cobra.ExactArgs(1),
		RunE:  runGet,
	}
	getCmd.Flags().Bool("raw", false, "Show raw XML response")
	getCmd.Flags().Bool("snapshot", true, "Show parsed snapshot")
	rootCmd.AddCommand(getCmd)

	// list command
	listCmd := &cobra.Command{
		Use:   "list",
		Short: "List recent retrieval records",
		RunE:  runList,
	}
	listCmd.Flags().String("agent", "", "Filter by agent ID")
	listCmd.Flags().String("source", "", "Filter by source")
	listCmd.Flags().Int("limit", 20, "Max records to show")
	rootCmd.AddCommand(listCmd)

	// export command
	exportCmd := &cobra.Command{
		Use:   "export [output.jsonl]",
		Short: "Export retrieval records to JSONL for analysis",
		Args:  cobra.ExactArgs(1),
		RunE:  runExport,
	}
	exportCmd.Flags().String("agent", "", "Filter by agent ID")
	exportCmd.Flags().String("source", "", "Filter by source")
	rootCmd.AddCommand(exportCmd)

	// demo command
	demoCmd := &cobra.Command{
		Use:   "demo",
		Short: "Run the full demo: search → show retrieval_id → get full record",
		RunE:  runDemo,
	}
	demoCmd.Flags().String("query", "BRCA1 pancreatic cancer", "Demo query")
	demoCmd.Flags().String("agent", "demo:user", "Agent ID for demo")
	rootCmd.AddCommand(demoCmd)

	verifyCmd := &cobra.Command{
		Use:   "verify",
		Short: "Verify the retrieval hash chain",
		Args:  cobra.NoArgs,
		RunE:  runVerify,
	}
	rootCmd.AddCommand(verifyCmd)

	anchorCmd := &cobra.Command{
		Use:   "anchor [out.json]",
		Short: "Write a signed chain-head checkpoint (needs BIOLAB_ANCHOR_KEY)",
		Args:  cobra.ExactArgs(1),
		RunE:  runAnchor,
	}
	rootCmd.AddCommand(anchorCmd)

	verifyAnchorCmd := &cobra.Command{
		Use:   "verify-anchor [anchor.json]",
		Short: "Check the chain and that it still extends a saved checkpoint",
		Args:  cobra.ExactArgs(1),
		RunE:  runVerifyAnchor,
	}
	rootCmd.AddCommand(verifyAnchorCmd)

	if err := rootCmd.Execute(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func getDB() (*sqlx.DB, error) {
	return db.Connect(dbPath)
}

func runSearch(cmd *cobra.Command, args []string) error {
	query := args[0]
	if maxResults < 1 || maxResults > 50 {
		return fmt.Errorf("max_results must be between 1 and 50")
	}

	database, err := getDB()
	if err != nil {
		return err
	}
	defer func() { _ = database.Close() }()

	client := pubmed.NewClient()
	log := retrieval.NewLog(database)
	defer func() { _ = log.Close() }()

	ctx := context.Background()
	fmt.Printf("Searching PubMed: %s\n", query)

	papers, err := client.SearchAndFetch(ctx, query, maxResults)
	if err != nil {
		return err
	}
	if len(papers) == 0 {
		fmt.Println("No results found")
		return nil
	}

	fmt.Printf("Found %d papers\n", len(papers))
	for _, paper := range papers {
		snapshot := retrieval.BuildSnapshotFromPubMed(paper)
		sourceMeta := retrieval.BuildSourceMetadata(paper)

		record := retrieval.BuildRecord(
			query,
			paper.PMID,
			agentID,
			"pubmed",
			sourceMeta,
			paper.RawXML,
			snapshot,
		)

		if err := log.Write(ctx, record); err != nil {
			return err
		}

		fmt.Printf("  PMID %s → retrieval_id: %s\n", paper.PMID, record.RetrievalID)
		fmt.Printf("    Title: %s...\n", truncate(paper.Title, 80))
	}
	return nil
}

func runSearchEuropePMC(cmd *cobra.Command, args []string) error {
	query := args[0]
	if maxResults < 1 || maxResults > 50 {
		return fmt.Errorf("max_results must be between 1 and 50")
	}

	database, err := getDB()
	if err != nil {
		return err
	}
	defer func() { _ = database.Close() }()

	client := europepmc.NewClient()
	log := retrieval.NewLog(database)
	defer func() { _ = log.Close() }()

	ctx := context.Background()
	fmt.Printf("Searching Europe PMC: %s\n", query)

	articles, err := client.SearchAndFetch(ctx, query, maxResults)
	if err != nil {
		return err
	}
	if len(articles) == 0 {
		fmt.Println("No results found")
		return nil
	}

	fmt.Printf("Found %d articles\n", len(articles))
	for _, article := range articles {
		externalID := article.PMID
		if externalID == "" {
			externalID = article.DOI
		}
		if externalID == "" {
			externalID = article.ID
		}

		record := europepmc.BuildRecord(query, externalID, agentID, "europepmc", article)

		if err := log.Write(ctx, record); err != nil {
			return err
		}

		fmt.Printf("  ID %s → retrieval_id: %s\n", article.ID, record.RetrievalID)
		fmt.Printf("    Title: %s...\n", truncate(article.Title, 80))
	}
	return nil
}

func runSearchClinicalTrials(cmd *cobra.Command, args []string) error {
	query := args[0]
	if maxResults < 1 || maxResults > 50 {
		return fmt.Errorf("max_results must be between 1 and 50")
	}

	database, err := getDB()
	if err != nil {
		return err
	}
	defer func() { _ = database.Close() }()

	client := clinicaltrials.NewClient()
	log := retrieval.NewLog(database)
	defer func() { _ = log.Close() }()

	ctx := context.Background()
	fmt.Printf("Searching ClinicalTrials.gov: %s\n", query)

	studies, err := client.SearchAndFetch(ctx, query, maxResults)
	if err != nil {
		return err
	}
	if len(studies) == 0 {
		fmt.Println("No results found")
		return nil
	}

	fmt.Printf("Found %d studies\n", len(studies))
	for _, study := range studies {
		record := clinicaltrials.BuildRecord(query, study.NCTId, agentID, study)

		if err := log.Write(ctx, record); err != nil {
			return err
		}

		fmt.Printf("  NCT %s → retrieval_id: %s\n", study.NCTId, record.RetrievalID)
		fmt.Printf("    Title: %s...\n", truncate(study.BriefTitle, 80))
		fmt.Printf("    Status: %s | Phase: %s\n", study.OverallStatus, study.Phase)
	}
	return nil
}

func runSearchBioRxiv(cmd *cobra.Command, args []string) error {
	category := args[0]
	server, _ := cmd.Flags().GetString("server")
	if maxResults < 1 || maxResults > 50 {
		return fmt.Errorf("max_results must be between 1 and 50")
	}

	database, err := getDB()
	if err != nil {
		return err
	}
	defer func() { _ = database.Close() }()

	client := biorxiv.NewClient()
	log := retrieval.NewLog(database)
	defer func() { _ = log.Close() }()

	ctx := context.Background()
	fmt.Printf("Searching %s (category: %s):\n", server, category)

	preprints, err := client.ListAndFetch(ctx, server, category, maxResults)
	if err != nil {
		return err
	}
	if len(preprints) == 0 {
		fmt.Println("No results found")
		return nil
	}

	fmt.Printf("Found %d preprints\n", len(preprints))
	for _, preprint := range preprints {
		externalID := preprint.DOI
		if externalID == "" {
			externalID = preprint.Date + "-" + preprint.Category
		}

		record := biorxiv.BuildRecord(
			fmt.Sprintf("category:%s", category),
			externalID,
			agentID,
			server,
			category,
			preprint,
		)

		if err := log.Write(ctx, record); err != nil {
			return err
		}

		fmt.Printf("  DOI %s → retrieval_id: %s\n", preprint.DOI, record.RetrievalID)
		fmt.Printf("    Title: %s...\n", truncate(preprint.Title, 80))
		fmt.Printf("    Category: %s | Date: %s\n", preprint.Category, preprint.Date)
	}
	return nil
}

func runGet(cmd *cobra.Command, args []string) error {
	retrievalID := args[0]
	showRaw, _ := cmd.Flags().GetBool("raw")
	showSnapshot, _ := cmd.Flags().GetBool("snapshot")

	database, err := getDB()
	if err != nil {
		return err
	}
	defer func() { _ = database.Close() }()

	log := retrieval.NewLog(database)
	defer func() { _ = log.Close() }()

	ctx := context.Background()
	record, err := log.Get(ctx, retrievalID)
	if err != nil {
		return err
	}
	if record == nil {
		return fmt.Errorf("no retrieval found for id: %s", retrievalID)
	}

	fmt.Printf("Retrieval ID: %s\n", record.RetrievalID)
	fmt.Printf("Source: %s\n", record.Source)
	fmt.Printf("External ID: %s\n", record.ExternalID)
	fmt.Printf("Query: %s\n", record.QueryText)
	fmt.Printf("Retrieved at: %s\n", record.RetrievedAt[:19])
	fmt.Printf("Agent: %s\n", record.AgentID)

	if showSnapshot {
		var snapshot models.Snapshot
		if err := json.Unmarshal([]byte(record.Snapshot), &snapshot); err != nil {
			return fmt.Errorf("parsing snapshot: %w", err)
		}
		fmt.Printf("\nSnapshot:\n")
		fmt.Printf("  Title: %s\n", snapshot.Title)
		fmt.Printf("  Abstract: %s...\n", truncate(snapshot.Abstract, 200))
		fmt.Printf("  DOI: %s\n", snapshot.DOI)
		fmt.Printf("  Journal: %s\n", snapshot.Journal.Title)
		fmt.Printf("  Authors: %d\n", len(snapshot.Authors))
		fmt.Printf("  Pub Types: %v\n", snapshot.PublicationTypes)
		fmt.Printf("  MeSH Terms: %d\n", len(snapshot.MeshTerms))
	}

	var sourceMeta models.SourceMetadata
	if err := json.Unmarshal([]byte(record.SourceMetadata), &sourceMeta); err != nil {
		return fmt.Errorf("parsing source metadata: %w", err)
	}
	fmt.Printf("\nSource Metadata: %+v\n", sourceMeta)
	fmt.Printf("Response Hash: %s\n", record.ResponseHash[:16]+"...")

	if showRaw {
		fmt.Printf("\nRaw XML:\n%s\n", record.RawResponse)
	}
	return nil
}

func runList(cmd *cobra.Command, args []string) error {
	agentFilter, _ := cmd.Flags().GetString("agent")
	sourceFilter, _ := cmd.Flags().GetString("source")
	limit, _ := cmd.Flags().GetInt("limit")

	database, err := getDB()
	if err != nil {
		return err
	}
	defer func() { _ = database.Close() }()

	log := retrieval.NewLog(database)
	defer func() { _ = log.Close() }()

	ctx := context.Background()
	records, err := log.List(ctx, agentFilter, sourceFilter, limit)
	if err != nil {
		return err
	}

	if len(records) == 0 {
		fmt.Println("No records found")
		return nil
	}

	fmt.Printf("%-12s %-8s %-12s %-40s %-20s %s\n", "RETRIEVAL_ID", "SOURCE", "EXT_ID", "QUERY", "RETRIEVED_AT", "AGENT")
	for _, r := range records {
		fmt.Printf("%-12s %-8s %-12s %-40s %-20s %s\n",
			r.RetrievalID[:12]+"...",
			r.Source,
			r.ExternalID,
			truncate(r.QueryText, 40),
			r.RetrievedAt[:19],
			r.AgentID,
		)
	}
	return nil
}

func runExport(cmd *cobra.Command, args []string) error {
	output := args[0]
	agentFilter, _ := cmd.Flags().GetString("agent")
	sourceFilter, _ := cmd.Flags().GetString("source")

	database, err := getDB()
	if err != nil {
		return err
	}
	defer func() { _ = database.Close() }()

	log := retrieval.NewLog(database)
	defer func() { _ = log.Close() }()

	ctx := context.Background()
	records, err := log.List(ctx, agentFilter, sourceFilter, 10000)
	if err != nil {
		return err
	}

	f, err := os.Create(output)
	if err != nil {
		return err
	}
	defer func() { _ = f.Close() }()

	enc := json.NewEncoder(f)
	for _, r := range records {
		var sourceMeta models.SourceMetadata
		var snapshot models.Snapshot
		if err := json.Unmarshal([]byte(r.SourceMetadata), &sourceMeta); err != nil {
			return fmt.Errorf("parsing source metadata for %s: %w", r.RetrievalID, err)
		}
		if err := json.Unmarshal([]byte(r.Snapshot), &snapshot); err != nil {
			return fmt.Errorf("parsing snapshot for %s: %w", r.RetrievalID, err)
		}

		out := map[string]interface{}{
			"retrieval_id":    r.RetrievalID,
			"source":          r.Source,
			"external_id":     r.ExternalID,
			"query_text":      r.QueryText,
			"retrieved_at":    r.RetrievedAt,
			"agent_id":        r.AgentID,
			"source_metadata": sourceMeta,
			"snapshot":        snapshot,
			"response_hash":   r.ResponseHash,
		}
		if err := enc.Encode(out); err != nil {
			return fmt.Errorf("writing record %s: %w", r.RetrievalID, err)
		}
	}

	fmt.Printf("Exported %d records to %s\n", len(records), output)
	return nil
}

func runDemo(cmd *cobra.Command, args []string) error {
	query, _ := cmd.Flags().GetString("query")
	agent, _ := cmd.Flags().GetString("agent")

	database, err := getDB()
	if err != nil {
		return err
	}
	defer func() { _ = database.Close() }()

	client := pubmed.NewClient()
	log := retrieval.NewLog(database)
	defer func() { _ = log.Close() }()

	ctx := context.Background()

	fmt.Println("═══ BIOLAB DEMO ═══")
	fmt.Printf("Query: %s\n", query)
	fmt.Printf("Agent: %s\n\n", agent)

	fmt.Println("Step 1: Search PubMed")
	papers, err := client.SearchAndFetch(ctx, query, 2)
	if err != nil {
		return err
	}
	if len(papers) == 0 {
		fmt.Println("No results")
		return nil
	}
	fmt.Printf("Found %d papers\n\n", len(papers))

	fmt.Println("Step 2: Store in audit trail (each paper gets a retrieval_id)")
	retrievalIDs := make([]string, 0, len(papers))
	for _, paper := range papers {
		snapshot := retrieval.BuildSnapshotFromPubMed(paper)
		sourceMeta := retrieval.BuildSourceMetadata(paper)

		record := retrieval.BuildRecord(
			query,
			paper.PMID,
			agent,
			"pubmed",
			sourceMeta,
			paper.RawXML,
			snapshot,
		)

		if err := log.Write(ctx, record); err != nil {
			return err
		}
		retrievalIDs = append(retrievalIDs, record.RetrievalID)
		fmt.Printf("  PMID %s → %s\n", paper.PMID, record.RetrievalID)
		fmt.Printf("    Title: %s...\n", truncate(paper.Title, 70))
	}

	fmt.Println("\nStep 3: Retrieve full audit record by retrieval_id")
	for _, rid := range retrievalIDs {
		fmt.Printf("\n  Retrieval ID: %s\n", rid)
		record, err := log.Get(ctx, rid)
		if err != nil {
			return err
		}
		if record == nil {
			continue
		}
		var snapshot models.Snapshot
		if err := json.Unmarshal([]byte(record.Snapshot), &snapshot); err != nil {
			return fmt.Errorf("parsing snapshot for %s: %w", rid, err)
		}
		fmt.Printf("  Title: %s\n", snapshot.Title)
		fmt.Printf("  DOI: %s\n", snapshot.DOI)
		fmt.Printf("  Journal: %s\n", snapshot.Journal.Title)
		fmt.Printf("  Retrieved: %s\n", record.RetrievedAt[:19])
		fmt.Printf("  Source: %s\n", record.Source)
		fmt.Printf("  Hash: %s...\n", record.ResponseHash[:16])
	}

	fmt.Println("\n✓ Demo complete")
	fmt.Println("Each retrieval_id creates an unforgeable link from conclusion → raw source.")
	return nil
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "..."
}

func anchorKey() ([]byte, error) {
	key := os.Getenv("BIOLAB_ANCHOR_KEY")
	if key == "" {
		return nil, fmt.Errorf("set BIOLAB_ANCHOR_KEY to sign and check anchors")
	}
	return []byte(key), nil
}

func runVerify(cmd *cobra.Command, args []string) error {
	database, err := getDB()
	if err != nil {
		return err
	}
	defer database.Close()
	ok, brokenID, err := retrieval.VerifyChain(database)
	if err != nil {
		return err
	}
	if !ok {
		return fmt.Errorf("chain broken at %s", brokenID)
	}
	var count int
	if err := database.Get(&count, "SELECT count(*) FROM retrievals"); err != nil {
		return err
	}
	fmt.Printf("Chain valid: %d records\n", count)
	return nil
}

func runAnchor(cmd *cobra.Command, args []string) error {
	key, err := anchorKey()
	if err != nil {
		return err
	}
	database, err := getDB()
	if err != nil {
		return err
	}
	defer database.Close()
	ok, brokenID, err := retrieval.VerifyChain(database)
	if err != nil {
		return err
	}
	if !ok {
		return fmt.Errorf("refusing to anchor a broken chain (at %s)", brokenID)
	}
	a, err := retrieval.MakeAnchor(database, key)
	if err != nil {
		return err
	}
	out, _ := json.MarshalIndent(a, "", "  ")
	if err := os.WriteFile(args[0], append(out, '\n'), 0o644); err != nil {
		return err
	}
	fmt.Printf("Anchored %d records -> %s\n", a.Count, args[0])
	return nil
}

func runVerifyAnchor(cmd *cobra.Command, args []string) error {
	key, err := anchorKey()
	if err != nil {
		return err
	}
	raw, err := os.ReadFile(args[0])
	if err != nil {
		return err
	}
	var a retrieval.Anchor
	if err := json.Unmarshal(raw, &a); err != nil {
		return fmt.Errorf("malformed anchor: %w", err)
	}
	database, err := getDB()
	if err != nil {
		return err
	}
	defer database.Close()
	chainOK, brokenID, err := retrieval.VerifyChain(database)
	if err != nil {
		return err
	}
	anchorOK, reason, err := retrieval.VerifyAnchor(database, a, key)
	if err != nil {
		return err
	}
	if !chainOK {
		return fmt.Errorf("chain broken at %s", brokenID)
	}
	if !anchorOK {
		return fmt.Errorf("anchor check failed: %s", reason)
	}
	fmt.Printf("OK: %s\n", reason)
	return nil
}
