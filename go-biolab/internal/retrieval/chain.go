package retrieval

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"strings"
	"time"
	"unicode/utf16"

	"github.com/jmoiron/sqlx"
	"github.com/srikarjy/biolab-mcp/go-biolab/internal/models"
)

// HashVersion 2 hashes every audited column. It is byte-compatible with the Python
// implementation (biolab/retrieval_log.py), so either side verifies the other's rows.
const HashVersion = 2

// pyJSONString encodes s exactly as Python's json.dumps(s, ensure_ascii=True): every
// character outside printable ASCII is \uXXXX-escaped (lowercase hex, UTF-16 surrogate
// pairs), which encoding/json does not do.
func pyJSONString(s string) string {
	var b strings.Builder
	b.WriteByte('"')
	for _, r := range s {
		switch {
		case r == '"':
			b.WriteString(`\"`)
		case r == '\\':
			b.WriteString(`\\`)
		case r == '\n':
			b.WriteString(`\n`)
		case r == '\r':
			b.WriteString(`\r`)
		case r == '\t':
			b.WriteString(`\t`)
		case r == '\b':
			b.WriteString(`\b`)
		case r == '\f':
			b.WriteString(`\f`)
		case r >= 0x20 && r <= 0x7e:
			b.WriteRune(r)
		case r > 0xffff:
			r1, r2 := utf16.EncodeRune(r)
			fmt.Fprintf(&b, `\u%04x\u%04x`, r1, r2)
		default:
			fmt.Fprintf(&b, `\u%04x`, r)
		}
	}
	b.WriteByte('"')
	return b.String()
}

func jsonArray(items ...string) string {
	parts := make([]string, len(items))
	for i, it := range items {
		parts[i] = pyJSONString(it)
	}
	return "[" + strings.Join(parts, ",") + "]"
}

// ComputeHash returns the chained hash for a record under its HashVersion.
func ComputeHash(r models.RetrievalRecord) (string, error) {
	var material string
	switch r.HashVersion {
	case 1:
		material = r.PrevHash + r.RawResponse + r.RetrievalID + r.RetrievedAt
	case 2:
		material = jsonArray(
			"biolab-retrieval-v2", r.PrevHash, r.RetrievalID, r.Source, r.ExternalID,
			r.QueryText, r.RetrievedAt, r.AgentID, r.SourceMetadata, r.RawResponse, r.Snapshot,
		)
	default:
		return "", fmt.Errorf("unknown hash_version %d", r.HashVersion)
	}
	sum := sha256.Sum256([]byte(material))
	return hex.EncodeToString(sum[:]), nil
}

// VerifyChain walks the log in insertion order. It returns ok=false and the first
// retrieval_id where the chain breaks (bad prev_hash link or a hash that no longer
// matches the row's contents).
func VerifyChain(db *sqlx.DB) (ok bool, brokenID string, err error) {
	var rows []models.RetrievalRecord
	err = db.Select(&rows, `
		SELECT retrieval_id, source, external_id, query_text, retrieved_at, agent_id,
		       source_metadata, raw_response, snapshot, response_hash, prev_hash, hash_version
		FROM retrievals ORDER BY rowid ASC`)
	if err != nil {
		return false, "", err
	}
	expectedPrev := ""
	for _, r := range rows {
		if r.PrevHash != expectedPrev {
			return false, r.RetrievalID, nil
		}
		recomputed, herr := ComputeHash(r)
		if herr != nil || recomputed != r.ResponseHash {
			return false, r.RetrievalID, nil
		}
		expectedPrev = r.ResponseHash
	}
	return true, "", nil
}

// Anchor is a signed checkpoint of the chain head, stored outside the database.
type Anchor struct {
	Version   int    `json:"version"`
	Count     int    `json:"count"`
	HeadHash  string `json:"head_hash"`
	CreatedAt string `json:"created_at"`
	HMAC      string `json:"hmac"`
}

func anchorMAC(a Anchor, key []byte) string {
	body := fmt.Sprintf(`[%d,%d,%s,%s]`, a.Version, a.Count, pyJSONString(a.HeadHash), pyJSONString(a.CreatedAt))
	m := hmac.New(sha256.New, key)
	m.Write([]byte(body))
	return hex.EncodeToString(m.Sum(nil))
}

func MakeAnchor(db *sqlx.DB, key []byte) (Anchor, error) {
	var count int
	if err := db.Get(&count, "SELECT count(*) FROM retrievals"); err != nil {
		return Anchor{}, err
	}
	head := ""
	var heads []string
	if err := db.Select(&heads, "SELECT response_hash FROM retrievals ORDER BY rowid DESC LIMIT 1"); err != nil {
		return Anchor{}, err
	}
	if len(heads) == 1 {
		head = heads[0]
	}
	a := Anchor{Version: 1, Count: count, HeadHash: head, CreatedAt: time.Now().UTC().Format("2006-01-02T15:04:05.000000+00:00")}
	a.HMAC = anchorMAC(a, key)
	return a, nil
}

// VerifyAnchor checks the live log against a checkpoint.
func VerifyAnchor(db *sqlx.DB, a Anchor, key []byte) (bool, string, error) {
	if !hmac.Equal([]byte(anchorMAC(a, key)), []byte(a.HMAC)) {
		return false, "anchor signature invalid (wrong key or edited anchor)", nil
	}
	if a.Count == 0 {
		return true, "anchor predates any records", nil
	}
	var heads []string
	if err := db.Select(&heads, "SELECT response_hash FROM retrievals ORDER BY rowid ASC LIMIT 1 OFFSET ?", a.Count-1); err != nil {
		return false, "", err
	}
	if len(heads) == 0 {
		var total int
		_ = db.Get(&total, "SELECT count(*) FROM retrievals")
		return false, fmt.Sprintf("log truncated: anchor covers %d records, log has %d", a.Count, total), nil
	}
	if heads[0] != a.HeadHash {
		return false, fmt.Sprintf("record %d no longer matches the anchored hash (log rewritten)", a.Count), nil
	}
	return true, fmt.Sprintf("log extends the anchored head at record %d", a.Count), nil
}
