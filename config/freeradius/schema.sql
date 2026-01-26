-- FreeRADIUS PostgreSQL Schema
-- Standard tables for user authentication and accounting

-- User check attributes (authentication)
CREATE TABLE radcheck (
    id SERIAL PRIMARY KEY,
    username VARCHAR(64) NOT NULL,
    attribute VARCHAR(64) NOT NULL,
    op VARCHAR(2) DEFAULT ':=',
    value VARCHAR(253) NOT NULL
);
CREATE INDEX idx_radcheck_username ON radcheck(username);

-- User reply attributes (sent back on successful auth)
CREATE TABLE radreply (
    id SERIAL PRIMARY KEY,
    username VARCHAR(64) NOT NULL,
    attribute VARCHAR(64) NOT NULL,
    op VARCHAR(2) DEFAULT ':=',
    value VARCHAR(253) NOT NULL
);
CREATE INDEX idx_radreply_username ON radreply(username);

-- User group membership
CREATE TABLE radusergroup (
    id SERIAL PRIMARY KEY,
    username VARCHAR(64) NOT NULL,
    groupname VARCHAR(64) NOT NULL,
    priority INTEGER DEFAULT 0
);
CREATE INDEX idx_radusergroup_username ON radusergroup(username);

-- Group check attributes
CREATE TABLE radgroupcheck (
    id SERIAL PRIMARY KEY,
    groupname VARCHAR(64) NOT NULL,
    attribute VARCHAR(64) NOT NULL,
    op VARCHAR(2) DEFAULT ':=',
    value VARCHAR(253) NOT NULL
);
CREATE INDEX idx_radgroupcheck_groupname ON radgroupcheck(groupname);

-- Group reply attributes
CREATE TABLE radgroupreply (
    id SERIAL PRIMARY KEY,
    groupname VARCHAR(64) NOT NULL,
    attribute VARCHAR(64) NOT NULL,
    op VARCHAR(2) DEFAULT ':=',
    value VARCHAR(253) NOT NULL
);
CREATE INDEX idx_radgroupreply_groupname ON radgroupreply(groupname);

-- NAS (Network Access Server) clients
CREATE TABLE nas (
    id SERIAL PRIMARY KEY,
    nasname VARCHAR(128) NOT NULL,
    shortname VARCHAR(32),
    type VARCHAR(30) DEFAULT 'other',
    ports INTEGER,
    secret VARCHAR(60) NOT NULL,
    server VARCHAR(64),
    community VARCHAR(50),
    description VARCHAR(200)
);
CREATE INDEX idx_nas_nasname ON nas(nasname);

-- Accounting records
CREATE TABLE radacct (
    radacctid BIGSERIAL PRIMARY KEY,
    acctsessionid VARCHAR(64) NOT NULL,
    acctuniqueid VARCHAR(32) NOT NULL UNIQUE,
    username VARCHAR(64),
    nasipaddress VARCHAR(15),
    nasportid VARCHAR(32),
    nasporttype VARCHAR(32),
    acctstarttime TIMESTAMP WITH TIME ZONE,
    acctupdatetime TIMESTAMP WITH TIME ZONE,
    acctstoptime TIMESTAMP WITH TIME ZONE,
    acctinterval INTEGER,
    acctsessiontime INTEGER,
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
    framedipaddress VARCHAR(15),
    framedipv6address VARCHAR(45),
    framedipv6prefix VARCHAR(45),
    framedinterfaceid VARCHAR(44),
    delegatedipv6prefix VARCHAR(45)
);
CREATE INDEX idx_radacct_username ON radacct(username);
CREATE INDEX idx_radacct_acctsessionid ON radacct(acctsessionid);
CREATE INDEX idx_radacct_acctstarttime ON radacct(acctstarttime);
CREATE INDEX idx_radacct_acctstoptime ON radacct(acctstoptime);
CREATE INDEX idx_radacct_nasipaddress ON radacct(nasipaddress);

-- Post-auth logging
CREATE TABLE radpostauth (
    id BIGSERIAL PRIMARY KEY,
    username VARCHAR(64) NOT NULL,
    pass VARCHAR(64),
    reply VARCHAR(32),
    authdate TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_radpostauth_username ON radpostauth(username);
CREATE INDEX idx_radpostauth_authdate ON radpostauth(authdate);
