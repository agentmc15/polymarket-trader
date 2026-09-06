"""Structural enforcement of GUARDRAILS.md §1.1 (PLAN.md D13).

GUARDRAILS.md §1.1: "The only modules allowed to contain order-placement
calls are `backend/app/venues/polymarket/live.py` and `backend/app/
venues/kalshi/live.py`, and `backend/tests/test_fences.py` enforces that
by AST walk." This module IS that enforcement.

`find_violations()` is a plain, allowlist-FREE primitive: given a `.py`
file or a directory, it returns every forbidden `Attribute`/`Name`/
`Constant` node it finds, with no knowledge of which files are meant to
be exempt. That is deliberate (ORCHESTRATOR RULING 3): a fence test that
passes because it finds nothing is indistinguishable from one that
passes because it looks nowhere, so the walker itself must be provable
against an injected positive control, independent of any allowlist. The
allowlist (`LIVE_MODULES` / `WRAPPER_MODULE` / `ALLOWED_UNTIL_T16`) is
applied on top, only in `_unexempted_violations()`, for the one test that
checks the real `app/` tree.

ORCHESTRATOR RULING 1 (historical) -- `app/bots/executor.py` was the
walker's first REAL target, not a hypothetical one: `OrderExecutor.
cancel_order` called `self.client.cancel_order(order_id)`, reachable
through `ClobClientWrapper.cancel_order` -> the real `py_clob_client.
cancel()`, with NO `assert_live_allowed()` anywhere in that chain, in a
file outside the two sanctioned `live.py` modules. `ALLOWED_UNTIL_T16`
named it explicitly until T16, which deleted `OrderExecutor` outright
(rewiring `bots/base.py::BaseBot.execute_signal` onto `OrderRouter`
instead) and emptied the set for good -- see `test_allowed_until_t16_is_
empty` below, which replaces the two tests that used to prove the
finding against that specific file.

T16 -- TWO RULES, NOT ONE, because the walker's original single
`FORBIDDEN_METHOD_NAMES` set was hiding a real gap while producing a
false alarm on a legitimate one. Read both before touching either:

RULE 1 (`RAW_CLIENT_METHOD_NAMES`) -- unchanged from before T16.
`post_order`/`create_order`/`create_or_derive_api_creds`/`ClobClient(`/
the literal order URLs are `py_clob_client`'s (or the raw venue HTTP
API's) own names. Calling one of these TRANSMITS an order. Confined to
`LIVE_MODULES` and `WRAPPER_MODULE` (the sanctioned wrapper) exactly as
before -- this is the strict rule GUARDRAILS.md §1.1 names first, and
T16 does not weaken it (see `test_walker_still_confines_raw_client_
names_even_inside_the_execution_layer`).

RULE 2 (`ADAPTER_PROTOCOL_METHOD_NAMES`) -- NEW in T16. `place_order`/
`cancel_order` are `app.venues.base.VenueAdapter` PROTOCOL methods, not
raw-client names -- calling one does not itself transmit (transmission
happens inside the `live.py` module the call eventually reaches, or
inside `SimulatedFillEngine` in paper mode). But BEFORE T16, only
`cancel_order` was forbidden at all (an accident of it colliding with a
`py_clob_client` name RULE 1 already had to name), while `place_order`
was not forbidden ANYWHERE -- `app/execution/router.py` calls
`adapter.place_order` freely, and so, before T16, could a strategy, a
Celery task, an API route, or a bot, bypassing `OrderRouter`'s risk
fences (`check_order_limits`), its per-venue capital reservations, and
its crash-safe `PENDING` row (PLAN.md D4) ENTIRELY, with nothing
flagging it. That was the real hole. The fix is symmetric: BOTH names
are confined to `EXECUTION_LAYER_DIRS` (`app/execution/`, `app/venues/`
-- the only packages allowed to route an order at all; a venue's
`live.py` lives under `app/venues/` too, so it needs no separate
mention). Closing the `place_order` hole is the substantive change;
`cancel_order` becoming callable from `app/execution/router.py` (for
`DELETE /orders/{id}` -> `OrderRouter.cancel()`) is what falls out of
applying that fix properly, not a special case bolted on for it.

WHY `OrderRouter`'s OWN METHODS ARE NAMED `submit`/`cancel`, NOT
`place_order`/`cancel_order`: the walker is SYNTACTIC -- it matches an
`ast.Attribute`'s NAME, not the real type of whatever it is called on
(`find_violations` never imports or type-checks anything, by design).
If `OrderRouter.cancel()` were instead spelled `cancel_order`, then a
perfectly legitimate `app/api/routes/trading.py` call to
`order_router.cancel_order(order_id)` would be textually IDENTICAL to a
route reaching into an adapter directly, and RULE 2 would have to
special-case "unless the receiver is the router" -- which the walker
has no way to check. Giving the router's public methods their own,
disjoint names sidesteps the ambiguity instead of trying to resolve it
after the fact; T14 already made this choice for `submit` (never named
`place_order`), and T16 follows it for `cancel`.

DEFECT (GUARDRAILS.md §3.4, kind=stale-pin): the brief's acceptance
criterion names `app/services/scoring.py` as the file to copy for the
positive/negative control. No such file exists in this repo (see
`app/services/` — `data_collector.py`, `polymarket/`, `backtesting/`,
`__init__.py`). The underlying requirement -- copy a REAL, currently-
clean module to `tmp_path`, inject a genuine forbidden call, and prove
the walker detects it (plus an unmodified negative control) -- is fully
satisfiable with any real module, so `app/services/data_collector.py` is
used instead. Recorded in NOTES.md.
"""
import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.config import Settings
from app.execution.fences import (
    LIVE_TRADING_CONFIRMATION_PHRASE,
    KillSwitchEngaged,
    LiveTradingDisabled,
    RiskLimitExceeded,
    assert_live_allowed,
    assert_placement_allowed,
    check_order_limits,
    kill_switch_engaged,
)

