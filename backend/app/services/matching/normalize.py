"""Deterministic title normalization for cross-venue event matching (D9).

Turns a market question into a stable bag of stemmed content tokens plus
the numeric thresholds it states, so two venues' wordings of the same
event line up and two venues' wordings of DIFFERENT events do not.

Everything here is deterministic and self-contained — no model, no
network, no downloaded corpus (GUARDRAILS.md §1.4, PLAN.md §2: event
matching has no LLM in the loop; it is a deterministic proposal a human
reviews).

STEMMER CHOICE. This module vendors a compact Porter-style stemmer
(`stem`) rather than importing `nltk.stem.PorterStemmer`, even though
`nltk` happens to be installed on this machine, for three reasons:

1. `nltk` is NOT in `backend/requirements.txt`. Importing it from
   `app/` would make the application depend on a package the deployment
   never installs (GUARDRAILS.md §2: missing deps come from
   `requirements.txt`).
2. `nltk`'s English stopword list is a DOWNLOADED corpus, not code, so
   the stopword half of this file has to be self-contained regardless.
   Vendoring one half and importing the other is worse than owning both.
3. Every `title_jaccard` this module feeds is persisted in
   `event_links.evidence` and read back by a human reviewer. A vendored
   stemmer is inspectable and version-stable; a dependency upgrade that
   silently changed one stem would silently change every stored score.

TWO DELIBERATE DEPARTURES from a stock English stopword list, both in
the safe direction (they make distinct events look LESS alike, never
more):

* NEGATIONS ARE KEPT. "no", "not", "never", "without", "neither" are
  stopwords in the usual lists. Dropping them makes "Will X happen?" and
  "Will X NOT happen?" normalize identically — the two sides of the same
  contract scored as the same event. They are kept as content tokens.
* COMPARISON/DIRECTION WORDS ARE KEPT. "above", "below", "over",
  "under", "more", "less" are stopwords in the usual lists too. Dropping
  them makes "above 100k" and "below 100k" identical. They are kept.

`will/the/by/before/after/on/in` ARE removed, per PLAN.md D9.
"""
import re
from dataclasses import dataclass
from functools import lru_cache

#: Placeholder standing in for any date, month name, or year.
DATE_TOKEN = "<date>"

#: Placeholder standing in for any numeric literal. The literal's VALUE
#: is not thrown away — it is extracted into `NormalizedTitle.thresholds`
#: and compared separately by `app.services.matching.matcher`, because
#: "above 100k" and "above 150k" are the same sentence and different
#: events.
NUM_TOKEN = "<num>"

#: Tokens that carry no subject matter. The blocking index in
#: `matcher.candidate_pairs` skips these: every market with a date in its
#: title shares `<date>`, so indexing on it would put every market in one
#: bucket and buy nothing.
PLACEHOLDER_TOKENS = frozenset({DATE_TOKEN, NUM_TOKEN})

#: Removed by name in PLAN.md D9.
_D9_STOPWORDS = frozenset({"will", "the", "by", "before", "after", "on", "in"})

#: General English function words. Derived from the usual English
#: stopword list MINUS the negations and comparison words named in this
#: module's docstring — dropping those merges opposite events.
_GENERAL_STOPWORDS = frozenset(
    {
        "a", "about", "again", "all", "am", "an", "and", "any", "are", "as",
        "at", "be", "because", "been", "being", "between", "both", "but",
        "can", "could", "did", "do", "does", "doing", "done", "during",
        "each", "for", "from", "further", "had", "has", "have", "having",
        "he", "her", "here", "hers", "herself", "him", "himself", "his",
        "how", "i", "if", "into", "is", "it", "its", "itself", "just", "me",
        "my", "myself", "of", "once", "only", "or", "other", "ought", "our",
        "ours", "ourselves", "out", "own", "same", "she", "should", "so",
        "some", "such", "than", "that", "their", "theirs", "them",
        "themselves", "then", "there", "these", "they", "this", "those",
        "through", "to", "too", "until", "us", "very", "was", "we", "were",
        "what", "when", "where", "which", "while", "who", "whom", "why",
        "with", "would", "you", "your", "yours", "yourself", "yourselves",
    }
)

#: The full removal set applied before stemming.
STOPWORDS = _D9_STOPWORDS | _GENERAL_STOPWORDS

