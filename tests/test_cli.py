from __future__ import annotations


async def test_doctor_never_calls_live_search(monkeypatch):
    """A doctor that spends money is a doctor nobody runs."""
    import vos.cli as cli

    calls: list[str] = []

    async def fake_get(url: str, **_):
        calls.append(url)

        class R:
            status_code = 200

            def json(self):
                return {"data": [{"id": "grok-4.1-fast"}]}

            def raise_for_status(self):
                return None

        return R()

    monkeypatch.setattr(cli, "_xai_get", fake_get)
    await cli._check_xai_live("key", "https://api.x.ai/v1", "grok-4.1-fast")

    assert all("chat/completions" not in url for url in calls)
