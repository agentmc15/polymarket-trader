"""Every request body model in `app/api/routes/` must reject unknown fields (T33).

THE BUG THIS IS A FENCE AGAINST HAS HAPPENED TWICE, IN TWO SUBSYSTEMS,
AND BOTH TIMES IT WAS SILENT.

  * The frontend posted `slippage_bps`; `BacktestRequest` declares
    `slippage_value`. Pydantic's DEFAULT `extra="ignore"` discarded the
    key, the field fell back to `0.001`, and every backtest ever run
    used the default slippage no matter what the form said. No error, no
    warning, wrong numbers.
  * `CLAUDE.md` documented `TRADING_KILL_SWITCH_PATH`; `Settings` binds
    `KILL_SWITCH_PATH`. Same silence, worse blast radius: an operator
    halting trading during an incident would have halted nothing.

Both are the same failure — a caller can be WRONG WITHOUT BEING TOLD —
and `extra="forbid"` is what turns it into a 422 that names the offending
field. `tests/test_settings_aliases.py` is the settings half of this
fence; this module is the API half.

REQUEST MODELS ONLY, AND THE CHECK IS DERIVED, NOT LISTED. `extra` is a
rule about parsing caller input, so it belongs on the models that parse
a request body and on no others: a response model serializes outward,
has no caller input to reject, and (were it ever fed back through
`model_validate`) would only gain a way to fail. So the set checked here
is re-derived from the LIVE ROUTE TABLE on every run — whatever
FastAPI actually parses a body into today, plus anything nested inside
it — rather than from a hand-maintained list that the 38th model added
next month would quietly fall outside of. `test_the_walker_finds_the_
request_models_it_is_supposed_to_check` and
`test_the_walker_flags_a_permissive_model_in_an_app_it_has_never_seen`
are the positive and negative controls for that walker, in the spirit of
`tests/test_fences.py`: a fence that passes because it found nothing is
indistinguishable from one that passes because it looked nowhere.
"""
from typing import Any, ForwardRef, get_args, get_type_hints

import pytest
from fastapi import Depends, FastAPI
from fastapi.dependencies.utils import get_flat_dependant
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError

from app.api.routes.backtesting import BacktestRequest, SweepRequest
from app.main import app

#: Every model FastAPI parses a request body into today, by dotted name.
#: Stated so the walker can be caught looking NOWHERE — if a route is
#: renamed or a body model is dropped, that is a real change and this
#: list is where it gets noticed, not a maintenance chore standing in
#: for the check itself (the check below walks the route table).
EXPECTED_REQUEST_MODELS = {
    "app.api.routes.backtesting.BacktestRequest",
    "app.api.routes.backtesting.SweepRequest",
    "app.api.routes.bots.BotConfig",
    "app.api.routes.links.ApproveRequest",
    "app.api.routes.links.RejectRequest",
    "app.api.routes.trading.OrderRequest",
}


class _ForwardRefOuter(BaseModel):
    """T39 F4 fixture: names `_ForwardRefInner`, defined LATER in this file, by string.

    Nothing here (or below) ever calls `model_rebuild()`, so
    `model_fields["nested"].annotation` stays an unresolved
    `typing.ForwardRef` forever (asserted in
    `test_the_walker_sees_a_permissive_model_behind_a_forward_reference`)
    even though this model validates a real nested payload correctly —
    pydantic/FastAPI resolve the reference internally the first time the
    model is used, independently of whether `field.annotation` was ever
    updated to match. Declared at MODULE scope, not inside a test
    function: `typing.get_type_hints` resolves a string annotation
    against the DEFINING MODULE's globals, which a class local to a
    function body is never a member of.
    """

    name: str
    nested: "_ForwardRefInner"


class _ForwardRefInner(BaseModel):
    """Deliberately permissive (no `extra="forbid"`) — the model the pre-T39
    walker could not reach because it never got past `_ForwardRefOuter`'s
    unresolved `ForwardRef`.
    """

    slippage_bps: int = 0