#: Magnitude suffixes recognized on a numeric literal. `%`/`percent` map
#: to 1.0 (so "above 5%" yields 5.0, matching "above 5 percent"); the
#: convention only has to be CONSISTENT, since thresholds are compared
#: against each other and never against a price.
_MAGNITUDES: dict[str, float] = {
    "k": 1e3,
    "thousand": 1e3,
    "m": 1e6,
    "mm": 1e6,
    "million": 1e6,
    "b": 1e9,
    "bn": 1e9,
    "billion": 1e9,
    "t": 1e12,
    "trillion": 1e12,
    "%": 1.0,
    "percent": 1.0,
    "pct": 1.0,
}

#: A numeric literal, optionally signed by a currency symbol and/or
#: followed by a magnitude suffix. Alternation order matters: the
#: comma-grouped form is tried first so `100,000` parses as one number
#: rather than as `100`.
_NUMBER_RE = re.compile(
    r"(?P<currency>[$€£])?\s*"
    r"(?P<value>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s*(?P<suffix>mm|bn|thousand|million|billion|trillion|percent|pct|[kmbt%])?\b",
    re.IGNORECASE,
)

_MONTH = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t)?(?:ember)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?)"
)
# "may" is excluded from the BARE month pattern only: as a standalone word
# it is far more often the modal verb ("may resolve") than the month.
_BARE_MONTH = (
    r"(?:january|jan|february|feb|march|mar|april|apr|june|jun|july|jul|"
    r"august|aug|september|sept|sep|october|oct|november|nov|december|dec)"
)
_ORD = r"(?:st|nd|rd|th)"

#: Date shapes, longest-first, all collapsed to `DATE_TOKEN`.
_DATE_RES = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b\d{4}-\d{1,2}-\d{1,2}\b",
        r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b",
        rf"\b{_MONTH}\.?\s+\d{{1,2}}{_ORD}?(?:\s*,?\s*\d{{4}})?(?!\d)",
        rf"\b\d{{1,2}}{_ORD}?\s+{_MONTH}\.?(?:\s*,?\s*\d{{4}})?(?!\d)",
        rf"\b{_BARE_MONTH}\b",
        r"\b(?:19|20)\d{2}\b",
        r"\bq[1-4]\b",
        rf"\b\d{{1,2}}{_ORD}\b",
    )
)

#: Bare integers left after the marked-number and date passes.
_BARE_NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?\b")

#: Words and the two placeholders; everything else (punctuation, stray
#: comparison glyphs like `>=` and `≥`) is dropped by not matching.
_TOKEN_RE = re.compile(r"<date>|<num>|[a-z0-9]+")

_VOWELS = frozenset("aeiou")


@dataclass(frozen=True)
class NormalizedTitle:
    """One market question reduced to comparable parts.

    Immutable (both fields are tuples) because instances are memoized by
    `normalize` — a caller that mutated a returned list would corrupt
    every later normalization of the same string.

    Attributes:
        tokens: Stemmed content tokens in source order, with dates and
            numbers replaced by `DATE_TOKEN`/`NUM_TOKEN`. Duplicates are
            kept; scoring compares SETS.
        thresholds: Every numeric literal found in the title, in source
            order, scaled by its magnitude suffix (`100k` -> `100000.0`).
            Years and dates are NOT here — they became `DATE_TOKEN`
            before this pass ran.
    """

    tokens: tuple[str, ...]
    thresholds: tuple[float, ...]


def _measure(stem_text: str) -> int:
    """Count vowel-consonant sequences in `stem_text` (Porter's `m`).

    Args:
        stem_text: A candidate stem.

    Returns:
        int: The number of VC transitions, Porter's measure of how much
            word is left. Suffix rules are guarded on it so short words
            are not stripped down to nothing.
    """
    form = "".join("v" if ch in _VOWELS else "c" for ch in stem_text)
    form = re.sub(r"c*", "", form, count=1)
    return form.count("vc")


def _has_vowel(stem_text: str) -> bool:
    """Return whether `stem_text` contains any vowel."""
    return any(ch in _VOWELS for ch in stem_text)


def _ends_double_consonant(stem_text: str) -> bool:
    """Return whether `stem_text` ends in a doubled consonant."""
    return (
        len(stem_text) >= 2
        and stem_text[-1] == stem_text[-2]
        and stem_text[-1] not in _VOWELS
    )


def _is_cvc(stem_text: str) -> bool:
    """Return whether `stem_text` ends consonant-vowel-consonant.

    Porter's `*o` condition, with the trailing consonant not in `wxy`.
    It is what turns `trad` (from "trading") back into `trade` so it
    matches the stem of "trade".
    """
    if len(stem_text) < 3:
        return False
    a, b, c = stem_text[-3], stem_text[-2], stem_text[-1]
    return a not in _VOWELS and b in _VOWELS and c not in _VOWELS and c not in "wxy"


