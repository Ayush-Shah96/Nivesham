"""Frozen baseline schema for the investment intelligence backend.

IMPORTANT:
This revision intentionally contains a static snapshot of the baseline schema.
It must not import ``Base.metadata`` because Alembic revisions are immutable:
the application models continue to evolve while this revision must remain the
schema that existed at revision 0001.

Later schema changes belong to 0003+ and must be replayed by Alembic.
"""

from alembic import op


revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:

    # Freeze the baseline enum types here. This migration must not depend on the
    # current application model, because later model changes are represented by
    # later Alembic revisions.
    op.execute(
        "CREATE TYPE userrole AS ENUM ('ADMIN', 'INVESTMENT', 'ANALYST')"
    )
    op.execute(
        "CREATE TYPE trackmode AS ENUM ('HT', 'LT')"
    )
    op.execute(
        "CREATE TYPE approvalstatus AS ENUM ("
        "'PENDING_AASHIL_APPROVAL', 'PENDING_APPROVAL', 'APPROVED', 'REJECTED'"
        ")"
    )

    # Baseline tables.
    op.execute('CREATE TABLE articles (\n\tid SERIAL NOT NULL, \n\turl VARCHAR(1000) NOT NULL, \n\ttitle VARCHAR(1000) NOT NULL, \n\tsource VARCHAR(120) NOT NULL, \n\tpublished_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\traw_content TEXT, \n\tcontent_hash VARCHAR(64) NOT NULL, \n\tprocessing_status VARCHAR(50) NOT NULL, \n\tsummary TEXT, \n\trelevance_score FLOAT, \n\tis_material BOOLEAN NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tcompanies_mentioned JSON NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (url)\n)')

    op.execute('CREATE TABLE companies (\n\tid SERIAL NOT NULL, \n\tlegal_name VARCHAR(255) NOT NULL, \n\tnse_symbol VARCHAR(40), \n\tbse_code VARCHAR(40), \n\tisin VARCHAR(20), \n\taliases JSON NOT NULL, \n\tmarket_cap FLOAT, \n\tmarket_cap_updated_at TIMESTAMP WITH TIME ZONE, \n\twebsite VARCHAR(500), \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (legal_name), \n\tUNIQUE (nse_symbol), \n\tUNIQUE (bse_code), \n\tUNIQUE (isin)\n)')

    op.execute('CREATE TABLE crisp_investor_notes (\n\tid SERIAL NOT NULL, \n\tsource_audio_uri VARCHAR(1000) NOT NULL, \n\ttranscript TEXT, \n\tnotes TEXT, \n\tdiarization JSON NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id)\n)')

    op.execute('CREATE TABLE emails (\n\tid SERIAL NOT NULL, \n\temail_id VARCHAR(255) NOT NULL, \n\tfrom_address VARCHAR(255) NOT NULL, \n\tto_address VARCHAR(255) NOT NULL, \n\tsubject VARCHAR(1000) NOT NULL, \n\tbody TEXT NOT NULL, \n\treceived_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tattachments_count INTEGER NOT NULL, \n\tattachment_ids JSON NOT NULL, \n\tprocessing_status VARCHAR(50) NOT NULL, \n\textracted_data JSON NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (email_id)\n)')

    op.execute('CREATE TABLE event_outbox (\n\tid SERIAL NOT NULL, \n\tevent_id VARCHAR(36) NOT NULL, \n\tevent_type VARCHAR(80) NOT NULL, \n\tsubject VARCHAR(160) NOT NULL, \n\tpayload JSON NOT NULL, \n\tstatus VARCHAR(30) NOT NULL, \n\tattempts INTEGER NOT NULL, \n\tlast_error TEXT, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tpublished_at TIMESTAMP WITH TIME ZONE, \n\tPRIMARY KEY (id)\n)')

    op.execute('CREATE TABLE market_intelligence_metrics (\n\tid SERIAL NOT NULL, \n\tsource_name VARCHAR(120) NOT NULL, \n\tcategory VARCHAR(120) NOT NULL, \n\tmetric_name VARCHAR(255) NOT NULL, \n\tmetric_value FLOAT NOT NULL, \n\tunit VARCHAR(80), \n\trecorded_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id)\n)')

    op.execute('CREATE TABLE processing_jobs (\n\tid SERIAL NOT NULL, \n\trequest_id VARCHAR(36) NOT NULL, \n\ttrace_id VARCHAR(36) NOT NULL, \n\tevent_id VARCHAR(36) NOT NULL, \n\tapplication VARCHAR(80) NOT NULL, \n\tworkflow_stage VARCHAR(120) NOT NULL, \n\tstatus VARCHAR(50) NOT NULL, \n\terror_message TEXT, \n\tretry_count INTEGER NOT NULL, \n\tstarted_at TIMESTAMP WITH TIME ZONE, \n\tcompleted_at TIMESTAMP WITH TIME ZONE, \n\tPRIMARY KEY (id), \n\tUNIQUE (request_id)\n)')

    op.execute('CREATE TABLE source_documents (\n\tid SERIAL NOT NULL, \n\tsource_type VARCHAR(80) NOT NULL, \n\tsource_uri VARCHAR(1000) NOT NULL, \n\tcontent_hash VARCHAR(64) NOT NULL, \n\tobject_key VARCHAR(1000), \n\tmime_type VARCHAR(255), \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (source_uri)\n)')

    op.execute('CREATE TABLE users (\n\tid SERIAL NOT NULL, \n\tusername VARCHAR(120) NOT NULL, \n\tpassword_hash VARCHAR(255) NOT NULL, \n\trole userrole NOT NULL, \n\tis_active BOOLEAN NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\ttelegram_user_id INTEGER, \n\tPRIMARY KEY (id), \n\tUNIQUE (telegram_user_id)\n)')

    op.execute('CREATE TABLE audit_logs (\n\tid SERIAL NOT NULL, \n\tuser_id INTEGER, \n\taction VARCHAR(120) NOT NULL, \n\tresource VARCHAR(120) NOT NULL, \n\tresource_id VARCHAR(120), \n\tdetail JSON NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(user_id) REFERENCES users (id) ON DELETE SET NULL\n)')

    op.execute('CREATE TABLE benpos_records (\n\tid SERIAL NOT NULL, \n\temail_id INTEGER NOT NULL, \n\tcompany_id INTEGER, \n\tholder_name VARCHAR(255), \n\tprevious_quantity FLOAT, \n\tcurrent_quantity FLOAT, \n\tchange_quantity FLOAT, \n\tsource_uri VARCHAR(1000), \n\tprocessing_status VARCHAR(50) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(email_id) REFERENCES emails (id) ON DELETE CASCADE, \n\tFOREIGN KEY(company_id) REFERENCES companies (id) ON DELETE SET NULL\n)')

    op.execute('CREATE TABLE filings (\n\tid SERIAL NOT NULL, \n\tfiling_id VARCHAR(200) NOT NULL, \n\tcompany_id INTEGER, \n\tfiling_type VARCHAR(100) NOT NULL, \n\tsource VARCHAR(50) NOT NULL, \n\tpublished_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tdocument_url VARCHAR(1000) NOT NULL, \n\tdocument_hash VARCHAR(64), \n\textracted_text TEXT, \n\textracted_tables JSON NOT NULL, \n\ttransactions JSON NOT NULL, \n\tprocessing_status VARCHAR(50) NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (filing_id), \n\tFOREIGN KEY(company_id) REFERENCES companies (id) ON DELETE SET NULL\n)')

    op.execute('CREATE TABLE financial_results (\n\tid SERIAL NOT NULL, \n\tcompany_id INTEGER NOT NULL, \n\tperiod VARCHAR(30) NOT NULL, \n\tpublished_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tsource_url VARCHAR(1000) NOT NULL, \n\tmetrics JSON NOT NULL, \n\tstatus approvalstatus NOT NULL, \n\tapproved_by INTEGER, \n\tapproved_at TIMESTAMP WITH TIME ZONE, \n\trejection_reason TEXT, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(company_id) REFERENCES companies (id) ON DELETE CASCADE, \n\tFOREIGN KEY(approved_by) REFERENCES users (id) ON DELETE SET NULL\n)')

    op.execute('CREATE TABLE insider_signals (\n\tid SERIAL NOT NULL, \n\tcompany_id INTEGER NOT NULL, \n\tstock_name VARCHAR(255) NOT NULL, \n\tperson_name VARCHAR(255) NOT NULL, \n\ttransaction_date TIMESTAMP WITH TIME ZONE NOT NULL, \n\tprice FLOAT, \n\tamount_inr FLOAT, \n\ttransaction_mode VARCHAR(100) NOT NULL, \n\tmarket_cap FLOAT NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(company_id) REFERENCES companies (id) ON DELETE CASCADE\n)')

    op.execute('CREATE TABLE target_companies (\n\tid SERIAL NOT NULL, \n\tcompany_id INTEGER NOT NULL, \n\tanalyst_id INTEGER NOT NULL, \n\ttrack_mode trackmode NOT NULL, \n\tactive BOOLEAN NOT NULL, \n\tapproval_status approvalstatus NOT NULL, \n\tapproval_by INTEGER, \n\tapproval_at TIMESTAMP WITH TIME ZONE, \n\tstart_date TIMESTAMP WITH TIME ZONE, \n\tend_date TIMESTAMP WITH TIME ZONE, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_target_company UNIQUE (company_id), \n\tFOREIGN KEY(company_id) REFERENCES companies (id) ON DELETE CASCADE, \n\tFOREIGN KEY(approval_by) REFERENCES users (id) ON DELETE SET NULL, \n\tFOREIGN KEY(analyst_id) REFERENCES users (id) ON DELETE RESTRICT\n)')

    # Baseline indexes.
    op.execute('CREATE INDEX ix_articles_content_hash ON articles (content_hash)')
    op.execute('CREATE INDEX ix_articles_source_published ON articles (source, published_at)')
    op.execute('CREATE UNIQUE INDEX ix_event_outbox_event_id ON event_outbox (event_id)')
    op.execute('CREATE INDEX ix_event_outbox_status ON event_outbox (status)')
    op.execute('CREATE INDEX ix_market_intelligence_metrics_recorded_at ON market_intelligence_metrics (recorded_at)')
    op.execute('CREATE INDEX ix_processing_jobs_event_id ON processing_jobs (event_id)')
    op.execute('CREATE INDEX ix_processing_jobs_trace_id ON processing_jobs (trace_id)')
    op.execute('CREATE INDEX ix_source_documents_content_hash ON source_documents (content_hash)')
    op.execute('CREATE UNIQUE INDEX ix_users_username ON users (username)')
    op.execute('CREATE INDEX ix_benpos_records_email_id ON benpos_records (email_id)')
    op.execute('CREATE INDEX ix_filings_document_hash ON filings (document_hash)')
    op.execute('CREATE INDEX ix_financial_results_status ON financial_results (status)')
    op.execute('CREATE INDEX ix_target_companies_company_id ON target_companies (company_id)')
    op.execute('CREATE INDEX ix_target_companies_approval_status ON target_companies (approval_status)')


def downgrade() -> None:
    # Drop dependent tables first.
    op.execute('DROP TABLE target_companies')
    op.execute('DROP TABLE insider_signals')
    op.execute('DROP TABLE financial_results')
    op.execute('DROP TABLE filings')
    op.execute('DROP TABLE benpos_records')
    op.execute('DROP TABLE audit_logs')
    op.execute('DROP TABLE users')
    op.execute('DROP TABLE source_documents')
    op.execute('DROP TABLE processing_jobs')
    op.execute('DROP TABLE market_intelligence_metrics')
    op.execute('DROP TABLE event_outbox')
    op.execute('DROP TABLE emails')
    op.execute('DROP TABLE crisp_investor_notes')
    op.execute('DROP TABLE companies')
    op.execute('DROP TABLE articles')

    op.execute("DROP TYPE approvalstatus")
    op.execute("DROP TYPE trackmode")
    op.execute("DROP TYPE userrole")
