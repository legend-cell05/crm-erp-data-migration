-- ===========================================================================
-- The legacy Arcadia CRM.
--
-- This schema is intentionally bad, because a migration tool that is only
-- ever tested against a clean source has not been tested. Everything here is
-- modelled on what actually turns up in a twenty-year-old CRM:
--
--   * every column is text, including amounts and dates;
--   * primary keys are zero-padded strings with a system prefix;
--   * there are no foreign keys, so orphan rows exist;
--   * deletion is a flag, and half the code forgot to filter on it;
--   * codes live in reference tables that do not cover every code in use;
--   * the same company is entered three times with three spellings.
--
-- Fixing any of that here would be cheating: the difficulty of the migration
-- IS this schema.
-- ===========================================================================

-- Every table below is CREATE TABLE IF NOT EXISTS, and there is deliberately
-- no DROP. `keystone init-db` is documented as safe to re-run, and an earlier
-- version of this file began with DROP TABLE -- which meant that re-running it
-- silently emptied the source system. The generator truncates before it
-- inserts, so re-seeding still works; re-initialising no longer destroys
-- anything.

-- Users of the old system. Some are inactive; some own records anyway.
CREATE TABLE IF NOT EXISTS ${LEGACY}.usr (
    usr_id      VARCHAR(10) PRIMARY KEY,
    login       VARCHAR(30),
    fullname    VARCHAR(80),
    email       VARCHAR(120),
    active      CHAR(1)          -- 'Y' / 'N' / sometimes NULL
);

-- Code tables. Deliberately incomplete: the application let users type codes
-- that were never registered here.
CREATE TABLE IF NOT EXISTS ${LEGACY}.ref_stage (
    stage_cd    VARCHAR(10) PRIMARY KEY,
    label       VARCHAR(60),
    is_won      CHAR(1),
    is_closed   CHAR(1)
);

CREATE TABLE IF NOT EXISTS ${LEGACY}.ref_industry (
    ind_code    VARCHAR(10) PRIMARY KEY,
    label       VARCHAR(60)
);

-- Companies.
CREATE TABLE IF NOT EXISTS ${LEGACY}.cust (
    custno      VARCHAR(12) PRIMARY KEY,   -- 'C-000123'
    company     VARCHAR(120),
    addr1       VARCHAR(120),
    addr2       VARCHAR(120),
    city        VARCHAR(80),
    zip         VARCHAR(20),
    ctry        VARCHAR(40),               -- 'FR', 'France', 'FRANCE', 'Fr.', ''
    phone       VARCHAR(40),
    fax         VARCHAR(40),
    vat_no      VARCHAR(30),
    ind_code    VARCHAR(10),
    owner_id    VARCHAR(10),
    status      VARCHAR(20),               -- 'A', 'I', 'PROSPECT', NULL
    del_flag    CHAR(1),
    notes       TEXT,
    created     VARCHAR(30),               -- '12/03/2019', '2019-03-12', '03-12-19'
    updated     VARCHAR(30)
);

-- People.
CREATE TABLE IF NOT EXISTS ${LEGACY}.person (
    persno      VARCHAR(12) PRIMARY KEY,
    custno      VARCHAR(12),               -- no FK: orphans exist
    fname       VARCHAR(60),
    lname       VARCHAR(60),
    title       VARCHAR(60),
    email       VARCHAR(120),
    phone       VARCHAR(40),
    mobile      VARCHAR(40),
    role_cd     VARCHAR(10),
    opt_out     CHAR(1),
    del_flag    CHAR(1),
    created     VARCHAR(30)
);

-- Opportunities. Amounts are text, with whatever the user typed.
CREATE TABLE IF NOT EXISTS ${LEGACY}.deal (
    dealno      VARCHAR(12) PRIMARY KEY,
    custno      VARCHAR(12),
    persno      VARCHAR(12),
    descr       VARCHAR(200),
    amount      VARCHAR(30),               -- '12 500,00', '$12,500.00', '12500'
    curr        VARCHAR(10),               -- 'EUR', '€', 'eur', ''
    stage_cd    VARCHAR(10),
    prob        VARCHAR(10),               -- '70', '70%', '0.7'
    close_dt    VARCHAR(30),
    owner_id    VARCHAR(10),
    del_flag    CHAR(1),
    created     VARCHAR(30)
);

-- Activity log. Also exported nightly to CSV, which is the copy keystone
-- reads -- see the file extractor.
CREATE TABLE IF NOT EXISTS ${LEGACY}.act (
    actno       VARCHAR(12) PRIMARY KEY,
    custno      VARCHAR(12),
    persno      VARCHAR(12),
    dealno      VARCHAR(12),
    act_type    VARCHAR(10),
    subj        VARCHAR(200),
    body        TEXT,
    act_dt      VARCHAR(30),
    owner_id    VARCHAR(10),
    del_flag    CHAR(1)
);

-- The one concession to sanity: indexes, so the extraction of 17 000 rows
-- does not take a coffee break.
CREATE INDEX IF NOT EXISTS ix_person_custno ON ${LEGACY}.person (custno);
CREATE INDEX IF NOT EXISTS ix_deal_custno   ON ${LEGACY}.deal (custno);
CREATE INDEX IF NOT EXISTS ix_act_custno    ON ${LEGACY}.act (custno);