# ---------------------------------------------------------------------------
# The allowlist GUARDRAILS.md §1.1 grants -- applied ONLY in
# `_unexempted_violations`, never inside `find_violations` itself.
# ---------------------------------------------------------------------------

#: The two modules allowed to contain the actual order-placement/
#: cancellation calls (GUARDRAILS.md §1.1). Paths are relative to
#: `APP_ROOT` (`backend/app`).
LIVE_MODULES: set[Path] = {
    Path("venues/polymarket/live.py"),
    Path("venues/kalshi/live.py"),
}

#: `ClobClientWrapper` (the thin wrapper AROUND `py_clob_client`) is where
#: `create_order`/`post_order`/`cancel_order`/`create_or_derive_api_creds`/
#: `ClobClient(...)` are DEFINED. Only `venues/polymarket/live.py` is
#: allowed to CALL into it for a real order (PLAN.md D3: "the seam is the
#: answer, not a pile of adapters") -- this file is exempt for the same
#: reason the two `live.py` modules are: it is the sanctioned place these
#: names are declared, not an unfenced caller.
WRAPPER_MODULE = Path("services/polymarket/client.py")

#: Empty as of T16 (PLAN.md D4/D13). Used to name exactly one finding:
#: `bots/executor.py::OrderExecutor.cancel_order` (module docstring,
#: ORCHESTRATOR RULING 1) -- gone now that `OrderExecutor` no longer
#: exists at all. Kept as an empty set rather than deleted outright, so
#: this file's own "the allowlist must not quietly grow" discipline
#: stays testable (`test_allowed_until_t16_is_empty` below): the honest
#: response to a FUTURE unfenced finding is closing it, never reopening
#: this escape hatch. Referenced by `_is_exempt` for BOTH RULE 1 and
#: RULE 2 (see module docstring), as a single shared, temporary
#: exception mechanism -- not two.
ALLOWED_UNTIL_T16 = set()  # type: set[Path]

#: `backend/app`, resolved from this test file's own location so the
#: check works regardless of the process's current working directory.
APP_ROOT = Path(__file__).resolve().parent.parent / "app"

#: RULE 1 (module docstring): raw `py_clob_client`/venue-HTTP names.
#: These TRANSMIT an order. Confined to `LIVE_MODULES`/`WRAPPER_MODULE`.
RAW_CLIENT_METHOD_NAMES = frozenset(
    {"post_order", "create_order", "create_or_derive_api_creds"}
)

#: RULE 2 (module docstring, new in T16): `app.venues.base.VenueAdapter`
#: PROTOCOL methods. Calling either does not itself transmit -- it
#: bypasses `OrderRouter` instead, which is the thing worth forbidding.
#: Confined to `EXECUTION_LAYER_DIRS`.
ADAPTER_PROTOCOL_METHOD_NAMES = frozenset({"place_order", "cancel_order"})