def _field_annotations(model: type[BaseModel]) -> dict[str, Any]:
    """Return `model`'s field annotations, resolved where possible.

    `field.annotation` (pydantic v2) is only guaranteed accurate once the
    model has been explicitly rebuilt (`model_rebuild()`) or its class
    body contained no string/forward-referenced annotation at definition
    time. A model that nests another model declared LATER in the same
    module — a wholly ordinary way to write two related request models —
    can validate correctly (pydantic resolves the reference lazily,
    internally, the first time it is needed) while `field.annotation`
    for that field is left holding the unresolved `typing.ForwardRef`
    forever, because nothing ever called `model_rebuild()` on it (T39
    F4). `typing.get_type_hints()` forces the same resolution
    `model_rebuild()` would and hands back the real class either way, so
    it is used here instead of trusting `field.annotation` directly.

    Args:
        model: The pydantic model whose fields are being walked.

    Returns:
        dict[str, Any]: Field name -> resolved annotation, falling back
            to `field.annotation` for any field `get_type_hints` could
            not resolve (e.g. one my referencing a name local to a test
            function) rather than raising — a model that could not be
            built at all would already have failed at import time, long
            before this fence runs.
    """
    try:
        hints = get_type_hints(model, include_extras=True)
    except NameError:
        hints = {}
    return {
        field_name: hints.get(field_name, field.annotation)
        for field_name, field in model.model_fields.items()
    }


def _reachable_models(
    annotation: Any, seen: dict[str, type[BaseModel]]
) -> dict[str, type[BaseModel]]:
    """Collect every `BaseModel` reachable from a type annotation.

    Recurses through `list[...]`/`dict[...]`/`X | None` arguments and
    into each model's own fields (resolved via `_field_annotations`, not
    `field.annotation` directly — see its docstring for why), so a body
    model that NESTS another model is covered too, including one only
    reachable through a forward reference — the nested one parses caller
    input just as directly as its parent.

    Args:
        annotation: A type annotation from a route parameter or a model
            field.
        seen: Accumulator, keyed by dotted model name (also the cycle
            guard).

    Returns:
        dict[str, type[BaseModel]]: `seen`, extended in place.
    """
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        name = f"{annotation.__module__}.{annotation.__qualname__}"
        if name in seen:
            return seen
        seen[name] = annotation
        for field_annotation in _field_annotations(annotation).values():
            _reachable_models(field_annotation, seen)
        return seen
    for arg in get_args(annotation):
        _reachable_models(arg, seen)
    return seen


def _request_body_models(application: FastAPI) -> dict[str, type[BaseModel]]:
    """Return every model `application` parses a request BODY into.

    Walks `get_flat_dependant(route.dependant)` rather than
    `route.dependant` directly (T39 F4): FastAPI's `Dependant` tree keeps
    a route's OWN body params separate from the body params of anything
    it `Depends(...)` on, and only the flattened form merges them. A body
    model declared on a `Depends(...)` callable parses exactly the same
    caller-supplied JSON a route's own body parameter would — the caller
    cannot tell the difference from the request they sent — so it must
    be checked identically; `route.dependant.body_params` alone is blind
    to it.

    Args:
        application: The FastAPI app to inspect.

    Returns:
        dict[str, type[BaseModel]]: Body models by dotted name.
    """
    found: dict[str, type[BaseModel]] = {}
    for route in application.routes:
        if not isinstance(route, APIRoute):
            continue
        for param in get_flat_dependant(route.dependant).body_params:
            _reachable_models(getattr(param, "type_", None), found)
    return found


def _response_models(application: FastAPI) -> dict[str, type[BaseModel]]:
    """Return every model `application` declares as a `response_model`.

    Args:
        application: The FastAPI app to inspect.

    Returns:
        dict[str, type[BaseModel]]: Response models by dotted name,
            including everything nested inside them.
    """
    found: dict[str, type[BaseModel]] = {}
    for route in application.routes:
        if not isinstance(route, APIRoute):
            continue
        _reachable_models(route.response_model, found)
    return found


def test_the_walker_finds_the_request_models_it_is_supposed_to_check() -> None:
    """Positive control: the walker looks somewhere, and finds all six."""
    found = set(_request_body_models(app))

    assert found >= EXPECTED_REQUEST_MODELS, EXPECTED_REQUEST_MODELS - found


def test_every_request_body_model_forbids_unknown_fields() -> None:
    """The fence itself. A new body model without `extra="forbid"` fails here."""
    permissive = sorted(
        name
        for name, model in _request_body_models(app).items()
        if model.model_config.get("extra") != "forbid"
    )

    assert permissive == []


