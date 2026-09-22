"""Cross-layer offline acceptance without paid providers or browser dependencies."""

from unittest.mock import patch

import httpx

from paper2html.ingestion._client import RateLimitedClient, _reset_singleton
from paper2html.pipeline.providers import MockProvider
from paper2html.pipeline.providers.base import ProviderResponse
from tests.test_arxiv_ingest import _ATOM
from tests.test_cli_process import _invoke, _make_fixture_pdf


def assert_library_open(cache, output):
    listed = _invoke(cache, "list")
    assert listed.exit_code == 0, listed.output
    with patch("paper2html.cli.webbrowser.open") as opened:
        result = _invoke(cache, "open", "1")
        assert result.exit_code == 0, result.output
        opened.assert_called_once_with(output.as_uri())


def test_real_pdf_partial_synthesis_recovers_then_cache_replays(tmp_path):
    class Recoverable(MockProvider):
        broken = True
        synthesis_calls = 0

        async def complete(self, messages, images, tools, **kwargs):
            if self.broken and "SYNTHESIS" in messages[-1].content:
                self.synthesis_calls += 1
                return ProviderResponse(content="null")
            return await super().complete(messages, images, tools, **kwargs)

    pdf = _make_fixture_pdf(tmp_path)
    cache, out = tmp_path / "cache", tmp_path / "result.html"
    provider = Recoverable()
    args = ("--provider", "mock", "process", str(pdf), "-o", str(out), "--overwrite")
    with patch("paper2html.cli.build_provider", return_value=provider):
        partial = _invoke(cache, *args)
        assert partial.exit_code == 0, partial.output
        assert "warning" in partial.output.lower()
        assert provider.synthesis_calls == 2 and out.is_file()
        calls = provider.call_count
        provider.broken = False
        recovered = _invoke(cache, *args)
        assert recovered.exit_code == 0, recovered.output
        assert provider.call_count > calls
        calls = provider.call_count
        content = out.read_bytes()
        replay = _invoke(cache, *args)
        assert replay.exit_code == 0, replay.output
        assert provider.call_count == calls
        assert out.read_bytes() == content
    assert_library_open(cache, out)


def test_arxiv_mock_transport_through_cli_library(tmp_path, monkeypatch):
    requests = []

    def handler(request):
        requests.append(str(request.url))
        if request.url.host == "export.arxiv.org":
            return httpx.Response(200, content=_ATOM)
        assert str(request.url) == "https://arxiv.org/html/2301.07041"
        return httpx.Response(
            200,
            text="<section><h2>Introduction</h2><p>term_a and term_b describe this result.</p></section>",
        )

    monkeypatch.setattr("paper2html.ingestion._client._RATE_LIMIT_SECONDS", 0.0)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        _reset_singleton(RateLimitedClient(http_client=client))
        out = tmp_path / "arxiv.html"
        cache = tmp_path / "cache"
        result = _invoke(
            cache, "--provider", "mock", "process", "2301.07041", "-o", str(out)
        )
        assert result.exit_code == 0, result.output
    assert len(requests) == 2
    assert "Test Paper Title" in out.read_text()
    assert 'class="term-def"' in out.read_text()
    assert_library_open(cache, out)