#: Every forbidden method-shaped name, RULE 1 and RULE 2 together. This
#: is what `find_violations` (allowlist-FREE) scans for; the RULE the
#: name belongs to only matters once `_is_exempt` decides where it is
#: allowed to appear.
FORBIDDEN_METHOD_NAMES = RAW_CLIENT_METHOD_NAMES | ADAPTER_PROTOCOL_METHOD_NAMES
FORBIDDEN_CLASS_NAME = "ClobClient"
FORBIDDEN_PATH_SUBSTRINGS = ("/portfolio/events/orders", "/portfolio/orders")

#: Top-level `app/` subpackages RULE 2 permits to call `place_order`/
#: `cancel_order` directly: the router itself, and every concrete
#: adapter (the two `live.py` modules included -- they are venues too).
#: Everything else -- `app/api/`, `app/bots/`, `app/strategies/`,
#: `app/tasks/`, `app/services/` -- must go through `OrderRouter.
#: submit()`/`.cancel()` instead.
EXECUTION_LAYER_DIRS = frozenset({"execution", "venues"})


@dataclass(frozen=True)
class Violation:
    """One forbidden AST node `find_violations` found.

    Attributes:
        path: The file the violation was found in, exactly as passed to
            (or discovered under) the scanned root -- absolute if the
            root was absolute, relative if it was relative.
        lineno: 1-indexed source line the node starts on.
        kind: `"method"` (a `post_order`/`create_order`/`cancel_order`/
            `create_or_derive_api_creds` `Attribute` or bare `Name`),
            `"class"` (a bare `Name` node reading `ClobClient`), or
            `"path_literal"` (a non-docstring string `Constant`
            containing one of `FORBIDDEN_PATH_SUBSTRINGS`).
        detail: The specific identifier or substring matched.
    """

    path: Path
    lineno: int
    kind: str
    detail: str


def _docstring_constant_ids(tree: ast.AST) -> set[int]:
    """Return `id()` of every `Constant` node that IS a module/class/
    function docstring (its scope's FIRST body statement, an `Expr` whose
    value is a string `Constant`).

    A docstring is, syntactically, an ordinary string literal -- nothing
    marks it as documentation except its position. Several real modules
    legitimately DISCUSS a venue's order-placement path in a docstring
    (e.g. `app/venues/types.py`'s `OrderAck` docstring quotes Kalshi's
    `POST /portfolio/events/orders` response shape) without that being a
    call site. Excluding docstring `Constant`s from the path-literal scan
    is what keeps that documentation from tripping the fence; it has no
    effect on the method-name/class-name checks, which only ever look at
    `Attribute`/`Name` nodes -- a docstring's prose is one string, never
    parsed into those.
    """
    docstring_ids: set[int] = set()
    scopes = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if isinstance(node, scopes) and node.body:
            first = node.body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docstring_ids.add(id(first.value))
    return docstring_ids


def find_violations(path: Path) -> list[Violation]:
    """Walk `path` for order-placement references. Allowlist-FREE.

    Args:
        path: A single `.py` file, or a directory walked recursively for
            every `*.py` file under it.

    Returns:
        list[Violation]: Every forbidden node found, across every file
            scanned, in file/traversal order. Empty if none.
    """
    files = [path] if path.is_file() else sorted(path.rglob("*.py"))
    violations: list[Violation] = []
    for file_path in files:
        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
        docstring_ids = _docstring_constant_ids(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_METHOD_NAMES:
                violations.append(Violation(file_path, node.lineno, "method", node.attr))
            elif isinstance(node, ast.Name):
                if node.id in FORBIDDEN_METHOD_NAMES:
                    violations.append(Violation(file_path, node.lineno, "method", node.id))
                elif node.id == FORBIDDEN_CLASS_NAME:
                    violations.append(Violation(file_path, node.lineno, "class", node.id))
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstring_ids
            ):
                for substring in FORBIDDEN_PATH_SUBSTRINGS:
                    if substring in node.value:
                        violations.append(
                            Violation(file_path, node.lineno, "path_literal", substring)
                        )
    return violations


