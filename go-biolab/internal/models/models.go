package models

type RetrievalRecord struct {
	RetrievalID    string `db:"retrieval_id" json:"retrieval_id"`
	Source         string `db:"source" json:"source"`
	ExternalID     string `db:"external_id" json:"external_id"`
	QueryText      string `db:"query_text" json:"query_text"`
	RetrievedAt    string `db:"retrieved_at" json:"retrieved_at"`
	AgentID        string `db:"agent_id" json:"agent_id"`
	SourceMetadata string `db:"source_metadata" json:"source_metadata"`
	RawResponse    string `db:"raw_response" json:"raw_response"`
	Snapshot       string `db:"snapshot" json:"snapshot"`
	ResponseHash   string `db:"response_hash" json:"response_hash"`
	PrevHash       string `db:"prev_hash" json:"prev_hash"`
	HashVersion    int    `db:"hash_version" json:"hash_version"`
}

type SourceMetadata struct {
	MedlineStatus string `json:"medline_status"`
	PubStatus     string `json:"pub_status"`
}

type Snapshot struct {
	Title            string   `json:"title"`
	Abstract         string   `json:"abstract"`
	Authors          []Author `json:"authors"`
	Journal          Journal  `json:"journal"`
	PublicationTypes []string `json:"publication_types"`
	MeshTerms        []string `json:"mesh_terms"`
	DOI              string   `json:"doi"`
	MedlineStatus    string   `json:"medline_status"`
	PubStatus        string   `json:"pub_status"`
}

type Author struct {
	LastName string `json:"lastname"`
	ForeName string `json:"forename"`
	Initials string `json:"initials"`
}

type Journal struct {
	Title           string `json:"title"`
	ISOAbbreviation string `json:"iso_abbreviation"`
	ISSN            string `json:"issn"`
	PubDate         string `json:"pub_date"`
}

type PubMedPaper struct {
	PMID             string
	Title            string
	Abstract         string
	MedlineStatus    string
	PubStatus        string
	RawXML           string
	Authors          []Author
	Journal          Journal
	PublicationTypes []string
	MeshTerms        []string
	DOI              string
}

type EuropePMCArticle struct {
	ID             string
	Source         string
	PMID           string
	DOI            string
	Title          string
	AuthorString   string
	JournalTitle   string
	JournalISOAbbr string
	ISSN           string
	Volume         string
	Issue          string
	PageInfo       string
	PubYear        string
	PubType        string
	IsOpenAccess   bool
	AbstractText   string
	Affiliation    string
	FullTextXML    string
}

type ClinicalTrialStudy struct {
	NCTId          string
	BriefTitle     string
	OfficialTitle  string
	Organization   string
	OverallStatus  string
	StartDate      string
	CompletionDate string
	StudyType      string
	Phase          string
	BriefSummary   string
	DetailedDesc   string
	Conditions     []string
	Keywords       []string
	Interventions  []Intervention
	Design         Design
	ArmGroups      []ArmGroup
	Eligibility    Eligibility
	Locations      []Location
	FullJSON       string
}

type Intervention struct {
	Type        string `json:"type"`
	Name        string `json:"name"`
	Description string `json:"description"`
}

type Design struct {
	StudyType         string `json:"study_type"`
	Allocation        string `json:"allocation"`
	InterventionModel string `json:"intervention_model"`
	PrimaryPurpose    string `json:"primary_purpose"`
	Masking           string `json:"masking"`
}

type ArmGroup struct {
	Label       string `json:"label"`
	Type        string `json:"type"`
	Description string `json:"description"`
}

type Eligibility struct {
	Criteria          string   `json:"criteria"`
	HealthyVolunteers string   `json:"healthy_volunteers"`
	Sex               string   `json:"sex"`
	GenderBased       string   `json:"gender_based"`
	MinimumAge        string   `json:"minimum_age"`
	MaximumAge        string   `json:"maximum_age"`
	StdAges           []string `json:"std_ages"`
}

type Location struct {
	Facility string `json:"facility"`
	City     string `json:"city"`
	State    string `json:"state"`
	Country  string `json:"country"`
}

type BioRxivPreprint struct {
	Title                   string
	Authors                 string
	AuthorCorresponding     string
	AuthorCorrespondingInst string
	DOI                     string
	Date                    string
	Version                 string
	Type                    string
	License                 string
	Category                string
	JATSXML                 string
	Abstract                string
	Funder                  string
	Published               string
	Server                  string
	FullJATSXML             string
}
