"""Use an honest application user agent for arXiv's public endpoints."""

import httpx

from paper2html import __version__
from paper2html.ingestion._client import RateLimitedClient


def test_application_user_agent_and_explicit_override(monkeypatch):
    monkeypatch.setattr("paper2html.ingestion._client._RATE_LIMIT_SECONDS", 0)
    agents = []

    def handle(request):
        agents.append(request.headers["User-Agent"])
        assert request.headers["Accept-Encoding"] == "identity"
        return httpx.Response(200, content=b"ok")

    with httpx.Client(transport=httpx.MockTransport(handle)) as transport:
        client = RateLimitedClient(transport)
        client.get("https://export.arxiv.org/api/query?id_list=2511.12723")
        client.get(
            "https://arxiv.org/html/2511.12723",
            headers={"User-Agent": "custom-reader/1"},
        )
    assert agents == [
        f"paper2html/{__version__} (+https://github.com/JewelsHovan/paper2html)",
        "custom-reader/1",
    ]


def test_single_id_metadata_lookup_uses_minimal_query(monkeypatch):
    from paper2html.ingestion._client import _reset_singleton
    from paper2html.ingestion._metadata import fetch_metadata
    from tests.test_arxiv_ingest import _ATOM

    monkeypatch.setattr("paper2html.ingestion._client._RATE_LIMIT_SECONDS", 0)

    def handle(request):
        # Single-id requests need no pagination. The live endpoint can reject
        # redundant query parameters even though the minimal request succeeds.
        assert list(request.url.params.multi_items()) == [("id_list", "2301.07041")]
        return httpx.Response(200, content=_ATOM)

    with httpx.Client(transport=httpx.MockTransport(handle)) as transport:
        _reset_singleton(RateLimitedClient(transport))
        assert fetch_metadata("2301.07041").arxiv_id == "2301.07041"