#: Porter step-2/4 suffixes, applied at most once and only when enough
#: word remains (`_measure > 1`). Deliberately a short list: an
#: over-eager step 4 merges words that name different events, and this
#: matcher's failure mode is a wrong equivalence, not a missed one.
_LATE_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("ational", "ate"),
    ("ization", "ize"),
    ("fulness", "ful"),
    ("iveness", "ive"),
    ("tional", "tion"),
    ("ement", ""),
    ("ance", ""),
    ("ence", ""),
    ("ation", "ate"),
    ("ment", ""),
    ("ness", ""),
    ("ible", ""),
    ("able", ""),
    ("ous", ""),
    ("ive", ""),
    ("ize", ""),
    ("iti", ""),
    ("ism", ""),
    ("ant", ""),
    ("ent", ""),
    ("al", ""),
    ("ic", ""),
)


def stem(token: str) -> str:
    """Reduce one token to a Porter-style stem.

    Implements Porter steps 1a (plurals), 1b (`-eed`/`-ed`/`-ing` with
    the `at`/`bl`/`iz`, doubled-consonant and CVC fix-ups), 1c (`y` ->
    `i`), and a conservative subset of steps 2/4 (`_LATE_SUFFIXES`).
    See this module's docstring for why it is vendored rather than
    imported.

    Args:
        token: A lowercase word token. Placeholders (`<date>`/`<num>`)
            must not be passed — they are not words.

    Returns:
        str: The stem. Tokens of 3 characters or fewer are returned
            unchanged; stripping them wins nothing and loses meaning
            ("gas" -> "ga").
    """
    if len(token) <= 3 or not token.isalpha():
        return token

    word = token
    # -- step 1a: plurals -------------------------------------------
    # `sses` -> `ss` and `ies` -> `i` both drop two characters; a bare
    # `ss` is left alone ("pass" is not a plural).
    if word.endswith(("sses", "ies")):
        word = word[:-2]
    elif word.endswith("s") and not word.endswith("ss"):
        word = word[:-1]

    # -- step 1b: past tense / gerund --------------------------------
    fixup = False
    if word.endswith("eed"):
        if _measure(word[:-3]) > 0:
            word = word[:-1]
    elif word.endswith("ed") and _has_vowel(word[:-2]):
        word = word[:-2]
        fixup = True
    elif word.endswith("ing") and _has_vowel(word[:-3]):
        word = word[:-3]
        fixup = True
    if fixup:
        if word.endswith(("at", "bl", "iz")):
            word += "e"
        elif _ends_double_consonant(word) and not word.endswith(("l", "s", "z")):
            word = word[:-1]
        elif _measure(word) == 1 and _is_cvc(word):
            word += "e"

    # -- step 1c: terminal y -----------------------------------------
    if word.endswith("y") and _has_vowel(word[:-1]):
        word = word[:-1] + "i"

    # -- steps 2/4: a conservative suffix table ----------------------
    for suffix, replacement in _LATE_SUFFIXES:
        if word.endswith(suffix) and _measure(word[: -len(suffix)]) > 1:
            word = word[: -len(suffix)] + replacement
            break

    # -- step 5: terminal e, doubled l -------------------------------
    # Keeps "close"/"closing" and "trade"/"trading" together while still
    # folding "deadline" -> "deadlin" and "controlled" -> "control".
    if word.endswith("e"):
        trimmed = word[:-1]
        measure = _measure(trimmed)
        if measure > 1 or (measure == 1 and not _is_cvc(trimmed)):
            word = trimmed
    if _measure(word) > 1 and word.endswith("ll"):
        word = word[:-1]

    return word or token


def _parse_number(value: str, suffix: str | None) -> float | None:
    """Convert one matched numeric literal to a float.

    Args:
        value: The digits, possibly comma-grouped ("100,000") and/or
            decimal ("1.5").
        suffix: A magnitude suffix ("k", "m", "%", ...) or `None`.

    Returns:
        float | None: The scaled value, or `None` if it does not parse
            (which is treated as "no threshold stated", never as a
            disagreement).
    """
    try:
        magnitude = float(value.replace(",", ""))
    except ValueError:
        return None
    if suffix:
        magnitude *= _MAGNITUDES.get(suffix.lower(), 1.0)
    return magnitude