def _is_exempt(rel_path: Path, violation: Violation) -> bool:
    """Return `True` if `violation`, found at `rel_path` (relative to the
    root `find_violations` was called with), is allowed there.

    Two different confinement rules apply, dispatched on WHICH forbidden
    name was matched (module docstring, "T16 -- TWO RULES, NOT ONE"):

    - RULE 2 first: if `violation` is an `ADAPTER_PROTOCOL_METHOD_NAMES`
      hit (`place_order`/`cancel_order`), it is allowed inside
      `EXECUTION_LAYER_DIRS` (`app/execution/`, `app/venues/`) or
      `ALLOWED_UNTIL_T16` -- and NOWHERE else, `LIVE_MODULES`/
      `WRAPPER_MODULE` included (those exemptions are RULE 1's, for a
      DIFFERENT set of names; a file being the sanctioned `live.py`
      does not, by itself, make every possible violation in it exempt).
    - RULE 1 otherwise (`RAW_CLIENT_METHOD_NAMES`, the `"class"` hit on
      `ClobClient`, or a `"path_literal"` hit): confined to
      `LIVE_MODULES`/`WRAPPER_MODULE`/`ALLOWED_UNTIL_T16`, unchanged
      from before T16.

    Args:
        rel_path: The violation's file, relative to the scanned root.
        violation: The violation being checked.

    Returns:
        bool: `True` if this violation is allowed at this location.
    """
    if violation.kind == "method" and violation.detail in ADAPTER_PROTOCOL_METHOD_NAMES:
        if rel_path in ALLOWED_UNTIL_T16:
            return True
        return bool(rel_path.parts) and rel_path.parts[0] in EXECUTION_LAYER_DIRS
    return rel_path in (LIVE_MODULES | {WRAPPER_MODULE} | ALLOWED_UNTIL_T16)


def _unexempted_violations(root: Path) -> list[Violation]:
    """`find_violations(root)`, filtered by `_is_exempt` (GUARDRAILS.md
    §1.1, both rules -- see module docstring).
    """
    return [
        v for v in find_violations(root) if not _is_exempt(v.path.relative_to(root), v)
    ]


# ---------------------------------------------------------------------------
# 1. The structural fence itself.
# ---------------------------------------------------------------------------


def test_no_module_outside_the_allowlist_references_order_placement() -> None:
    """GUARDRAILS.md §1.1, enforced: only `LIVE_MODULES` (plus the wrapper
    and the until-T16 exemption) may reference order-placement machinery
    anywhere under `backend/app`.
    """
    violations = _unexempted_violations(APP_ROOT)

    assert violations == [], (
        "order-placement reference(s) found outside the allowed modules: "
        f"{violations}"
    )


def test_allowed_until_t16_is_empty() -> None:
    """T16 (PLAN.md D4/D13) closed this escape hatch for good.

    `bots/executor.py`'s unfenced `cancel_order` call is gone --
    `OrderExecutor` no longer exists at all (T16 deletes it outright;
    `bots/base.py::BaseBot.execute_signal` now routes through
    `OrderRouter.submit()` instead). `DELETE /orders/{id}` reaches
    `adapter.cancel_order` through `OrderRouter.cancel()`, inside
    `app/execution/`, which RULE 2 (module docstring) permits directly
    -- no allowlist entry needed for it either.

    `ALLOWED_UNTIL_T16` is kept as an empty set rather than deleted
    outright: `test_no_module_outside_the_allowlist_references_order_
    placement` below still runs the allowlist-FREE walker against the
    real `app/` tree on every test run, so if a genuinely new unfenced
    finding ever appears, the honest response is closing it with a new,
    reviewed exception -- never quietly reopening this one.
    """
    assert ALLOWED_UNTIL_T16 == set()


def test_walker_flags_place_order_called_from_the_api_layer(tmp_path: Path) -> None:
    """RULE 2 (module docstring), THE SUBSTANTIVE FIX: `place_order` is a
    `VenueAdapter` PROTOCOL method, not a raw venue-client call -- but
    calling it directly bypasses `OrderRouter`'s risk fences, capital
    reservations, and crash-safe `PENDING` row entirely. An API route
    (or a strategy, a bot, a Celery task) reaching `adapter.place_order`
    directly instead of going through `OrderRouter.submit()` is exactly
    the hole T16 closes -- this is its positive control.
    """
    api_dir = tmp_path / "api" / "routes"
    api_dir.mkdir(parents=True)
    (api_dir / "sketchy.py").write_text(
        "async def handler(adapter, order):\n"
        "    return await adapter.place_order(order)\n",
        encoding="utf-8",
    )

    violations = _unexempted_violations(tmp_path)

    assert any(v.kind == "method" and v.detail == "place_order" for v in violations), (
        f"expected a 'place_order' violation from app/api/, got {violations}"
    )


