"""Celery task for the periodic cross-venue link proposal (T30, PLAN.md D9).

`propose_event_links` runs `app.services.matching.propose_links` over both
venues' open-market lists once, on the beat schedule in
`app/tasks/__init__.py` (every `settings.link_proposal_interval_s`, 3600s
by default), and files the results with
`app.services.matching.persist.persist_proposals`.

THE DEFECT THIS CLOSES. `propose_links` had exactly one caller: the
manual `POST /links/propose` endpoint. There was no beat and no other
scheduled job, so on a fresh install `event_links` started empty and
stayed empty until a human curled that endpoint by hand — and
`app.services.scanner._build_strategies` filters links to
`status="approved"` before it constructs `LinkBook`, so
`cross_venue_arbitrage`, the centerpiece strategy, was handed nothing and
scanned nothing, forever. The strategy was never broken; it was unfed.
This module is the feeder. (Same class of defect as the near-resolution
pass having no periodic caller before T25 — see `app/tasks/scanner.py`.)

IT PROPOSES. IT DOES NOT APPROVE, AND IT MUST NEVER LEARN TO. Everything
this task writes is `"proposed"`; the promotion of a link to `approved`
happens in `app/api/routes/links.py`, driven by a person looking at both
venues' `rules_text` side by side, and nowhere else. An automatic
proposer makes auto-approving high-confidence pairs tempting — the
pipeline would then flow end to end with no human in it — and that
temptation is precisely what PLAN.md R1 forbids. `confidence` is a
similarity score, not a guarantee of identity: two contracts that read
alike can settle on different facts (different resolution source,
different timezone cutoff, different handling of a tie or an annulment).
A false link is not a missed opportunity; it is a position that believes
it is hedged while both legs can lose at settlement. The structural
guarantees, in three layers: this module contains no lifecycle
assignment at all, `persist_proposals` raises `ValueError` on any
proposal not carrying `PROPOSED`, and
`app.strategies.cross_venue_arbitrage.LinkBook` raises on any link that
is not already approved.

IT ALSO DOES NOT ROUTE, for the reason `app/tasks/scanner.py` documents
at length: `app.execution.router` is not imported here, and this task's
only output is rows in a review queue.

ADAPTERS COME FROM `app.tasks.scanner.read_adapters`, deliberately
reused rather than re-implemented — it is the one place the
`get_read_adapter` (never `get_adapter`) policy for periodic tasks is
written down. In `"live"` mode `get_adapter` would construct an
order-PLACING adapter behind `assert_live_allowed()`; this task places
nothing, ever, and an engaged kill switch has no bearing on whether a
review queue keeps filling.
"""
import logging
from itertools import combinations
from typing import Any

from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.database import async_session_factory, run_async_task
from app.models.event_link import EventLink
from app.services.matching import persist_proposals, propose_links
from app.tasks import celery_app
from app.tasks.scanner import read_adapters
from app.venues.base import MarketDataAdapter
from app.venues.types import VenueId, VenueMarket

logger = logging.getLogger(__name__)


async def _open_markets(
    venue: VenueId, adapter: MarketDataAdapter
) -> list[VenueMarket] | None:
    """Read one venue's OPEN markets, or `None` if it cannot be read.

    `status="open"` mirrors `POST /links/propose` exactly: a closed or
    settled market cannot be one leg of a future trade, and proposing a
    pair a reviewer can no longer act on only dilutes the queue.

    A venue that fails is skipped rather than fatal — the same "one
    venue's trouble does not blank out the other's work" policy
    `app.tasks.scanner.read_adapters` uses one level up. With only one
    venue readable there is simply no pair to match, which is a pass that
    proposes nothing rather than an error.

    Args:
        venue: The venue being read, for the log line.
        adapter: Its read-only market-data adapter.

    Returns:
        list[VenueMarket] | None: The open markets, or `None` if the
            venue could not be listed.
    """
    try:
        markets: list[VenueMarket] = await adapter.list_markets(status="open")
    except Exception as exc:  # noqa: BLE001 - one venue must not stop the rest
        logger.warning(
            "link_proposal",
            extra={
                "event": "link_proposal_venue_unreadable",
                "venue": venue,
                "reason": type(exc).__name__,
            },
        )
        return None
    return markets


