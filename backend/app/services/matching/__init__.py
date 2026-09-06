"""Cross-venue event equivalence (PLAN.md D9).

The subsystem that answers "is Kalshi's contract the SAME BET as
Polymarket's?" — deterministically, with its reasoning recorded, and
without ever deciding the answer on its own. See
`app.services.matching.matcher` for the score and the one veto, and
`app.models.event_link` for what gets persisted and who may write it.

There is no model and no network anywhere in here (PLAN.md §2: event
matching has no LLM in the loop; an assist hook is a later kit's
problem). Everything is a pure function of the two `VenueMarket` values
handed in.

Example:
    ```python
    from app.services.matching import propose_links, score_pair

    evidence = score_pair(polymarket_market, kalshi_market)
    print(evidence.confidence, evidence.as_dict()["distinct_tokens"])

    # Unsaved rows for a human to review via `GET /links`.
    links = propose_links(polymarket_markets, kalshi_markets)
    ```
"""
from app.services.matching.matcher import (
    CLOSE_TOLERANCE_HOURS,
    THRESHOLD_MISMATCH_CAP,
    UNKNOWN_SCORE,
    WEIGHTS,
    LinkEvidence,
    candidate_pairs,
    compare_sources,
    compare_thresholds,
    derive_outcome_map,
    jaccard,
    propose_links,
    score_pair,
    tri_state_score,
)
from app.services.matching.normalize import (
    DATE_TOKEN,
    NUM_TOKEN,
    PLACEHOLDER_TOKENS,
    STOPWORDS,
    NormalizedTitle,
    content_tokens,
    extract_thresholds,
    normalize,
    normalize_title,
    stem,
)

__all__ = [
    # normalize
    "DATE_TOKEN",
    "NUM_TOKEN",
    "PLACEHOLDER_TOKENS",
    "STOPWORDS",
    "NormalizedTitle",
    "content_tokens",
    "extract_thresholds",
    "normalize",
    "normalize_title",
    "stem",
    # matcher
    "CLOSE_TOLERANCE_HOURS",
    "THRESHOLD_MISMATCH_CAP",
    "UNKNOWN_SCORE",
    "WEIGHTS",
    "LinkEvidence",
    "candidate_pairs",
    "compare_sources",
    "compare_thresholds",
    "derive_outcome_map",
    "jaccard",
    "propose_links",
    "score_pair",
    "tri_state_score",
]
