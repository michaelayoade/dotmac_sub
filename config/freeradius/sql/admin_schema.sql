-- Staff-only RADIUS auth set (device login). Never populated from subscribers.
CREATE TABLE IF NOT EXISTS radcheck_admin (
    id BIGSERIAL PRIMARY KEY,
    username VARCHAR(64) NOT NULL DEFAULT '',
    attribute VARCHAR(64) NOT NULL DEFAULT '',
    op CHAR(2) NOT NULL DEFAULT '==',
    value VARCHAR(253) NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS radcheck_admin_username ON radcheck_admin (username);

CREATE TABLE IF NOT EXISTS radreply_admin (
    id BIGSERIAL PRIMARY KEY,
    username VARCHAR(64) NOT NULL DEFAULT '',
    attribute VARCHAR(64) NOT NULL DEFAULT '',
    op CHAR(2) NOT NULL DEFAULT '=',
    value VARCHAR(253) NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS radreply_admin_username ON radreply_admin (username);

CREATE TABLE IF NOT EXISTS radacct_admin (
    radacctid BIGSERIAL PRIMARY KEY,
    acctsessionid VARCHAR(64) NOT NULL DEFAULT '',
    acctuniqueid VARCHAR(32) NOT NULL UNIQUE,
    username VARCHAR(64) NOT NULL DEFAULT '',
    nasipaddress INET NOT NULL,
    nasportid VARCHAR(32),
    acctstarttime TIMESTAMPTZ,
    acctupdatetime TIMESTAMPTZ,
    acctstoptime TIMESTAMPTZ,
    acctsessiontime BIGINT,
    acctterminatecause VARCHAR(32) DEFAULT '',
    callingstationid VARCHAR(50) DEFAULT '',
    servicetype VARCHAR(32) DEFAULT ''
);
CREATE INDEX IF NOT EXISTS radacct_admin_username ON radacct_admin (username);
CREATE INDEX IF NOT EXISTS radacct_admin_active ON radacct_admin (acctstoptime) WHERE acctstoptime IS NULL;