def _replace_marked_numbers(text: str, found: list[float]) -> str:
    """Collapse numbers that CANNOT be a date, recording their values.

    A literal qualifies only if it carries a marker no date shape has: a
    currency symbol, a magnitude suffix, comma grouping, or a decimal
    point. Running this BEFORE the date pass is what stops `$2,000` from
    being read as the year 2000, and requiring a marker is what stops it
    from eating `12/31/2025`.

    Args:
        text: Lowercased title text.
        found: Accumulator appended to in source order.

    Returns:
        str: `text` with qualifying literals replaced by `NUM_TOKEN`.
    """

    def _sub(match: re.Match[str]) -> str:
        value = match.group("value")
        suffix = match.group("suffix")
        marked = bool(match.group("currency") or suffix or "," in value or "." in value)
        if not marked:
            return match.group(0)
        parsed = _parse_number(value, suffix)
        if parsed is None:
            return match.group(0)
        found.append(parsed)
        return f" {NUM_TOKEN} "

    return _NUMBER_RE.sub(_sub, text)


def _replace_bare_numbers(text: str, found: list[float]) -> str:
    """Collapse the plain integers left after the date pass.

    Args:
        text: Title text with dates already collapsed.
        found: Accumulator appended to in source order.

    Returns:
        str: `text` with bare numbers replaced by `NUM_TOKEN`.
    """

    def _sub(match: re.Match[str]) -> str:
        parsed = _parse_number(match.group(0), None)
        if parsed is None:
            return match.group(0)
        found.append(parsed)
        return f" {NUM_TOKEN} "

    return _BARE_NUMBER_RE.sub(_sub, text)


def _normalize_impl(title: str) -> NormalizedTitle:
    """Run the full pipeline once. Memoized behind `normalize`."""
    text = title.casefold()
    thresholds: list[float] = []
    text = _replace_marked_numbers(text, thresholds)
    for date_re in _DATE_RES:
        text = date_re.sub(f" {DATE_TOKEN} ", text)
    text = _replace_bare_numbers(text, thresholds)

    tokens: list[str] = []
    for raw in _TOKEN_RE.findall(text):
        if raw in PLACEHOLDER_TOKENS:
            tokens.append(raw)
            continue
        # Length-1 leftovers are possessive/initial noise ("trump's" ->
        # "trump", "s"), never subject matter.
        if len(raw) < 2 or raw in STOPWORDS:
            continue
        tokens.append(stem(raw))
    return NormalizedTitle(tokens=tuple(tokens), thresholds=tuple(thresholds))


@lru_cache(maxsize=8192)
def _normalize_cached(title: str) -> NormalizedTitle:
    """Memoize `_normalize_impl`.

    `propose_links` normalizes each market once for the blocking index
    and again for every candidate pair it scores; the cache makes the
    second and later calls free. Safe to share because `NormalizedTitle`
    is immutable.
    """
    return _normalize_impl(title)


def normalize(title: str) -> NormalizedTitle:
    """Normalize one market question into tokens and thresholds.

    Args:
        title: Raw venue question text. UNTRUSTED input (GUARDRAILS.md
            §6): it is tokenized and scored, never executed or
            interpreted as an instruction.

    Returns:
        NormalizedTitle: Stemmed content tokens plus the numeric
            thresholds stated in the title.
    """
    return _normalize_cached(title)


def normalize_title(title: str) -> list[str]:
    """Return the stemmed content tokens of one market question.

    The PLAN.md D9 entry point. Lowercases, strips punctuation, drops
    stopwords (see this module's docstring for the two words classes it
    deliberately KEEPS), collapses month names / dates / years to
    `DATE_TOKEN` and numeric literals to `NUM_TOKEN`, and stems the rest.

    Args:
        title: Raw venue question text.

    Returns:
        list[str]: Tokens in source order, duplicates kept.
    """
    return list(normalize(title).tokens)


def extract_thresholds(title: str) -> list[float]:
    """Return the numeric thresholds stated in one market question.

    Handles `100k`, `100,000`, `$100K`, `1.5m`, `5%`, and plain integers,
    scaling each by its magnitude suffix. Years and dates are excluded —
    they were collapsed to `DATE_TOKEN` before this pass.

    Args:
        title: Raw venue question text.

    Returns:
        list[float]: Thresholds in source order.
    """
    return list(normalize(title).thresholds)


def content_tokens(title: str) -> frozenset[str]:
    """Return the distinct non-placeholder tokens of a question.

    This is the blocking key set used by
    `app.services.matching.matcher.candidate_pairs`: `DATE_TOKEN` and
    `NUM_TOKEN` are excluded because nearly every market carries them, so
    indexing on them would put the whole corpus in one bucket.

    Args:
        title: Raw venue question text.

    Returns:
        frozenset[str]: Distinct content tokens, possibly empty (a title
            of nothing but a date and a number has no subject).
    """
    return frozenset(normalize(title).tokens) - PLACEHOLDER_TOKENS