def test_the_walker_flags_a_permissive_model_in_an_app_it_has_never_seen() -> None:
    """Negative control: the check FAILS on a model that ignores extras.

    Without this, `test_every_request_body_model_forbids_unknown_fields`
    would pass identically if `_request_body_models` returned `{}` — the
    fence would be proving nothing. A throwaway app carrying one
    deliberately permissive body model must be flagged.
    """
    class _Permissive(BaseModel):
        name: str

    probe = FastAPI()

    @probe.post("/probe")
    async def _handler(body: _Permissive) -> dict[str, str]:  # pragma: no cover
        return {"name": body.name}

    found = _request_body_models(probe)
    permissive_name = f"{_Permissive.__module__}.{_Permissive.__qualname__}"

    assert set(found) == {permissive_name}
    assert [
        name
        for name, model in found.items()
        if model.model_config.get("extra") != "forbid"
    ] == [permissive_name]


def test_the_walker_sees_a_permissive_model_hidden_behind_depends() -> None:
    """T39 F4, shape 1: a body model declared on a `Depends(...)` callable.

    `route.dependant.body_params` holds only a route's OWN body
    parameters; a sub-dependency's body parameters live inside their own
    `Dependant`, nested under `route.dependant.dependencies`, and only
    `get_flat_dependant()` merges the whole tree. A caller posting a
    JSON body cannot tell which of the two shapes handled it — both
    parse the identical bytes — so a walker blind to one is a walker a
    permissive model can hide behind.
    """
    class _HiddenBody(BaseModel):
        """Mirrors the ORIGINAL bug's shape (`BacktestRequest`/
        `slippage_bps`, this module's docstring): a caller posts one
        spelling, the model declares another, and with no
        `extra="forbid"` the mismatch is silent rather than a 422.
        """

        slippage_value: float = 0.001

    async def _dep(body: _HiddenBody) -> _HiddenBody:
        return body

    probe = FastAPI()

    @probe.post("/probe")
    async def _handler(body: _HiddenBody = Depends(_dep)) -> dict[str, float]:
        return {"slippage_value": body.slippage_value}

    hidden_name = f"{_HiddenBody.__module__}.{_HiddenBody.__qualname__}"

    # RED: the pre-T39 extraction read only `route.dependant.body_params`
    # and never descended into `route.dependant.dependencies` at all.
    old_found: dict[str, type[BaseModel]] = {}
    for route in probe.routes:
        if isinstance(route, APIRoute):
            for param in route.dependant.body_params:
                _reachable_models(getattr(param, "type_", None), old_found)
    assert old_found == {}, "the pre-T39 walker must find nothing here"

    # GREEN: `get_flat_dependant` merges the sub-dependency's body params.
    found = _request_body_models(probe)
    assert hidden_name in found
    assert found[hidden_name] is _HiddenBody
    assert found[hidden_name].model_config.get("extra") != "forbid"

    # The original bug, reproduced through the one shape the old walker
    # could not see: `TestClient` POSTs the frontend's actual spelling,
    # and it is silently discarded rather than rejected.
    client = TestClient(probe)
    response = client.post("/probe", json={"slippage_bps": 50})
    assert response.status_code == 200, response.text
    assert response.json() == {"slippage_value": 0.001}


def test_the_walker_sees_a_permissive_model_behind_a_forward_reference() -> None:
    """T39 F4, shape 2: a nested model reachable only through a `ForwardRef`.

    `_ForwardRefOuter.nested` names `_ForwardRefInner` (defined later in
    this module) as a string, and neither model ever calls
    `model_rebuild()`. `model_fields["nested"].annotation` therefore
    stays an unresolved `typing.ForwardRef` even though `_ForwardRefOuter`
    validates a real nested payload correctly. The pre-T39 recursion
    walked `field.annotation` directly and had nothing to recurse into
    once it hit that `ForwardRef`, so `_ForwardRefInner` — one hop from a
    request body — was invisible to it.
    """
    field = _ForwardRefOuter.model_fields["nested"]
    assert isinstance(field.annotation, ForwardRef), (
        "fixture invariant: if pydantic ever resolves this eagerly, this "
        "test is not exercising the gap it claims to"
    )

    probe = FastAPI()

    @probe.post("/probe")
    async def _handler(body: _ForwardRefOuter) -> dict[str, str]:  # pragma: no cover
        return {"name": body.name}

    inner_name = f"{_ForwardRefInner.__module__}.{_ForwardRefInner.__qualname__}"

    # RED: the pre-T39 recursion (follow `field.annotation` verbatim,
    # never `typing.get_type_hints`) cannot see past the `ForwardRef`.
    def _old_reachable_models(
        annotation: Any, seen: dict[str, type[BaseModel]]
    ) -> dict[str, type[BaseModel]]:
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            name = f"{annotation.__module__}.{annotation.__qualname__}"
            if name in seen:
                return seen
            seen[name] = annotation
            for old_field in annotation.model_fields.values():
                _old_reachable_models(old_field.annotation, seen)
            return seen
        for arg in get_args(annotation):
            _old_reachable_models(arg, seen)
        return seen

    old_found: dict[str, type[BaseModel]] = {}
    for route in probe.routes:
        if isinstance(route, APIRoute):
            for param in route.dependant.body_params:
                _old_reachable_models(getattr(param, "type_", None), old_found)
    assert inner_name not in old_found

    # GREEN: the current walker resolves the forward reference and finds it.
    found = _request_body_models(probe)
    assert inner_name in found
    assert found[inner_name] is _ForwardRefInner
    assert found[inner_name].model_config.get("extra") != "forbid"


