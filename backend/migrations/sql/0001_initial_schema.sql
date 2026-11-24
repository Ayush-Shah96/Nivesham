CREATE TABLE articles (
	id SERIAL NOT NULL, 
	url VARCHAR(1000) NOT NULL, 
	title VARCHAR(1000) NOT NULL, 
	source VARCHAR(120) NOT NULL, 
	published_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	raw_content TEXT, 
	content_hash VARCHAR(64) NOT NULL, 
	processing_status VARCHAR(50) NOT NULL, 
	summary TEXT, 
	companies_mentioned JSON NOT NULL, 
	relevance_score FLOAT, 
	is_material BOOLEAN NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (url)
)

;

CREATE INDEX ix_articles_source_published ON articles (source, published_at);

CREATE INDEX ix_articles_content_hash ON articles (content_hash);


CREATE TABLE companies (
	id SERIAL NOT NULL, 
	legal_name VARCHAR(255) NOT NULL, 
	nse_symbol VARCHAR(40), 
	bse_code VARCHAR(40), 
	isin VARCHAR(20), 
	aliases JSON NOT NULL, 
	market_cap FLOAT, 
	market_cap_updated_at TIMESTAMP WITH TIME ZONE, 
	website VARCHAR(500), 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (legal_name), 
	UNIQUE (nse_symbol), 
	UNIQUE (bse_code), 
	UNIQUE (isin)
)

;


CREATE TABLE crisp_investor_notes (
	id SERIAL NOT NULL, 
	source_audio_uri VARCHAR(1000) NOT NULL, 
	transcript TEXT, 
	notes TEXT, 
	diarization JSON NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
)

;


CREATE TABLE emails (
	id SERIAL NOT NULL, 
	email_id VARCHAR(255) NOT NULL, 
	from_address VARCHAR(255) NOT NULL, 
	to_address VARCHAR(255) NOT NULL, 
	subject VARCHAR(1000) NOT NULL, 
	body TEXT NOT NULL, 
	received_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	attachments_count INTEGER NOT NULL, 
	attachment_ids JSON NOT NULL, 
	processing_status VARCHAR(50) NOT NULL, 
	extracted_data JSON NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (email_id)
)

;


CREATE TABLE market_intelligence_metrics (
	id SERIAL NOT NULL, 
	source_name VARCHAR(120) NOT NULL, 
	category VARCHAR(120) NOT NULL, 
	metric_name VARCHAR(255) NOT NULL, 
	metric_value FLOAT NOT NULL, 
	unit VARCHAR(80), 
	recorded_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
)

;

CREATE INDEX ix_market_intelligence_metrics_recorded_at ON market_intelligence_metrics (recorded_at);


CREATE TABLE processing_jobs (
	id SERIAL NOT NULL, 
	request_id VARCHAR(36) NOT NULL, 
	trace_id VARCHAR(36) NOT NULL, 
	event_id VARCHAR(36) NOT NULL, 
	application VARCHAR(80) NOT NULL, 
	workflow_stage VARCHAR(120) NOT NULL, 
	status VARCHAR(50) NOT NULL, 
	error_message TEXT, 
	retry_count INTEGER NOT NULL, 
	started_at TIMESTAMP WITH TIME ZONE, 
	completed_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	UNIQUE (request_id)
)

;

CREATE INDEX ix_processing_jobs_event_id ON processing_jobs (event_id);

CREATE INDEX ix_processing_jobs_trace_id ON processing_jobs (trace_id);


CREATE TABLE users (
	id SERIAL NOT NULL, 
	username VARCHAR(120) NOT NULL, 
	password_hash VARCHAR(255) NOT NULL, 
	role userrole NOT NULL, 
	is_active BOOLEAN NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
)

;

CREATE UNIQUE INDEX ix_users_username ON users (username);


CREATE TABLE audit_logs (
	id SERIAL NOT NULL, 
	user_id INTEGER, 
	action VARCHAR(120) NOT NULL, 
	resource VARCHAR(120) NOT NULL, 
	resource_id VARCHAR(120), 
	detail JSON NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE SET NULL
)

;


CREATE TABLE filings (
	id SERIAL NOT NULL, 
	filing_id VARCHAR(200) NOT NULL, 
	company_id INTEGER, 
	filing_type VARCHAR(100) NOT NULL, 
	source VARCHAR(50) NOT NULL, 
	published_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	document_url VARCHAR(1000) NOT NULL, 
	document_hash VARCHAR(64), 
	extracted_text TEXT, 
	extracted_tables JSON NOT NULL, 
	transactions JSON NOT NULL, 
	processing_status VARCHAR(50) NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (filing_id), 
	FOREIGN KEY(company_id) REFERENCES companies (id) ON DELETE SET NULL
)

;

CREATE INDEX ix_filings_document_hash ON filings (document_hash);


CREATE TABLE financial_results (
	id SERIAL NOT NULL, 
	company_id INTEGER NOT NULL, 
	period VARCHAR(30) NOT NULL, 
	published_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	source_url VARCHAR(1000) NOT NULL, 
	metrics JSON NOT NULL, 
	status approvalstatus NOT NULL, 
	approved_by INTEGER, 
	approved_at TIMESTAMP WITH TIME ZONE, 
	rejection_reason TEXT, 
	PRIMARY KEY (id), 
	FOREIGN KEY(company_id) REFERENCES companies (id) ON DELETE CASCADE, 
	FOREIGN KEY(approved_by) REFERENCES users (id) ON DELETE SET NULL
)

;

CREATE INDEX ix_financial_results_status ON financial_results (status);


CREATE TABLE insider_signals (
	id SERIAL NOT NULL, 
	company_id INTEGER NOT NULL, 
	stock_name VARCHAR(255) NOT NULL, 
	person_name VARCHAR(255) NOT NULL, 
	price FLOAT, 
	amount_inr FLOAT, 
	transaction_mode VARCHAR(100) NOT NULL, 
	market_cap FLOAT NOT NULL, 
	transaction_date TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(company_id) REFERENCES companies (id) ON DELETE CASCADE
)

;


CREATE TABLE target_companies (
	id SERIAL NOT NULL, 
	company_id INTEGER NOT NULL, 
	analyst_id INTEGER NOT NULL, 
	track_mode trackmode NOT NULL, 
	active BOOLEAN NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_target_company UNIQUE (company_id), 
	FOREIGN KEY(company_id) REFERENCES companies (id) ON DELETE CASCADE, 
	FOREIGN KEY(analyst_id) REFERENCES users (id) ON DELETE RESTRICT
)

;

CREATE INDEX ix_target_companies_company_id ON target_companies (company_id);