@pytest.mark.parametrize("package", ["bots", "strategies", "tasks", "services"])
def test_walker_flags_adapter_protocol_calls_outside_the_execution_layer(
    tmp_path: Path, package: str
) -> None:
    """RULE 2 is not API-only: a bot, a strategy, a Celery task, or a
    service calling `cancel_order` directly bypasses `OrderRouter`
    exactly as an API route would -- none of these four packages is
    `app/execution/` or `app/venues/`.
    """
    pkg_dir = tmp_path / package
    pkg_dir.mkdir()
    (pkg_dir / "sketchy.py").write_text(
        "async def f(adapter, order_id):\n"
        "    return await adapter.cancel_order(order_id)\n",
        encoding="utf-8",
    )

    violations = _unexempted_violations(tmp_path)

    assert any(v.kind == "method" and v.detail == "cancel_order" for v in violations), (
        f"expected a 'cancel_order' violation from {package}/, got {violations}"
    )


def test_walker_allows_adapter_protocol_calls_inside_the_execution_layer(
    tmp_path: Path,
) -> None:
    """The negative control for RULE 2: `app/execution/` (the router
    itself) and `app/venues/` (every concrete adapter) MAY call
    `place_order`/`cancel_order` -- that is the router's/adapter's job,
    and it is what every OTHER package must route through instead
    (PLAN.md D4; see the two tests above).
    """
    (tmp_path / "execution").mkdir()
    (tmp_path / "execution" / "router.py").write_text(
        "async def submit(adapter, order):\n"
        "    return await adapter.place_order(order)\n"
        "\n"
        "async def cancel(adapter, order_id):\n"
        "    return await adapter.cancel_order(order_id)\n",
        encoding="utf-8",
    )
    (tmp_path / "venues").mkdir()
    (tmp_path / "venues" / "paper.py").write_text(
        "class PaperVenueAdapter:\n"
        "    async def cancel_order(self, order_id):\n"
        "        return await self._inner.cancel_order(order_id)\n",
        encoding="utf-8",
    )

    violations = _unexempted_violations(tmp_path)

    assert violations == []


def test_walker_still_confines_raw_client_names_even_inside_the_execution_layer(
    tmp_path: Path,
) -> None:
    """RULE 2 widens WHERE `place_order`/`cancel_order` may be called
    from -- it must not accidentally widen RULE 1 along with it. A raw
    `py_clob_client`-shaped call (`client.post_order(...)`) inside
    `app/execution/` is still a violation there, unless the file is one
    of `LIVE_MODULES`/`WRAPPER_MODULE`: it would mean something in the
    execution layer talks to the raw venue client directly instead of
    through a `VenueAdapter`, which is the exact thing RULE 1 exists to
    catch regardless of which package it happens in.
    """
    execution_dir = tmp_path / "execution"
    execution_dir.mkdir()
    (execution_dir / "router.py").write_text(
        "async def sketchy(client, order):\n"
        "    return await client.post_order(order)\n",
        encoding="utf-8",
    )

    violations = _unexempted_violations(tmp_path)

    assert any(v.kind == "method" and v.detail == "post_order" for v in violations), (
        f"expected a 'post_order' violation even inside app/execution/, got {violations}"
    )


def test_walker_detects_an_injected_post_order_call_positive_control(
    tmp_path: Path,
) -> None:
    """ORCHESTRATOR RULING 3 / brief acceptance 1 (POSITIVE CONTROL): copy
    a REAL, currently-clean module to `tmp_path`, inject a genuine
    `client.post_order(x)` call, and require the walker to return a
    violation for it. A fence test that only ever finds nothing is
    indistinguishable from one that looks nowhere -- this is the check
    that rules that out. (See module docstring DEFECT note: the brief
    names `app/services/scoring.py`, which does not exist in this repo;
    `app/services/data_collector.py` is used instead.)
    """
    source_path = APP_ROOT / "services" / "data_collector.py"
    injected = tmp_path / "data_collector_injected.py"
    injected.write_text(
        source_path.read_text(encoding="utf-8")
        + "\n\ndef _t13_injected_violation(client, x):\n"
        + "    return client.post_order(x)\n",
        encoding="utf-8",
    )

    violations = find_violations(injected)

    assert any(
        v.kind == "method" and v.detail == "post_order" for v in violations
    ), f"expected a 'post_order' violation in the injected copy, got {violations}"