def test_no_response_model_forbids_extras() -> None:
    """The setting means exactly one thing: "this model parses caller input".

    A blanket base class applied to every model in `app/api/routes/`
    would satisfy the fence above while making `extra="forbid"`
    meaningless as a marker — a reader could no longer tell which models
    face a client. Response models serialize OUTWARD; the setting there
    buys nothing and can only fail a round trip through
    `model_validate`.
    """
    forbidding = sorted(
        name
        for name, model in _response_models(app).items()
        if model.model_config.get("extra") == "forbid"
    )

    assert forbidding == []


def test_a_backtest_request_naming_slippage_bps_is_refused_not_defaulted() -> None:
    """The original bug, at the model that had it.

    `slippage_bps` is the key the frontend actually posted. Before T33
    this constructed a perfectly valid `BacktestRequest` whose
    `slippage_value` was the DEFAULT — the caller's number silently gone.
    """
    payload: dict[str, Any] = {
        "strategy": "binary_complement_arbitrage",
        "start_date": "2024-01-01T00:00:00Z",
        "end_date": "2024-06-01T00:00:00Z",
    }

    # The `type: ignore` is the static half of the same finding:
    # `pyproject.toml` sets pydantic-mypy's `init_forbid_extra`, so mypy
    # rejects this call too. That is the point of the test, not a defect
    # in it — the ignore says "yes, this call is wrong on purpose".
    with pytest.raises(ValidationError) as exc_info:
        BacktestRequest(**payload, slippage_bps=50)  # type: ignore[call-arg]

    assert [
        error["loc"]
        for error in exc_info.value.errors()
        if error["type"] == "extra_forbidden"
    ] == [("slippage_bps",)]

    # The value that used to be applied silently in its place...
    assert BacktestRequest(**payload).slippage_value == 0.001
    # ...and the spelling the model actually declares, which still works.
    assert BacktestRequest(**payload, slippage_value=0.005).slippage_value == 0.005


def test_the_two_backtest_bodies_do_not_silently_absorb_each_other() -> None:
    """A sweep body posted to `POST /backtests` must fail, not run one level.

    `capital_levels` is `SweepRequest`'s field and not
    `BacktestRequest`'s. Sent to the single-run route it used to be
    discarded, and the caller got back a normal `BacktestResponse` for a
    ONE-capital run it never asked for — a sweep that silently wasn't.
    """
    payload: dict[str, Any] = {
        "strategy": "binary_complement_arbitrage",
        "start_date": "2024-01-01T00:00:00Z",
        "end_date": "2024-06-01T00:00:00Z",
    }

    with pytest.raises(ValidationError) as exc_info:
        BacktestRequest(**payload, capital_levels=[100.0, 1000.0])  # type: ignore[call-arg]

    assert [
        error["loc"]
        for error in exc_info.value.errors()
        if error["type"] == "extra_forbidden"
    ] == [("capital_levels",)]

    # `SweepRequest` — which does declare it — accepts the same body, and
    # forbids extras in its own right rather than by inheritance alone.
    assert SweepRequest(**payload, capital_levels=[100.0, 1000.0]).capital_levels == [
        100.0,
        1000.0,
    ]
    with pytest.raises(ValidationError):
        SweepRequest(**payload, capital_levls=[100.0])  # type: ignore[call-arg]
