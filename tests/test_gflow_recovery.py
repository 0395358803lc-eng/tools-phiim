from types import SimpleNamespace

import pytest

from flow_story_studio.flow_integration.errors import FlowIntegrationError
from flow_story_studio.flow_integration.recovery import _download_migrated_record


class _Response:
    def __init__(self, status: int, *, headers=None, body=b""):
        self.status = status
        self.headers = headers or {}
        self._body = body

    async def body(self):
        return self._body


class _Request:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_migrated_recovery_follows_only_allowed_google_redirects(tmp_path):
    mp4 = b"\x00\x00\x00\x18ftypisom" + b"x" * 32
    request = _Request(
        [
            _Response(
                302,
                headers={"location": "https://flow-content.google/video/final"},
            ),
            _Response(200, body=mp4),
        ]
    )
    page = SimpleNamespace(request=request)
    record = SimpleNamespace(
        media_id="media-1",
        video_url="https://lh3.googleusercontent.com/start",
        poster_url=None,
    )

    result = await _download_migrated_record(page, record, tmp_path)

    assert result == tmp_path / "media-1.mp4"
    assert result.read_bytes() == mp4
    assert [call[0] for call in request.calls] == [
        "https://lh3.googleusercontent.com/start",
        "https://flow-content.google/video/final",
    ]
    assert all(call[1]["max_redirects"] == 0 for call in request.calls)


@pytest.mark.asyncio
async def test_migrated_recovery_blocks_non_google_redirect(tmp_path):
    request = _Request(
        [
            _Response(
                302,
                headers={"location": "https://evil.example/video.mp4"},
            )
        ]
    )
    page = SimpleNamespace(request=request)
    record = SimpleNamespace(
        media_id="media-2",
        video_url="https://lh3.googleusercontent.com/start",
        poster_url=None,
    )

    with pytest.raises(FlowIntegrationError, match="blocked redirect"):
        await _download_migrated_record(page, record, tmp_path)

    assert len(request.calls) == 1
    assert not (tmp_path / "media-2.mp4").exists()
