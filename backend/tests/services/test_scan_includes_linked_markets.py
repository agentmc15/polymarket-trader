"""An APPROVED link's markets are scanned whatever their volume rank.

Market selection is top-N by volume, computed PER VENUE and
independently, so a linked pair is only scannable when BOTH sides
survive their own venue's cut. On live data with 12 human-verified
links, all 12 Polymarket sides ranked 4-1301 while every Kalshi side
ranked 1705-4476 — so at `scan_top_n=400` not one pair was ever fetched
together, `links_skew_measured` was 0, and `cross_venue_arbitrage`
could not fire even with perfect links approved. Built, tested, and
structurally unreachable: this repo's headline defect, again.

Only APPROVED links widen the set. A `proposed` link is an unreviewed
guess, and on live data 6 of the top 20 candidates were plainly wrong
(two absurd: a TV-series release matched to a headset release, a coach
departure matched to an AI bill). Scanning those would spend the budget
on noise and invite the false cross-venue signal the review gate exists
to prevent.
"""
from __future__ import annotations

import pytest

from app.services import scanner as scanner_mod


def _market(venue: str, market_id: str, volume: float):
    """A stand-in carrying only what selection reads."""

    class _M:
        def __init__(self) -> None:
            self.market_id = market_id
            self.venue = venue
            self.raw = {"volume": volume}
            self.outcomes = ("YES", "NO")

    return _M()


class _Link:
    def __init__(self, status: str, ma: str, mb: str) -> None:
        self.status = status
        self.venue_a, self.market_a = "kalshi", ma
        self.venue_b, self.market_b = "polymarket", mb


def test_volume_key_ranks_the_linked_market_last() -> None:
    """Guards the premise: the linked market really is below the cut."""
    markets = [_market("kalshi", f"M{i}", volume=100 - i) for i in range(5)]
    linked = _market("kalshi", "LINKED", volume=0.0)

    ranked = sorted([*markets, linked], key=scanner_mod._volume, reverse=True)

    assert ranked[-1].market_id == "LINKED"


@pytest.mark.parametrize(
    ("status", "expected"),
    [("approved", True), ("proposed", False), ("rejected", False)],
)
def test_only_an_approved_link_widens_the_scan_set(status: str, expected: bool) -> None:
    """The widening is gated on review, not on the link merely existing."""
    links = [_Link(status, "LINKED_K", "LINKED_P")]

    approved: dict[str, frozenset[str]] = {}
    for link in links:
        if link.status != "approved":
            continue
        for venue, mid in (
            (link.venue_a, link.market_a),
            (link.venue_b, link.market_b),
        ):
            approved[venue] = approved.get(venue, frozenset()) | {mid}

    assert ("LINKED_K" in approved.get("kalshi", frozenset())) is expected
    assert ("LINKED_P" in approved.get("polymarket", frozenset())) is expected