async def run_link_proposal() -> dict[str, Any]:
    """Score every cross-venue market pair and file the proposals.

    The whole pass: read each venue's open markets, run the deterministic
    matcher over each unordered pair of venues, and reconcile the result
    against `event_links` without ever overwriting a human's decision
    (`persist_proposals` owns those four cases and documents them).

    `min_confidence` is left to `propose_links`' own default, which is
    PLAN.md D9's floor of 0.5 — deliberately NOT a second knob here. The
    floor governs what a reviewer is asked to look at; it has never
    governed what may trade, and a beat that could widen it would only
    change the size of the queue.

    Venue pairs are derived from the adapters that could be built rather
    than hard-coded, so a third venue in
    `app.tasks.scanner.SCANNED_VENUES` starts being matched against both
    existing ones with no change here.

    Returns:
        dict[str, Any]: `{"mode", "scanned_venues", "venue_pairs",
            "markets_read", "proposals", "created", "updated",
            "skipped_reviewed", "conflict"}`. `conflict` is `True` when
            the commit lost a race with another writer and the pass was
            rolled back whole.
    """
    adapters = read_adapters()

    listings: dict[VenueId, list[VenueMarket]] = {}
    for venue in sorted(adapters):
        markets = await _open_markets(venue, adapters[venue])
        if markets is not None:
            listings[venue] = markets

    pairs = list(combinations(sorted(listings), 2))
    proposals: list[EventLink] = []
    for venue_a, venue_b in pairs:
        proposals.extend(propose_links(listings[venue_a], listings[venue_b]))

    summary: dict[str, Any] = {
        "mode": settings.trading_mode,
        "scanned_venues": sorted(adapters),
        "venue_pairs": [f"{a}+{b}" for a, b in pairs],
        "markets_read": {venue: len(rows) for venue, rows in listings.items()},
        "proposals": len(proposals),
        "created": 0,
        "updated": 0,
        "skipped_reviewed": 0,
        "conflict": False,
    }

    async with async_session_factory() as session:
        try:
            outcome = await persist_proposals(session, proposals)
        except IntegrityError:
            # Another writer (a human on `POST /links/propose`, or an
            # overlapping run) inserted one of these pairs between this
            # pass's lookup and its commit. Nothing is lost by deferring:
            # the matcher is deterministic, so the next interval refiles
            # exactly the same proposals against a table that now has
            # the other writer's row in it.
            await session.rollback()
            summary["conflict"] = True
            logger.warning(
                "link_proposal",
                extra={
                    "event": "link_proposal_conflict",
                    "proposals": len(proposals),
                },
            )
            return summary

        summary["created"] = outcome.created
        summary["updated"] = outcome.updated
        summary["skipped_reviewed"] = outcome.skipped_reviewed

    # Explicit keys, not `**summary`: `created` is a reserved
    # `logging.LogRecord` attribute (the record's own timestamp) and
    # `extra` refuses to shadow one, so splatting the summary would turn
    # every completed pass into a `KeyError` inside the logger.
    logger.info(
        "link_proposal",
        extra={
            "event": "link_proposal_complete",
            "mode": summary["mode"],
            "venue_pairs": summary["venue_pairs"],
            "markets_read": summary["markets_read"],
            "proposals": summary["proposals"],
            "links_created": summary["created"],
            "links_updated": summary["updated"],
            "links_skipped_reviewed": summary["skipped_reviewed"],
        },
    )
    return summary


@celery_app.task(name="app.tasks.matching.propose_event_links")
def propose_event_links() -> dict[str, Any]:
    """Celery entry point for `run_link_proposal()`.

    Returns:
        dict[str, Any]: The pass summary.
    """
    return run_async_task(run_link_proposal())
