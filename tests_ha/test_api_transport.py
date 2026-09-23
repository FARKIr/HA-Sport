"""Transport tests against a local HTTP server (curl_cffi + aiohttp fallback)."""
import aiohttp
from aiohttp import web
import pytest

from custom_components.ha_sport import api
from custom_components.ha_sport.api import SofascoreClient, SportApiError


@pytest.fixture
async def server(socket_enabled):
    hits = []

    async def ok(request):
        hits.append(request.headers.get("User-Agent", ""))
        return web.json_response({"categories": [{"id": 1, "alpha2": "CZ"}]})

    async def forbidden(request):
        return web.Response(status=403, text="Forbidden")

    app = web.Application()
    app.router.add_get("/ok/api/v1/sport/football/categories", ok)
    app.router.add_get("/bad/api/v1/sport/football/categories", forbidden)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}", hits
    await runner.cleanup()
    await api.async_close_shared_session()


async def test_curl_transport_used_first(server, monkeypatch):
    base, hits = server
    monkeypatch.setattr(api, "FALLBACK_BASE_URLS", ())
    async with aiohttp.ClientSession() as session:
        client = SofascoreClient(session, f"{base}/ok/api/v1")
        assert client._transports[0] == "curl"
        cats = await client.categories("football")
    assert cats[0]["alpha2"] == "CZ"
    assert "Chrome" in hits[0]  # impersonated browser user agent


async def test_fallback_and_error_message(server, monkeypatch):
    base, _ = server
    monkeypatch.setattr(api, "FALLBACK_BASE_URLS", (f"{base}/ok/api/v1",))
    async with aiohttp.ClientSession() as session:
        client = SofascoreClient(session, f"{base}/bad/api/v1")
        assert (await client.categories("football"))[0]["id"] == 1
        # working base url promoted
        assert client._bases[0].endswith("/ok/api/v1")

    monkeypatch.setattr(api, "FALLBACK_BASE_URLS", ())
    async with aiohttp.ClientSession() as session:
        client = SofascoreClient(session, f"{base}/bad/api/v1")
        with pytest.raises(SportApiError) as err:
            await client.get("/sport/football/categories")
    assert "HTTP 403" in str(err.value) and "(curl)" in str(err.value) and "(aiohttp)" in str(err.value)