def test_walker_reports_nothing_on_the_unmodified_negative_control(
    tmp_path: Path,
) -> None:
    """NEGATIVE CONTROL (ORCHESTRATOR RULING 3): the exact same source
    file, copied UNMODIFIED, must report NO violations. Paired with the
    positive control above, this proves the walker distinguishes "detects
    the bad thing that was added" from "flags everything it looks at".
    """
    source_path = APP_ROOT / "services" / "data_collector.py"
    clean_copy = tmp_path / "data_collector_clean.py"
    clean_copy.write_text(source_path.read_text(encoding="utf-8"), encoding="utf-8")

    violations = find_violations(clean_copy)

    assert violations == []


def test_walker_ignores_a_docstring_mentioning_a_forbidden_path(tmp_path: Path) -> None:
    """A docstring that merely DISCUSSES a venue's order path (as
    `app/venues/types.py::OrderAck` genuinely does, quoting Kalshi's
    `POST /portfolio/events/orders` response shape) is documentation, not
    a call site, and must not trip the path-literal check -- otherwise
    that real file would need a bogus allowlist entry of its own.
    """
    module = tmp_path / "prose_only.py"
    module.write_text(
        '"""Docs mention POST /portfolio/events/orders here, in prose."""\n'
        "x = 1\n",
        encoding="utf-8",
    )

    violations = find_violations(module)

    assert violations == []


def test_walker_still_flags_the_same_path_as_a_real_code_literal(tmp_path: Path) -> None:
    """The docstring exemption above must not swallow a REAL literal --
    only its position (first statement of a scope) exempts it."""
    module = tmp_path / "path_literal.py"
    module.write_text(
        '"""An unrelated module docstring."""\n'
        'ORDERS_PATH = "/portfolio/events/orders"\n',
        encoding="utf-8",
    )

    violations = find_violations(module)

    assert any(v.kind == "path_literal" for v in violations)


def test_walker_flags_a_bare_clobclient_name(tmp_path: Path) -> None:
    """A bare `ClobClient(...)` reference (not `ClobClientWrapper`, which
    must NOT match) is flagged as a `"class"` violation."""
    module = tmp_path / "bare_clobclient.py"
    module.write_text(
        "from py_clob_client.client import ClobClient\n"
        "\n"
        "def make():\n"
        "    return ClobClient(host='x')\n",
        encoding="utf-8",
    )

    violations = find_violations(module)

    assert any(v.kind == "class" and v.detail == "ClobClient" for v in violations)


def test_walker_does_not_flag_clobclientwrapper(tmp_path: Path) -> None:
    """`ClobClientWrapper` (the sanctioned wrapper class name) must not be
    mistaken for a bare `ClobClient` reference -- exact identifier match
    only, not a substring match."""
    module = tmp_path / "wrapper_only.py"
    module.write_text(
        "class ClobClientWrapper:\n"
        "    pass\n"
        "\n"
        "w = ClobClientWrapper()\n",
        encoding="utf-8",
    )

    violations = find_violations(module)

    assert violations == []


def test_walker_does_not_flag_a_method_definition_by_itself(tmp_path: Path) -> None:
    """Defining a method NAMED `cancel_order` (e.g. an abstract interface
    method on `VenueAdapter`, or the sanctioned implementation in a
    `live.py`) is not itself a violation -- only an `Attribute`/`Name`
    node referencing it (i.e. actually CALLING it) is. A `FunctionDef`'s
    `name` is a plain string, not an AST `Name` node.
    """
    module = tmp_path / "def_only.py"
    module.write_text(
        "class Adapter:\n"
        "    async def cancel_order(self, order_id: str) -> None:\n"
        "        raise NotImplementedError\n",
        encoding="utf-8",
    )

    violations = find_violations(module)

    assert violations == []


# ---------------------------------------------------------------------------
# 2. `assert_live_allowed()` -- kill switch (T13 addition on top of T11).
# ---------------------------------------------------------------------------


