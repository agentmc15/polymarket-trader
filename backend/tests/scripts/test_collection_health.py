"""`app.scripts.collection_health` (mm-proveout T10).

Derived from TASKS.md T10's brief and acceptance lines, not from the
implementation: seeded `book_snapshots` rows must produce the exact
expected counts/shares/gap-detection numbers (hand-computed in each test
body, per GUARDRAILS.md §5's "every money-math test states the expected
number, computed by hand" convention -- these are not money-math, but the
same discipline applies to any figure a test asserts), an empty venue
must exit `1`, and the JSON schema must be stable.

Uses `tests/conftest.py`'s `test_session`/`test_engine` fixtures (SQLite,
`Base.metadata.create_all`) -- the same "existing conftest pattern" every
other model/service test in this repo uses, never a live database.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import app.scripts.collection_health as ch
from app.models.book_snapshot import BookSnapshot
from app.scripts.collection_health import (
    _DEFAULT_INTERVAL_S,
    _interval_s_and_source,
    compute_report,
    compute_venue_health,
)

NOW = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)


def _snapshot(**overrides: Any) -> BookSnapshot:
    fields: dict[str, Any] = {
        "venue": "kalshi",
        "market_id": "M1",
        "outcome": "YES",
        "ts": NOW,
        "bids": [{"price": 0.40, "size": 10.0}],
        "asks": [{"price": 0.55, "size": 10.0}],
        "tick_size": 0.01,
        "min_size": 1.0,
    }
    fields.update(overrides)
    return BookSnapshot(**fields)


async def _seed(session: AsyncSession, *rows: BookSnapshot) -> None:
    for row in rows:
        session.add(row)
    await session.commit()


# ---------------------------------------------------------------------------
# `_interval_s_and_source` -- the T9-race defensive read.
# ---------------------------------------------------------------------------


class _NoIntervalSettings:
    """Stands in for `Settings` before T9 lands `book_collection_interval_s`."""


class _WithIntervalSettings:
    book_collection_interval_s = 45.0


def test_interval_falls_back_to_default_when_setting_is_absent() -> None:
    value, source = _interval_s_and_source(_NoIntervalSettings())

    assert value == _DEFAULT_INTERVAL_S
    assert "default" in source
    assert "T9" in source


def test_interval_prefers_the_real_setting_when_present() -> None:
    value, source = _interval_s_and_source(_WithIntervalSettings())

    assert value == 45.0
    assert source == "settings"


def test_interval_reads_the_real_settings_singleton_not_the_fallback() -> None:
    """T9 has landed `Settings.book_collection_interval_s` (app/config.py,
    `default=60.0`) -- a value that is COINCIDENTALLY identical to this
    module's own `_DEFAULT_INTERVAL_S` fallback (both `60.0`, by design:
    the module docstring says the fallback mirrors "T9's own stated
    default"). That coincidence means a check of `value` alone can never
    tell "read the real setting" apart from "silently fell back" --
    exactly the shape of the T8 `taker_rate=0.07` trap. Every other test
    in this file calls `_interval_s_and_source` with a hand-built
    stand-in object (`_NoIntervalSettings`/`_WithIntervalSettings`);
    none of them exercises the getattr lookup against the ACTUAL
    `app.config.settings` singleton this script runs against in
    production. Only the `source` label -- not the value -- can catch a
    regression (e.g. a typo'd attribute name) that would silently fall
    back to the same number production already has."""
    from app.config import settings as real_settings

    value, source = _interval_s_and_source()

    assert value == real_settings.book_collection_interval_s
    assert source == "settings"
    assert "default" not in source


# ---------------------------------------------------------------------------
# `compute_venue_health` -- counts, shares, median, gaps.
# ---------------------------------------------------------------------------


class TestCounts:
    async def test_snapshot_and_distinct_counts(self, test_session: AsyncSession) -> None:
        await _seed(
            test_session,
            _snapshot(market_id="A", outcome="YES", ts=NOW - timedelta(hours=3)),
            _snapshot(market_id="A", outcome="NO", ts=NOW - timedelta(hours=1)),
            _snapshot(market_id="B", outcome="YES", ts=NOW - timedelta(minutes=30)),
        )

        health = await compute_venue_health(
            test_session, "kalshi", since=NOW - timedelta(hours=24), interval_s=60.0, now=NOW
        )

        assert health.n_snapshots == 3
        assert health.n_distinct_markets == 2  # {A, B}
        assert health.n_distinct_market_outcome == 3  # {(A,YES),(A,NO),(B,YES)}
        assert health.first_ts == NOW - timedelta(hours=3)
        assert health.last_ts == NOW - timedelta(minutes=30)

    async def test_window_excludes_rows_older_than_since(
        self, test_session: AsyncSession
    ) -> None:
        await _seed(
            test_session,
            _snapshot(market_id="OLD", ts=NOW - timedelta(hours=25)),
            _snapshot(market_id="NEW", ts=NOW - timedelta(hours=1)),
        )

        health = await compute_venue_health(
            test_session, "kalshi", since=NOW - timedelta(hours=24), interval_s=60.0, now=NOW
        )

        assert health.n_snapshots == 1
        assert health.n_distinct_markets == 1

    async def test_other_venue_rows_are_never_counted(
        self, test_session: AsyncSession
    ) -> None:
        await _seed(
            test_session,
            _snapshot(venue="kalshi", market_id="K1"),
            _snapshot(venue="polymarket", market_id="P1"),
        )

        kalshi = await compute_venue_health(
            test_session, "kalshi", since=NOW - timedelta(hours=24), interval_s=60.0, now=NOW
        )

        assert kalshi.n_snapshots == 1
        assert kalshi.n_distinct_markets == 1


class TestShares:
    async def test_share_volume_non_null_never_treats_none_as_zero(
        self, test_session: AsyncSession
    ) -> None:
        """3 of 4 rows carry a non-null `volume` (one is `None`, T8's
        pre-migration-008 case) -> 0.75. A row with `volume=0.0` (a real,
        observed zero) must count as PRESENT, not missing."""
        await _seed(
            test_session,
            _snapshot(market_id="A", outcome="YES", ts=NOW - timedelta(hours=3), volume=100.0),
            _snapshot(market_id="A", outcome="YES", ts=NOW - timedelta(hours=2), volume=None),
            _snapshot(market_id="A", outcome="NO", ts=NOW - timedelta(hours=1), volume=0.0),
            _snapshot(market_id="B", outcome="YES", ts=NOW - timedelta(minutes=30), volume=200.0),
        )

        health = await compute_venue_health(
            test_session, "kalshi", since=NOW - timedelta(hours=24), interval_s=60.0, now=NOW
        )

        assert health.n_snapshots == 4
        assert health.share_volume_non_null == pytest.approx(0.75)

    async def test_share_two_sided_counts_only_rows_with_both_sides_present(
        self, test_session: AsyncSession
    ) -> None:
        """2 of 4 rows have BOTH bids and asks non-empty -> 0.5."""
        await _seed(
            test_session,
            _snapshot(
                market_id="A", outcome="YES", ts=NOW - timedelta(hours=3),
                bids=[{"price": 0.4, "size": 1.0}], asks=[{"price": 0.5, "size": 1.0}],
            ),
            _snapshot(
                market_id="A", outcome="YES", ts=NOW - timedelta(hours=2),
                bids=[{"price": 0.4, "size": 1.0}], asks=[],
            ),
            _snapshot(
                market_id="A", outcome="NO", ts=NOW - timedelta(hours=1),
                bids=[], asks=[{"price": 0.5, "size": 1.0}],
            ),
            _snapshot(
                market_id="B", outcome="YES", ts=NOW - timedelta(minutes=30),
                bids=[{"price": 0.4, "size": 1.0}], asks=[{"price": 0.5, "size": 1.0}],
            ),
        )

        health = await compute_venue_health(
            test_session, "kalshi", since=NOW - timedelta(hours=24), interval_s=60.0, now=NOW
        )

        assert health.share_two_sided == pytest.approx(0.5)

    async def test_shares_are_none_when_venue_has_no_snapshots(
        self, test_session: AsyncSession
    ) -> None:
        health = await compute_venue_health(
            test_session, "kalshi", since=NOW - timedelta(hours=24), interval_s=60.0, now=NOW
        )

        assert health.n_snapshots == 0
        assert health.share_volume_non_null is None
        assert health.share_two_sided is None
        assert health.median_seconds_between_snapshots is None
        assert health.n_gaps_over_2x_interval == 0
        assert health.first_ts is None
        assert health.last_ts is None
        # Staleness is undefined, not "maximally stale", when there is no
        # data at all -- that failure is `empty_venues`'s, not this one's
        # (see `VenueHealth.is_stale`'s docstring). The threshold itself
        # is still always computed (it doesn't depend on any row).
        assert health.seconds_since_last_snapshot is None
        assert health.is_stale is False
        assert health.staleness_threshold_s == pytest.approx(2 * 60.0)


class TestMedianAndGaps:
    async def test_median_and_gap_count_pooled_across_groups(
        self, test_session: AsyncSession
    ) -> None:
        """Two `(market, outcome)` groups. Group C/YES has deltas 60s then
        240s; group D/NO has delta 30s -- pooled deltas sorted are
        [30, 60, 240], median = 60. At `interval_s=60` the gap threshold
        is `2*60=120`; only the 240s delta exceeds it -> 1 gap."""
        t0 = NOW - timedelta(hours=1)
        await _seed(
            test_session,
            _snapshot(market_id="C", outcome="YES", ts=t0),
            _snapshot(market_id="C", outcome="YES", ts=t0 + timedelta(seconds=60)),
            _snapshot(market_id="C", outcome="YES", ts=t0 + timedelta(seconds=300)),
            _snapshot(market_id="D", outcome="NO", ts=t0),
            _snapshot(market_id="D", outcome="NO", ts=t0 + timedelta(seconds=30)),
        )

        health = await compute_venue_health(
            test_session, "kalshi", since=NOW - timedelta(hours=24), interval_s=60.0, now=NOW
        )

        assert health.median_seconds_between_snapshots == pytest.approx(60.0)
        assert health.n_gaps_over_2x_interval == 1

    async def test_a_group_with_a_single_row_contributes_no_delta(
        self, test_session: AsyncSession
    ) -> None:
        await _seed(test_session, _snapshot(market_id="A", outcome="YES", ts=NOW))

        health = await compute_venue_health(
            test_session, "kalshi", since=NOW - timedelta(hours=24), interval_s=60.0, now=NOW
        )

        assert health.n_snapshots == 1
        assert health.median_seconds_between_snapshots is None
        assert health.n_gaps_over_2x_interval == 0

    async def test_grouping_is_by_market_and_outcome_together_not_either_alone(
        self, test_session: AsyncSession
    ) -> None:
        """Three (market, outcome) series, deliberately overlapping on ONE
        key each so that grouping by `market_id` alone or by `outcome`
        alone -- instead of the tuple `(market_id, outcome)` -- pools rows
        across a series boundary and produces a DIFFERENT median:

        - (M1, A): ts = t0, t0+10s          -> delta 10
        - (M1, B): ts = t0+5s, t0+205s       -> delta 200   (shares market M1 with the row above)
        - (M2, A): ts = t0+100s, t0+400s     -> delta 300   (shares outcome A with the first row)

        Correct per-(market, outcome) grouping: deltas = [10, 200, 300],
        median = 200.

        Grouping by `market_id` ONLY would merge (M1,A) and (M1,B) into
        one 4-row series (t0, t0+5, t0+10, t0+205) contributing deltas
        [5, 5, 195], plus (M2, A)'s 300 -> pooled [5, 5, 195, 300],
        median = 100 -- wrong.

        Grouping by `outcome` ONLY would merge (M1,A) and (M2,A) into one
        4-row series (t0, t0+10, t0+100, t0+400) contributing deltas
        [10, 90, 300], plus (M1, B)'s 200 -> pooled [10, 90, 200, 300],
        median = 145 -- also wrong.

        Only the correct per-(market_id, outcome) grouping yields 200.
        """
        t0 = NOW - timedelta(hours=2)
        await _seed(
            test_session,
            _snapshot(market_id="M1", outcome="A", ts=t0),
            _snapshot(market_id="M1", outcome="A", ts=t0 + timedelta(seconds=10)),
            _snapshot(market_id="M1", outcome="B", ts=t0 + timedelta(seconds=5)),
            _snapshot(market_id="M1", outcome="B", ts=t0 + timedelta(seconds=205)),
            _snapshot(market_id="M2", outcome="A", ts=t0 + timedelta(seconds=100)),
            _snapshot(market_id="M2", outcome="A", ts=t0 + timedelta(seconds=400)),
        )

        health = await compute_venue_health(
            test_session, "kalshi", since=NOW - timedelta(hours=24), interval_s=60.0, now=NOW
        )

        assert health.median_seconds_between_snapshots == pytest.approx(200.0)
        # threshold = 2*60 = 120: only 200 and 300 exceed it -> 2 gaps.
        assert health.n_gaps_over_2x_interval == 2

    async def test_gap_exactly_at_threshold_does_not_count(
        self, test_session: AsyncSession
    ) -> None:
        """`n_gaps_over_2x_interval` must use a STRICT `>`, not `>=`: a
        delta of EXACTLY `2*interval_s` (here `2*60=120`) is the boundary
        the brief describes as "gaps > 2x interval", not ">= 2x interval",
        so it must NOT be counted."""
        t0 = NOW - timedelta(hours=1)
        await _seed(
            test_session,
            _snapshot(market_id="EXACT", outcome="YES", ts=t0),
            _snapshot(market_id="EXACT", outcome="YES", ts=t0 + timedelta(seconds=120)),
        )

        health = await compute_venue_health(
            test_session, "kalshi", since=NOW - timedelta(hours=24), interval_s=60.0, now=NOW
        )

        assert health.median_seconds_between_snapshots == pytest.approx(120.0)
        assert health.n_gaps_over_2x_interval == 0

    async def test_gap_just_over_threshold_counts(
        self, test_session: AsyncSession
    ) -> None:
        """The mirror of the exactly-at-threshold case: a delta ONE
        microsecond past `2*interval_s` must count, proving the threshold
        isn't accidentally padded/rounded to exclude near-boundary gaps
        too."""
        t0 = NOW - timedelta(hours=1)
        await _seed(
            test_session,
            _snapshot(market_id="OVER", outcome="YES", ts=t0),
            _snapshot(
                market_id="OVER",
                outcome="YES",
                ts=t0 + timedelta(seconds=120, microseconds=1),
            ),
        )

        health = await compute_venue_health(
            test_session, "kalshi", since=NOW - timedelta(hours=24), interval_s=60.0, now=NOW
        )

        assert health.n_gaps_over_2x_interval == 1


class TestGapSemantics:
    def test_kalshi_and_polymarket_carry_distinct_gap_semantics(self) -> None:
        assert "kalshi" in ch._GAP_SEMANTICS
        assert "polymarket" in ch._GAP_SEMANTICS
        assert ch._GAP_SEMANTICS["kalshi"] != ch._GAP_SEMANTICS["polymarket"]
        # Kalshi's note says a gap CAN be trusted; Polymarket's warns
        # against reading one as an outage on its own -- the two must not
        # say the same thing about the same statistic.
        assert "utcnow" in ch._GAP_SEMANTICS["kalshi"]
        assert "quiet" in ch._GAP_SEMANTICS["polymarket"]


# ---------------------------------------------------------------------------
# Staleness (`now - last_ts`) -- mm-proveout T10 RETRY. The pre-fix
# `compute_venue_health` computed gaps only between CONSECUTIVE stored
# rows (`zip(ts_list, ts_list[1:])`), never comparing the newest `ts` to
# report-generation time. Once a collector for a venue died, the rows it
# had already written stayed perfectly on-cadence forever, and nothing
# noticed. Both tests below are the red team's exact reproductions
# (TASKS.md T10 retry brief) against seeded SQLite: they must fail on
# the pre-fix code and pass once `seconds_since_last_snapshot`/
# `is_stale` are wired into `exit_code`.
# ---------------------------------------------------------------------------


class TestStaleness:
    async def test_kalshi_goes_silent_for_the_final_hour_is_reported_stale(
        self, test_session: AsyncSession
    ) -> None:
        """Red-team repro 1. Kalshi polls every 60s from `NOW-3h` to
        `NOW-1h` (121 rows, perfectly on-cadence) then goes silent for
        the final hour -- 60 missed 60s polls. Polymarket gets one
        healthy, fresh row (ts=NOW) so `empty_venues` stays `[]` and
        staleness is the ONLY signal that can catch this.

        Pre-fix (reproduced live): `median_seconds_between_snapshots
        == 60.0`, `n_gaps_over_2x_interval == 0`, `empty_venues == []`,
        `exit_code == 0` -- an hour-dead collector reads as healthy
        because every gap it ever measures is between two rows that
        both predate the outage.

        Post-fix: kalshi's `seconds_since_last_snapshot` is `3600.0`,
        which exceeds kalshi's `2 * interval_s = 120` staleness
        threshold, so `is_stale=True`, `"kalshi" in stale_venues`, and
        `exit_code == 1`.
        """
        t_start = NOW - timedelta(hours=3)
        t_end = NOW - timedelta(hours=1)
        n_steps = int((t_end - t_start).total_seconds() // 60) + 1  # 121
        rows = [
            _snapshot(
                venue="kalshi",
                market_id="M1",
                outcome="YES",
                ts=t_start + timedelta(seconds=60 * i),
            )
            for i in range(n_steps)
        ]
        rows.append(_snapshot(venue="polymarket", market_id="P1", outcome="YES", ts=NOW))
        await _seed(test_session, *rows)

        report = await compute_report(test_session, hours=24.0, interval_s=60.0, now=NOW)
        kalshi = report.venues["kalshi"]

        assert kalshi.n_snapshots == 121
        assert kalshi.median_seconds_between_snapshots == pytest.approx(60.0)
        assert kalshi.n_gaps_over_2x_interval == 0
        assert report.empty_venues == []
        # The defect, made concrete: the OLD report called this OK.
        assert report.exit_code == 1
        assert kalshi.seconds_since_last_snapshot == pytest.approx(3600.0)
        assert kalshi.is_stale is True
        assert "kalshi" in report.stale_venues

    async def test_one_23_hour_old_kalshi_row_in_a_24_hour_window_is_stale(
        self, test_session: AsyncSession
    ) -> None:
        """Red-team repro 2. A single Kalshi row, 23 hours old, inside a
        24-hour window -- with no second row in its `(market, outcome)`
        group, the old pairs-only gap check has nothing to diff it
        against at all. Polymarket gets one healthy, fresh row (ts=NOW)
        so `empty_venues` stays `[]`, isolating the staleness signal.

        Pre-fix (reproduced live): `n_snapshots == 1`,
        `median_seconds_between_snapshots is None`,
        `n_gaps_over_2x_interval == 0`, `exit_code == 0`.

        Post-fix: `seconds_since_last_snapshot == 82800.0` (23h) is far
        past kalshi's `2 * interval_s = 120` threshold, so
        `is_stale=True` and `exit_code == 1`.
        """
        await _seed(
            test_session,
            _snapshot(venue="kalshi", market_id="M1", ts=NOW - timedelta(hours=23)),
            _snapshot(venue="polymarket", market_id="P1", ts=NOW),
        )

        report = await compute_report(test_session, hours=24.0, interval_s=60.0, now=NOW)
        kalshi = report.venues["kalshi"]

        assert kalshi.n_snapshots == 1
        assert kalshi.median_seconds_between_snapshots is None
        assert kalshi.n_gaps_over_2x_interval == 0
        assert report.empty_venues == []
        # The defect, made concrete: the OLD report called this OK.
        assert report.exit_code == 1
        assert kalshi.seconds_since_last_snapshot == pytest.approx(82800.0)
        assert kalshi.is_stale is True


# ---------------------------------------------------------------------------
# `compute_report` -- both venues, empty-venue exit code, JSON schema.
# ---------------------------------------------------------------------------


class TestReport:
    async def test_report_covers_both_venues(self, test_session: AsyncSession) -> None:
        await _seed(
            test_session,
            _snapshot(venue="kalshi", market_id="K1"),
            _snapshot(venue="polymarket", market_id="P1"),
        )

        report = await compute_report(
            test_session, hours=24.0, interval_s=60.0, now=NOW
        )

        assert set(report.venues) == {"kalshi", "polymarket"}
        assert report.venues["kalshi"].n_snapshots == 1
        assert report.venues["polymarket"].n_snapshots == 1
        assert report.exit_code == 0
        assert report.empty_venues == []

    async def test_one_empty_venue_sets_exit_code_1(
        self, test_session: AsyncSession
    ) -> None:
        await _seed(test_session, _snapshot(venue="kalshi", market_id="K1"))

        report = await compute_report(
            test_session, hours=24.0, interval_s=60.0, now=NOW
        )

        assert report.exit_code == 1
        assert report.empty_venues == ["polymarket"]

    async def test_both_venues_empty_lists_both_kalshi_first(
        self, test_session: AsyncSession
    ) -> None:
        report = await compute_report(
            test_session, hours=24.0, interval_s=60.0, now=NOW
        )

        assert report.exit_code == 1
        assert report.empty_venues == ["kalshi", "polymarket"]

    async def test_rows_outside_the_window_still_count_as_empty(
        self, test_session: AsyncSession
    ) -> None:
        """A venue with rows in the TABLE but none inside the `--hours`
        window is exactly as unhealthy as a venue with no rows at all --
        the report must not be fooled by stale history."""
        await _seed(
            test_session,
            _snapshot(venue="kalshi", market_id="K1", ts=NOW - timedelta(hours=48)),
        )

        report = await compute_report(
            test_session, hours=24.0, interval_s=60.0, now=NOW
        )

        assert report.venues["kalshi"].n_snapshots == 0
        assert "kalshi" in report.empty_venues
        assert report.exit_code == 1

    async def test_nonpositive_hours_raises(self, test_session: AsyncSession) -> None:
        with pytest.raises(ValueError):
            await compute_report(test_session, hours=0.0, interval_s=60.0, now=NOW)


class TestJSONSchema:
    async def test_to_dict_schema_is_stable(self, test_session: AsyncSession) -> None:
        await _seed(test_session, _snapshot(venue="kalshi", market_id="K1", volume=1.0))

        report = await compute_report(
            test_session, hours=24.0, interval_s=60.0, interval_source="settings", now=NOW
        )
        payload = report.to_dict()

        assert set(payload) == {
            "generated_at",
            "hours",
            "since",
            "book_collection_interval_s",
            "book_collection_interval_s_source",
            "venues",
            "empty_venues",
            "stale_venues",
            "exit_code",
        }
        assert set(payload["venues"]) == {"kalshi", "polymarket"}
        for venue_payload in payload["venues"].values():
            assert set(venue_payload) == {
                "venue",
                "n_snapshots",
                "n_distinct_markets",
                "n_distinct_market_outcome",
                "share_volume_non_null",
                "share_two_sided",
                "median_seconds_between_snapshots",
                "n_gaps_over_2x_interval",
                "first_ts",
                "last_ts",
                "seconds_since_last_snapshot",
                "staleness_threshold_s",
                "is_stale",
                "gap_semantics",
            }
        # JSON-round-trips cleanly (datetimes are ISO strings, not objects).
        json.loads(json.dumps(payload))
        assert payload["venues"]["kalshi"]["first_ts"] == NOW.isoformat()
        assert payload["venues"]["polymarket"]["first_ts"] is None
        assert payload["exit_code"] == 1
        # kalshi's lone row is exactly `NOW` (fresh, seconds_since_last_snapshot=0)
        # -- polymarket's empty-venue failure is what drives exit_code here, not
        # staleness; a stable schema still carries the full key set for the
        # empty venue too (never a missing key).
        assert payload["venues"]["kalshi"]["seconds_since_last_snapshot"] == pytest.approx(0.0)
        assert payload["venues"]["kalshi"]["is_stale"] is False
        assert payload["venues"]["polymarket"]["seconds_since_last_snapshot"] is None
        assert payload["venues"]["polymarket"]["is_stale"] is False
        assert payload["venues"]["polymarket"]["staleness_threshold_s"] == pytest.approx(15 * 60.0)
        assert payload["stale_venues"] == []


class TestRender:
    async def test_render_names_both_venues_and_flags_the_empty_one(
        self, test_session: AsyncSession
    ) -> None:
        await _seed(test_session, _snapshot(venue="kalshi", market_id="K1"))

        report = await compute_report(
            test_session, hours=24.0, interval_s=60.0, now=NOW
        )
        text = report.render()

        assert "kalshi" in text
        assert "polymarket" in text
        assert "NOTE kalshi" in text
        assert "NOTE polymarket" in text
        assert "FAIL: polymarket" in text


# ---------------------------------------------------------------------------
# `_run` -- the real CLI seam, exercised against SQLite via a monkeypatched
# `async_session_factory` (never a live database -- GUARDRAILS.md §1.4).
# ---------------------------------------------------------------------------


class TestRunEndToEnd:
    async def test_run_writes_json_and_returns_1_when_both_venues_empty(
        self,
        test_engine: AsyncEngine,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Any,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        session_factory = async_sessionmaker(
            test_engine, class_=AsyncSession, expire_on_commit=False
        )
        monkeypatch.setattr(ch, "async_session_factory", session_factory)
        out_path = tmp_path / "health.json"

        exit_code = await ch._run(24.0, str(out_path))

        assert exit_code == 1
        payload = json.loads(out_path.read_text())
        assert payload["exit_code"] == 1
        assert set(payload["empty_venues"]) == {"kalshi", "polymarket"}
        err = capsys.readouterr().err
        assert "FAIL" in err

    async def test_run_returns_0_when_both_venues_have_snapshots(
        self,
        test_engine: AsyncEngine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`_run` never receives a `now` override (that seam is
        production-only, see `compute_report`'s docstring), so it always
        measures staleness against the REAL `utcnow()` at call time. Both
        seeded rows must therefore carry a REAL, fresh `ts` (`utcnow()`,
        not the fixed `NOW` constant every other test in this file uses)
        -- seeding with `NOW` would make this test's pass/fail depend on
        whether the suite happens to run before or after `NOW`'s
        wall-clock moment on `NOW`'s own calendar date, which is exactly
        the kind of accidental time-dependence GUARDRAILS.md's "aware
        UTC only" discipline exists to rule out.
        """
        from app.utils.time import utcnow

        session_factory = async_sessionmaker(
            test_engine, class_=AsyncSession, expire_on_commit=False
        )
        fresh = utcnow()
        async with session_factory() as session:
            await _seed(
                session,
                _snapshot(venue="kalshi", market_id="K1", ts=fresh),
                _snapshot(venue="polymarket", market_id="P1", ts=fresh),
            )
        monkeypatch.setattr(ch, "async_session_factory", session_factory)

        exit_code = await ch._run(24.0, None)

        assert exit_code == 0

    async def test_run_returns_1_when_only_one_venue_is_empty(
        self,
        test_engine: AsyncEngine,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Any,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The brief says "exit 1 if EITHER venue has zero snapshots" --
        the mixed case (one venue healthy, one empty) is the interesting
        one a "both empty" test cannot pin: it proves `_run`'s exit code
        isn't computed as `n_snapshots(kalshi) == 0 and
        n_snapshots(polymarket) == 0` (an AND that would wrongly return 0
        here) but as an OR across venues, all the way through the real
        CLI seam (not just `compute_report` in isolation). It also checks
        that the FAIL message names the empty venue specifically, not
        both.

        Kalshi's row must be seeded at a REAL, fresh `utcnow()` -- not the
        fixed `NOW` constant -- for the same reason as
        `test_run_returns_0_when_both_venues_have_snapshots`: `_run` never
        receives a `now` override, so it always measures staleness against
        wall-clock time at call time. A hardcoded `NOW` eventually falls
        further in the past than kalshi's staleness threshold, at which
        point kalshi would ALSO be reported (via `stale_venues`), breaking
        `assert "kalshi" not in err` below through no fault of the
        assertion -- exactly the T10 fixture defect already fixed once in
        this file and swept for here.
        """
        from app.utils.time import utcnow

        session_factory = async_sessionmaker(
            test_engine, class_=AsyncSession, expire_on_commit=False
        )
        fresh = utcnow()
        async with session_factory() as session:
            await _seed(session, _snapshot(venue="kalshi", market_id="K1", ts=fresh))
        monkeypatch.setattr(ch, "async_session_factory", session_factory)
        out_path = tmp_path / "health.json"

        exit_code = await ch._run(24.0, str(out_path))

        assert exit_code == 1
        payload = json.loads(out_path.read_text())
        assert payload["exit_code"] == 1
        assert payload["empty_venues"] == ["polymarket"]
        assert payload["venues"]["kalshi"]["n_snapshots"] == 1
        assert payload["venues"]["polymarket"]["n_snapshots"] == 0
        err = capsys.readouterr().err
        assert "FAIL: polymarket" in err
        assert "kalshi" not in err

    async def test_run_returns_1_and_warns_on_stderr_when_a_venue_is_stale_but_not_empty(
        self,
        test_engine: AsyncEngine,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Staleness must be wired into the SAME stderr FAIL path
        `empty_venues` already uses, all the way through the real `_run`
        CLI seam -- not just into `exit_code`/JSON. Kalshi's lone row is
        real-`utcnow()`-minus-23h (definitely stale, definitely NOT
        empty); Polymarket gets a real, fresh row so it is neither empty
        nor stale, isolating the staleness-only failure path."""
        from app.utils.time import utcnow

        session_factory = async_sessionmaker(
            test_engine, class_=AsyncSession, expire_on_commit=False
        )
        fresh = utcnow()
        async with session_factory() as session:
            await _seed(
                session,
                _snapshot(venue="kalshi", market_id="K1", ts=fresh - timedelta(hours=23)),
                _snapshot(venue="polymarket", market_id="P1", ts=fresh),
            )
        monkeypatch.setattr(ch, "async_session_factory", session_factory)

        exit_code = await ch._run(24.0, None)

        assert exit_code == 1
        err = capsys.readouterr().err
        assert "FAIL: kalshi" in err
        assert "staleness threshold" in err
        assert "polymarket" not in err
