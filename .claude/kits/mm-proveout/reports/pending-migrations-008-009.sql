BEGIN;

-- Running upgrade 007 -> 008

ALTER TABLE book_snapshots ADD COLUMN volume FLOAT;

ALTER TABLE book_snapshots ADD COLUMN taker_fee_rate FLOAT;

ALTER TABLE book_snapshots ADD COLUMN maker_rebate_rate FLOAT;

ALTER TABLE book_snapshots ADD COLUMN volume_lifetime FLOAT;

ALTER TABLE book_snapshots ADD COLUMN observed_at TIMESTAMP WITH TIME ZONE;

ALTER TABLE book_snapshots ADD COLUMN fee_source VARCHAR(32);

ALTER TABLE book_snapshots ADD COLUMN maker_fee_rate FLOAT;

UPDATE alembic_version SET version_num='008' WHERE alembic_version.version_num = '007';

-- Running upgrade 008 -> 009

CREATE TABLE selection_membership (
    id SERIAL NOT NULL, 
    venue VARCHAR(16) NOT NULL, 
    market_id VARCHAR(128) NOT NULL, 
    selected BOOLEAN NOT NULL, 
    last_selected_at TIMESTAMP WITH TIME ZONE, 
    last_deselected_at TIMESTAMP WITH TIME ZONE, 
    PRIMARY KEY (id), 
    CONSTRAINT uq_selection_membership_venue_market UNIQUE (venue, market_id), 
    CONSTRAINT ck_selection_membership_venue_valid CHECK (venue IN ('polymarket', 'kalshi'))
);

UPDATE alembic_version SET version_num='009' WHERE alembic_version.version_num = '008';

COMMIT;

