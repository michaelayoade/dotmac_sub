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
    acctsessionid VARCHAR(64) NOT NULL,
    acctuniqueid VARCHAR(32) NOT NULL UNIQUE,
    username VARCHAR(64),
    nasipaddress INET,
    nasportid VARCHAR(32),
    nasporttype VARCHAR(32),
    acctstarttime TIMESTAMP WITH TIME ZONE,
    acctupdatetime TIMESTAMP WITH TIME ZONE,
    acctstoptime TIMESTAMP WITH TIME ZONE,
    acctinterval INTEGER,
    acctsessiontime BIGINT,
    acctauthentic VARCHAR(32),
    connectinfo_start VARCHAR(50),
    connectinfo_stop VARCHAR(50),
    acctinputoctets BIGINT,
    acctoutputoctets BIGINT,
    calledstationid VARCHAR(50),
    callingstationid VARCHAR(50),
    acctterminatecause VARCHAR(32),
    servicetype VARCHAR(32),
    framedprotocol VARCHAR(32),
    framedipaddress INET,
    framedipv6address VARCHAR(45),
    framedipv6prefix VARCHAR(45),
    framedinterfaceid VARCHAR(44),
    delegatedipv6prefix VARCHAR(45),
    class VARCHAR(64)
);
CREATE INDEX IF NOT EXISTS radacct_admin_username ON radacct_admin (username);
CREATE INDEX IF NOT EXISTS radacct_admin_active ON radacct_admin (username) WHERE acctstoptime IS NULL;
