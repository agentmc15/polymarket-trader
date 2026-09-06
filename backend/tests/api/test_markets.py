"""`app/api/routes/markets.py`'s response envelope shape (T41).

`list_markets` is still a `# TODO` stub — this does not test market
listing behavior, only that the envelope it returns matches
`frontend/src/types/index.ts::PaginatedResponse<T>` (`{data, total,
skip, limit}`), which is what `api.getMarkets()` declares and
`useMarkets` reads (`response.data || []`). Markets is the default,
live tab in `App.tsx`: today the stub's `[]` and the frontend's `|| []`
fallback both render "No markets found" regardless of the envelope key,
which is exactly why this drifted unnoticed. The moment someone
implements real listing without reading this test, a `markets` key (or
any other rename) would produce a populated backend with a permanently
blank UI and no error anywhere — this test exists to make that
impossible to do silently.

See the long comment above the route's `return` for the full reasoning
on why `data` was picked over the sibling `markets`/`backtests`/
`opportunities` convention.
"""
from httpx import AsyncClient


async def test_list_markets_envelope_shape(client: AsyncClient) -> None:
    """`GET /markets` returns exactly `{data, total, skip, limit}`."""
    response = await client.get("/api/v1/markets")

    assert response.status_code == 200
    body = response.json()

    assert set(body.keys()) == {"data", "total", "skip", "limit"}
    assert isinstance(body["data"], list)
    assert isinstance(body["total"], int)
    assert isinstance(body["skip"], int)
    assert isinstance(body["limit"], int)


async def test_list_markets_pagination_params_roundtrip(client: AsyncClient) -> None:
    """`skip`/`limit` query params land unchanged in the envelope."""
    response = await client.get("/api/v1/markets", params={"skip": 5, "limit": 10})

    assert response.status_code == 200
    body = response.json()

    assert body["skip"] == 5
    assert body["limit"] == 10
