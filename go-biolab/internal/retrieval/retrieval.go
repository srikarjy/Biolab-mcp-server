package retrieval

import (
	"context"
	"database/sql"
	"encoding/json"
	"sync"
	"time"

	"github.com/jmoiron/sqlx"
	"github.com/srikarjy/biolab-mcp/go-biolab/internal/models"
)

type WriteRequest struct {
	Record   models.RetrievalRecord
	Response chan error
}

type Log struct {
	db         *sqlx.DB
	writeQueue chan WriteRequest
	wg         sync.WaitGroup
	ctx        context.Context
	cancel     context.CancelFunc
}

func NewLog(db *sqlx.DB) *Log {
	ctx, cancel := context.WithCancel(context.Background())
	l := &Log{
		db:         db,
		writeQueue: make(chan WriteRequest, 1000),
		ctx:        ctx,
		cancel:     cancel,
	}
	l.wg.Add(1)
	go l.writerLoop()
	return l
}

func (l *Log) writerLoop() {
	defer l.wg.Done()
	for {
		select {
		case <-l.ctx.Done():
			return
		case req := <-l.writeQueue:
			err := l.writeSync(req.Record)
			req.Response <- err
		}
	}
}

// writeSync chains and inserts one record. BEGIN IMMEDIATE takes the write lock before
// the head is read, so no other writer (goroutine or process) can interleave between
// reading the previous hash and inserting the next row.
func (l *Log) writeSync(record models.RetrievalRecord) (err error) {
	ctx := context.Background()
	conn, err := l.db.Conn(ctx)
	if err != nil {
		return err
	}
	defer conn.Close()
	if _, err = conn.ExecContext(ctx, "BEGIN IMMEDIATE"); err != nil {
		return err
	}
	defer func() {
		if err != nil {
			_, _ = conn.ExecContext(ctx, "ROLLBACK")
		}
	}()

	var head string
	row := conn.QueryRowContext(ctx, "SELECT response_hash FROM retrievals ORDER BY rowid DESC LIMIT 1")
	if scanErr := row.Scan(&head); scanErr != nil && scanErr != sql.ErrNoRows {
		return scanErr
	}
	record.PrevHash = head
	record.HashVersion = HashVersion
	record.ResponseHash, err = ComputeHash(record)
	if err != nil {
		return err
	}
	if _, err = conn.ExecContext(ctx, `
		INSERT INTO retrievals
			(retrieval_id, source, external_id, query_text, retrieved_at,
			 agent_id, source_metadata, raw_response, snapshot, response_hash,
			 prev_hash, hash_version)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
	`, record.RetrievalID, record.Source, record.ExternalID, record.QueryText,
		record.RetrievedAt, record.AgentID, record.SourceMetadata,
		record.RawResponse, record.Snapshot, record.ResponseHash,
		record.PrevHash, record.HashVersion); err != nil {
		return err
	}
	_, err = conn.ExecContext(ctx, "COMMIT")
	return err
}

func (l *Log) Write(ctx context.Context, record models.RetrievalRecord) error {
	resp := make(chan error, 1)
	select {
	case l.writeQueue <- WriteRequest{Record: record, Response: resp}:
		select {
		case err := <-resp:
			return err
		case <-ctx.Done():
			return ctx.Err()
		}
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (l *Log) Get(ctx context.Context, retrievalID string) (*models.RetrievalRecord, error) {
	var record models.RetrievalRecord
	err := l.db.GetContext(ctx, &record, `
		SELECT retrieval_id, source, external_id, query_text, retrieved_at,
			   agent_id, source_metadata, raw_response, snapshot, response_hash, prev_hash, hash_version
		FROM retrievals WHERE retrieval_id = ?
	`, retrievalID)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	return &record, err
}

func (l *Log) List(ctx context.Context, agentID, source string, limit int) ([]models.RetrievalRecord, error) {
	var records []models.RetrievalRecord
	query := `SELECT retrieval_id, source, external_id, query_text, retrieved_at,
		agent_id, source_metadata, raw_response, snapshot, response_hash, prev_hash, hash_version
		FROM retrievals WHERE 1=1`
	args := []interface{}{}
	if agentID != "" {
		query += " AND agent_id = ?"
		args = append(args, agentID)
	}
	if source != "" {
		query += " AND source = ?"
		args = append(args, source)
	}
	query += " ORDER BY retrieved_at DESC LIMIT ?"
	args = append(args, limit)
	err := l.db.SelectContext(ctx, &records, query, args...)
	return records, err
}

func (l *Log) Close() error {
	l.cancel()
	l.wg.Wait()
	return nil
}

func BuildRecord(
	queryText, externalID, agentID, source string,
	sourceMetadata models.SourceMetadata,
	rawResponse string,
	snapshot models.Snapshot,
) models.RetrievalRecord {
	retrievalID := generateID()
	retrievedAt := time.Now().UTC().Format(time.RFC3339)
	sourceMetaJSON, _ := json.Marshal(sourceMetadata)
	snapshotJSON, _ := json.Marshal(snapshot)

	return models.RetrievalRecord{
		RetrievalID:    retrievalID,
		Source:         source,
		ExternalID:     externalID,
		QueryText:      queryText,
		RetrievedAt:    retrievedAt,
		AgentID:        agentID,
		SourceMetadata: string(sourceMetaJSON),
		RawResponse:    rawResponse,
		Snapshot:       string(snapshotJSON),
		HashVersion:    HashVersion,
	}
}

func BuildSnapshotFromPubMed(paper models.PubMedPaper) models.Snapshot {
	return models.Snapshot{
		Title:            paper.Title,
		Abstract:         paper.Abstract,
		Authors:          paper.Authors,
		Journal:          paper.Journal,
		PublicationTypes: paper.PublicationTypes,
		MeshTerms:        paper.MeshTerms,
		DOI:              paper.DOI,
		MedlineStatus:    paper.MedlineStatus,
		PubStatus:        paper.PubStatus,
	}
}

func BuildSourceMetadata(paper models.PubMedPaper) models.SourceMetadata {
	return models.SourceMetadata{
		MedlineStatus: paper.MedlineStatus,
		PubStatus:     paper.PubStatus,
	}
}

func generateID() string {
	return time.Now().UTC().Format("20060102150405.000000") + "-" + randomString(12)
}

func randomString(n int) string {
	const letters = "abcdefghijklmnopqrstuvwxyz0123456789"
	b := make([]byte, n)
	for i := range b {
		b[i] = letters[time.Now().UnixNano()%int64(len(letters))]
		time.Sleep(time.Nanosecond)
	}
	return string(b)
}