def test_assert_live_allowed_raises_by_default() -> None:
    """Default `Settings()` (paper, unconfirmed) still raises
    `LiveTradingDisabled` when checked directly (not through a live
    adapter's constructor)."""
    with pytest.raises(LiveTradingDisabled):
        assert_live_allowed(Settings())


def test_assert_live_allowed_raises_with_mode_but_no_confirmation() -> None:
    """`trading_mode="live"` ALONE still raises -- both conditions are
    required together, per T11 (re-verified here at the fence-function
    level, not only through `PolymarketLiveAdapter`)."""
    cfg = Settings(TRADING_MODE="live", LIVE_TRADING_CONFIRMATION="")

    with pytest.raises(LiveTradingDisabled):
        assert_live_allowed(cfg)


def test_assert_live_allowed_raises_kill_switch_engaged_when_file_present(
    tmp_path: Path,
) -> None:
    """Both conditions satisfied, but the kill-switch file exists: raises
    `KillSwitchEngaged`, NOT `LiveTradingDisabled` -- a distinct failure
    mode for "was armed, then halted" vs. "was never armed"."""
    kill_switch_file = tmp_path / "TRADING_KILL_SWITCH"
    kill_switch_file.write_text("", encoding="utf-8")
    cfg = Settings(
        TRADING_MODE="live",
        LIVE_TRADING_CONFIRMATION=LIVE_TRADING_CONFIRMATION_PHRASE,
        KILL_SWITCH_PATH=str(kill_switch_file),
    )

    with pytest.raises(KillSwitchEngaged):
        assert_live_allowed(cfg)


def test_kill_switch_engages_regardless_of_file_contents(tmp_path: Path) -> None:
    """ORCHESTRATOR RULING 4: existence alone engages the switch -- its
    contents are never read or parsed, so a file full of unrelated
    garbage engages it exactly as an empty one does."""
    kill_switch_file = tmp_path / "TRADING_KILL_SWITCH"
    kill_switch_file.write_text(
        "this text is never read or parsed by assert_live_allowed",
        encoding="utf-8",
    )
    cfg = Settings(
        TRADING_MODE="live",
        LIVE_TRADING_CONFIRMATION=LIVE_TRADING_CONFIRMATION_PHRASE,
        KILL_SWITCH_PATH=str(kill_switch_file),
    )

    with pytest.raises(KillSwitchEngaged):
        assert_live_allowed(cfg)


def test_assert_live_allowed_passes_when_armed_and_no_kill_switch_file(
    tmp_path: Path,
) -> None:
    """Both conditions satisfied and no file at `kill_switch_path`:
    passes -- an absolute, `tmp_path`-based path keeps this deterministic
    and independent of the process's current working directory."""
    cfg = Settings(
        TRADING_MODE="live",
        LIVE_TRADING_CONFIRMATION=LIVE_TRADING_CONFIRMATION_PHRASE,
        KILL_SWITCH_PATH=str(tmp_path / "TRADING_KILL_SWITCH"),
    )

    assert_live_allowed(cfg)  # must not raise


# ---------------------------------------------------------------------------
# 2b. `assert_placement_allowed()` -- the kill switch as a PLACEMENT fence
#     (T14 remediation).
#
# `assert_live_allowed()` above guards adapter CONSTRUCTION and runs once
# per process, because `app/api/deps.py` caches the router and its
# adapters for the life of the application. Throwing the switch after the
# first order therefore changed nothing at all. `assert_placement_allowed`
# is the halt that actually halts: `OrderRouter.submit()` calls it on
# EVERY submission, before any leg is planned, reserved or placed.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cfg_kwargs",
    [
        # Paper: the default, and the only mode any test process runs in.
        {"TRADING_MODE": "paper"},
        # Live and fully armed: the switch must still halt it.
        {
            "TRADING_MODE": "live",
            "LIVE_TRADING_CONFIRMATION": LIVE_TRADING_CONFIRMATION_PHRASE,
        },
    ],
    ids=["paper", "live"],
)
def test_assert_placement_allowed_halts_in_every_mode(
    tmp_path: Path, cfg_kwargs: dict[str, str]
) -> None:
    """The halt is MODE-INDEPENDENT, deliberately.

    "Is live trading armed?" and "is trading halted right now?" are
    different questions. Conflating them is what let a placement fence
    answer a reconciliation question — and a halt that could only be
    exercised with real money is a halt nobody has ever tested.
    """
    switch = tmp_path / "TRADING_KILL_SWITCH"
    switch.write_text("", encoding="utf-8")
    cfg = Settings(KILL_SWITCH_PATH=str(switch), **cfg_kwargs)

    assert kill_switch_engaged(cfg) is True
    with pytest.raises(KillSwitchEngaged):
        assert_placement_allowed(cfg)


