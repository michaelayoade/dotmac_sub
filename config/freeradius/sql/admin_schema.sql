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
    nasportid VARCHAR(253),
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

-- Post-auth logging (mirrors radpostauth from schema.sql)
CREATE TABLE IF NOT EXISTS radpostauth_admin (
    id BIGSERIAL PRIMARY KEY,
    username VARCHAR(64) NOT NULL,
    pass VARCHAR(64),
    reply VARCHAR(32),
    authdate TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    nasipaddress INET,
    calledstationid VARCHAR(50),
    callingstationid VARCHAR(50)
);
CREATE INDEX IF NOT EXISTS radpostauth_admin_username ON radpostauth_admin (username);
CREATE INDEX IF NOT EXISTS radpostauth_admin_authdate ON radpostauth_admin (authdate);

-- Group check attributes (mirrors radgroupcheck from schema.sql)
CREATE TABLE IF NOT EXISTS radgroupcheck_admin (
    id SERIAL PRIMARY KEY,
    groupname VARCHAR(64) NOT NULL,
    attribute VARCHAR(64) NOT NULL,
    op VARCHAR(2) DEFAULT ':=',
    value VARCHAR(253) NOT NULL
);
CREATE INDEX IF NOT EXISTS radgroupcheck_admin_groupname ON radgroupcheck_admin (groupname);

-- Group reply attributes (mirrors radgroupreply from schema.sql)
CREATE TABLE IF NOT EXISTS radgroupreply_admin (
    id SERIAL PRIMARY KEY,
    groupname VARCHAR(64) NOT NULL,
    attribute VARCHAR(64) NOT NULL,
    op VARCHAR(2) DEFAULT ':=',
    value VARCHAR(253) NOT NULL
);
CREATE INDEX IF NOT EXISTS radgroupreply_admin_groupname ON radgroupreply_admin (groupname);

-- User group membership (mirrors radusergroup from schema.sql)
CREATE TABLE IF NOT EXISTS radusergroup_admin (
    id SERIAL PRIMARY KEY,
    username VARCHAR(64) NOT NULL,
    groupname VARCHAR(64) NOT NULL,
    priority INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS radusergroup_admin_username ON radusergroup_admin (username);
