"""`BookSnapshot.volume`/`taker_fee_rate`/`maker_rebate_rate` (mm-proveout
T8, PLAN.md D9, migration `008`) and `volume_lifetime`/`observed_at`/
`fee_source`/`maker_fee_rate` (mm-proveout T15, same migration).

Uses the `test_session` fixture from `tests/conftest.py`, which runs
`Base.metadata.create_all` on an in-memory SQLite engine -- so these
tests exercise the SAME schema the hand-written migration `008` must
produce (verified separately, offline, via `alembic upgrade head --sql`
per GUARDRAILS.md §2), not a second, drifted definition of it. All seven
new columns are nullable with no backfill (a row collected before `008`
has no venue call available to reconstruct a historical volume or fee),
so both a fully-populated row and an all-`None` row must round-trip, and
the pre-existing unique constraint on `(venue, market_id, outcome, ts)`
must be untouched by their addition.

`_upsert_book_snapshot`'s monotonicity guard on `volume_lifetime` and its
unconditional write of `observed_at` on a same-`ts` refresh are exercised
through `collect_books` in `tests/services/test_book_collection_selection.py`
instead of here -- both need a `VenueMarket`/`FixtureAdapter` round trip,
which is that file's fixture, not this one's.
"""
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.book_snapshot import BookSnapshot


def _snapshot(**overrides: object) -> BookSnapshot:
    fields: dict[str, object] = {
        "venue": "kalshi",
        "market_id": "TICKER-1",
        "outcome": "YES",
        "ts": datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC),
        "bids": [{"price": 0.40, "size": 10.0}],
        "asks": [{"price": 0.55, "size": 10.0}],
        "tick_size": 0.01,
        "min_size": 1.0,
    }
    fields.update(overrides)
    return BookSnapshot(**fields)


class TestVolumeAndFeeRoundTrip:
    """Both a populated row and an all-`None` row round-trip on SQLite."""

    async def test_row_with_volume_and_fee_round_trips(
        self, test_session: AsyncSession
    ) -> None:
        observed_at = datetime(2026, 9, 7, 12, 0, 30, tzinfo=UTC)
        row = _snapshot(
            volume=1234.5,
            taker_fee_rate=0.07,
            maker_rebate_rate=0.0175,
            volume_lifetime=54321.0,
            observed_at=observed_at,
            fee_source="venue_schedule",
            maker_fee_rate=0.0,
        )
        test_session.add(row)
        await test_session.commit()

        fetched = await test_session.get(BookSnapshot, row.id)
        assert fetched is not None
        assert fetched.volume == 1234.5
        assert fetched.taker_fee_rate == 0.07
        assert fetched.maker_rebate_rate == 0.0175
        assert fetched.volume_lifetime == 54321.0
        assert fetched.observed_at.replace(tzinfo=UTC) == observed_at
        assert fetched.fee_source == "venue_schedule"
        assert fetched.maker_fee_rate == 0.0

    async def test_row_with_all_seven_none_round_trips(
        self, test_session: AsyncSession
    ) -> None:
        """A row collected before migration `008` has all seven `NULL` --
        this must not raise `IntegrityError` or coerce to `0.0`."""
        row = _snapshot(
            market_id="TICKER-2",
            volume=None,
            taker_fee_rate=None,
            maker_rebate_rate=None,
            volume_lifetime=None,
            observed_at=None,
            fee_source=None,
            maker_fee_rate=None,
        )
        test_session.add(row)
        await test_session.commit()

        fetched = await test_session.get(BookSnapshot, row.id)
        assert fetched is not None
        assert fetched.volume is None
        assert fetched.taker_fee_rate is None
        assert fetched.maker_rebate_rate is None
        assert fetched.volume_lifetime is None
        assert fetched.observed_at is None
        assert fetched.fee_source is None
        assert fetched.maker_fee_rate is None

    async def test_taker_fee_rate_and_maker_rebate_rate_round_trip_independently(
        self, test_session: AsyncSession
    ) -> None:
        """Adversarial: every other value in this file uses `0.07`/
        `0.0175`, a pair close enough together that an accidental column
        swap at the ORM/DB boundary could still look plausible on a
        cursory read. Distinct, non-round values (`0.13` vs `0.31`, and
        neither equal to `volume`) make a swap or cross-contamination
        between the two fee columns unmistakable if it ever happened."""
        row = _snapshot(
            market_id="TICKER-5",
            volume=123456.789,
            taker_fee_rate=0.13,
            maker_rebate_rate=0.31,
        )
        test_session.add(row)
        await test_session.commit()

        fetched = await test_session.get(BookSnapshot, row.id)
        assert fetched is not None
        assert fetched.volume == 123456.789
        assert fetched.taker_fee_rate == 0.13
        assert fetched.maker_rebate_rate == 0.31
        assert fetched.taker_fee_rate != fetched.maker_rebate_rate
        assert fetched.taker_fee_rate != fetched.volume
        assert fetched.maker_rebate_rate != fetched.volume

    async def test_omitting_the_seven_columns_entirely_also_round_trips(
        self, test_session: AsyncSession
    ) -> None:
        """Not just `None` passed explicitly -- the ORM default (no
        argument at all, the shape every pre-T8/pre-T15 caller still
        uses) must also leave them `NULL`, not raise for a missing
        required column."""
        row = _snapshot(market_id="TICKER-3")
        test_session.add(row)
        await test_session.commit()

        fetched = await test_session.get(BookSnapshot, row.id)
        assert fetched is not None
        assert fetched.volume is None
        assert fetched.taker_fee_rate is None
        assert fetched.maker_rebate_rate is None
        assert fetched.volume_lifetime is None
        assert fetched.observed_at is None
        assert fetched.fee_source is None
        assert fetched.maker_fee_rate is None


class TestUniqueConstraintUnchanged:
    """`uq_book_snapshots_venue_market_outcome_ts` still fires -- the new
    nullable columns must not have widened or weakened it."""

    async def test_duplicate_natural_key_still_conflicts(
        self, test_session: AsyncSession
    ) -> None:
        first = _snapshot(volume=100.0, taker_fee_rate=0.07, maker_rebate_rate=0.0)
        test_session.add(first)
        await test_session.commit()

        # Same (venue, market_id, outcome, ts); different volume/fee --
        # proves the constraint is still keyed on the original four
        # columns only, not accidentally extended to include the new ones.
        test_session.add(_snapshot(volume=999.0, taker_fee_rate=0.99, maker_rebate_rate=0.5))
        with pytest.raises(IntegrityError):
            await test_session.commit()
        await test_session.rollback()

        rows = (
            await test_session.execute(
                select(BookSnapshot).where(BookSnapshot.market_id == "TICKER-1")
            )
        ).scalars().all()
        assert len(rows) == 1

    async def test_different_ts_is_not_a_conflict(
        self, test_session: AsyncSession
    ) -> None:
        """Sanity check that the constraint (and the new columns) don't
        over-restrict a genuinely new observation."""
        test_session.add(_snapshot(market_id="TICKER-4", volume=1.0))
        test_session.add(
            _snapshot(
                market_id="TICKER-4",
                ts=datetime(2026, 9, 7, 13, 0, 0, tzinfo=UTC),
                volume=2.0,
            )
        )
        await test_session.commit()

        rows = (
            await test_session.execute(
                select(BookSnapshot).where(BookSnapshot.market_id == "TICKER-4")
            )
        ).scalars().all()
        assert len(rows) == 2
