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
from typing import Any, get_args

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
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


def _reachable_models(
    annotation: Any, seen: dict[str, type[BaseModel]]
) -> dict[str, type[BaseModel]]:
    """Collect every `BaseModel` reachable from a type annotation.

    Recurses through `list[...]`/`dict[...]`/`X | None` arguments and
    into each model's own fields, so a body model that NESTS another
    model is covered too — the nested one parses caller input just as
    directly as its parent.

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
        for field in annotation.model_fields.values():
            _reachable_models(field.annotation, seen)
        return seen
    for arg in get_args(annotation):
        _reachable_models(arg, seen)
    return seen


def _request_body_models(application: FastAPI) -> dict[str, type[BaseModel]]:
    """Return every model `application` parses a request BODY into.

    Args:
        application: The FastAPI app to inspect.

    Returns:
        dict[str, type[BaseModel]]: Body models by dotted name.
    """
    found: dict[str, type[BaseModel]] = {}
    for route in application.routes:
        if not isinstance(route, APIRoute):
            continue
        for param in route.dependant.body_params:
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
