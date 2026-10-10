package db

import (
	"fmt"

	"github.com/jmoiron/sqlx"
	_ "modernc.org/sqlite"
)

var Schema = `
CREATE TABLE IF NOT EXISTS retrievals (
    retrieval_id     TEXT PRIMARY KEY,
    source           TEXT NOT NULL,
    external_id      TEXT NOT NULL,
    query_text       TEXT NOT NULL,
    retrieved_at     TEXT NOT NULL,
    agent_id         TEXT NOT NULL,
    source_metadata  TEXT NOT NULL,
    raw_response     TEXT NOT NULL,
    snapshot         TEXT NOT NULL,
    response_hash    TEXT NOT NULL,
    prev_hash        TEXT NOT NULL DEFAULT '',
    hash_version     INTEGER NOT NULL DEFAULT 2
);

CREATE INDEX IF NOT EXISTS idx_retrievals_external_id ON retrievals(external_id);
CREATE INDEX IF NOT EXISTS idx_retrievals_agent_id ON retrievals(agent_id);
CREATE INDEX IF NOT EXISTS idx_retrievals_retrieved_at ON retrievals(retrieved_at);
CREATE INDEX IF NOT EXISTS idx_retrievals_source ON retrievals(source);
`

func Connect(dbPath string) (*sqlx.DB, error) {
	db, err := sqlx.Connect("sqlite", dbPath)
	if err != nil {
		return nil, fmt.Errorf("connect: %w", err)
	}

	if err := upgradeRetrievals(db); err != nil {
		return nil, fmt.Errorf("upgrade: %w", err)
	}
	if _, err := db.Exec(Schema); err != nil {
		return nil, fmt.Errorf("schema: %w", err)
	}

	// Enable WAL mode for better concurrency
	if _, err := db.Exec("PRAGMA journal_mode=WAL"); err != nil {
		return nil, fmt.Errorf("wal mode: %w", err)
	}

	return db, nil
}

// upgradeRetrievals adds the chain columns to a database created before hash chaining.
// Legacy rows are labelled hash_version 1; they hashed only raw_response and carry no
// prev_hash, so they are not part of a verifiable chain.
func upgradeRetrievals(db *sqlx.DB) error {
	var cols []string
	if err := db.Select(&cols, "SELECT name FROM pragma_table_info('retrievals')"); err != nil {
		return err
	}
	if len(cols) == 0 {
		return nil // fresh database; Schema creates the full table
	}
	have := map[string]bool{}
	for _, c := range cols {
		have[c] = true
	}
	if !have["prev_hash"] {
		if _, err := db.Exec("ALTER TABLE retrievals ADD COLUMN prev_hash TEXT NOT NULL DEFAULT ''"); err != nil {
			return err
		}
	}
	if !have["hash_version"] {
		if _, err := db.Exec("ALTER TABLE retrievals ADD COLUMN hash_version INTEGER NOT NULL DEFAULT 1"); err != nil {
			return err
		}
	}
	return nil
}

func MustConnect(dbPath string) *sqlx.DB {
	db, err := Connect(dbPath)
	if err != nil {
		panic(err)
	}
	return db
}