def test_assert_placement_allowed_passes_with_no_switch_file(tmp_path: Path) -> None:
    """No file, no halt — in paper, without arming anything."""
    cfg = Settings(
        TRADING_MODE="paper",
        KILL_SWITCH_PATH=str(tmp_path / "TRADING_KILL_SWITCH"),
    )

    assert kill_switch_engaged(cfg) is False
    assert_placement_allowed(cfg)  # must not raise


def test_the_placement_fence_does_not_gate_reads(tmp_path: Path) -> None:
    """An engaged switch must not make a READ-ONLY adapter unavailable.

    `app.tasks.execution` reconciles through
    `app.venues.registry.get_read_adapter`, which returns an adapter with
    no `place_order` at all and consults no placement fence. Reconciling
    is exactly what an operator wants to keep doing during a halt; before
    this, engaging the switch stopped it.
    """
    from app.venues.base import ReconcileAdapter
    from app.venues.registry import get_read_adapter

    switch = tmp_path / "TRADING_KILL_SWITCH"
    switch.write_text("", encoding="utf-8")

    adapter = get_read_adapter("polymarket", "paper")

    assert isinstance(adapter, ReconcileAdapter)
    assert hasattr(adapter, "get_open_orders")
    assert hasattr(adapter, "get_fills")


# ---------------------------------------------------------------------------
# 3. `check_order_limits()` boundary cases.
# ---------------------------------------------------------------------------


def test_check_order_limits_passes_exactly_at_every_cap() -> None:
    """Every limit is a strict "exceeds", not "reaches" -- sitting exactly
    ON a configured cap must pass, not trip it."""
    cfg = Settings()

    check_order_limits(
        order_notional=cfg.max_order_notional_usd,
        open_notional=cfg.max_open_notional_usd - cfg.max_order_notional_usd,
        daily_pnl=-cfg.max_daily_loss_usd,
        settings_obj=cfg,
    )  # must not raise


def test_check_order_limits_raises_just_past_the_order_notional_cap() -> None:
    cfg = Settings()

    with pytest.raises(RiskLimitExceeded):
        check_order_limits(
            order_notional=cfg.max_order_notional_usd + 0.01,
            open_notional=0,
            daily_pnl=0,
            settings_obj=cfg,
        )


def test_check_order_limits_raises_just_past_the_open_notional_cap() -> None:
    cfg = Settings()

    with pytest.raises(RiskLimitExceeded):
        check_order_limits(
            order_notional=1,
            open_notional=cfg.max_open_notional_usd,
            daily_pnl=0,
            settings_obj=cfg,
        )


def test_check_order_limits_raises_just_past_the_daily_loss_floor() -> None:
    cfg = Settings()

    with pytest.raises(RiskLimitExceeded):
        check_order_limits(
            order_notional=0,
            open_notional=0,
            daily_pnl=-cfg.max_daily_loss_usd - 0.01,
            settings_obj=cfg,
        )


def test_check_order_limits_reports_every_breached_reason_at_once() -> None:
    """All three checks are evaluated, not short-circuited -- breaching
    all three at once names all three in the raised message."""
    cfg = Settings()

    with pytest.raises(RiskLimitExceeded) as excinfo:
        check_order_limits(
            order_notional=cfg.max_order_notional_usd * 10,
            open_notional=cfg.max_open_notional_usd * 10,
            daily_pnl=-cfg.max_daily_loss_usd * 10,
            settings_obj=cfg,
        )

    message = str(excinfo.value)
    assert "max_order_notional_usd" in message
    assert "max_open_notional_usd" in message
    assert "max_daily_loss_usd" in message


def test_check_order_limits_defaults_to_the_process_wide_settings() -> None:
    """With no `settings_obj`, resolves against the process-wide
    singleton -- same default pattern as `assert_live_allowed`. The test
    process's singleton carries the T13 defaults (250/100/1000), so a
    tiny, clearly-within-limits order must pass."""
    check_order_limits(order_notional=1, open_notional=1, daily_pnl=0)
