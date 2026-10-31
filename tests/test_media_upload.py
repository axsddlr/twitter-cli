"""Video/image media upload: chunking, size and type guards, STATUS polling.

`upload_media` drives X's chunked INIT -> APPEND -> FINALIZE (-> STATUS) flow.
It was previously exercised only by the image path, so the video branch (new in
this change) had no coverage: segment sizing, async-processing polling, and the
failure states all went untested.
"""

from __future__ import annotations

import base64
import json

import click

import pytest

from twitter_cli import client as client_mod
from twitter_cli.client import TwitterClient
from twitter_cli.exceptions import MediaUploadError

FOUR_MB = 4 * 1024 * 1024


class _Resp:
    def __init__(self, status_code=200, payload=None, text=None):
        self.status_code = status_code
        self.text = text if text is not None else json.dumps(payload or {})


class _FakeSession:
    """Records every upload call and replays scripted responses by command."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.posts = []
        self.gets = []

    def post(self, url, headers=None, data=None, timeout=None):
        self.posts.append({"url": url, "data": data, "headers": headers, "timeout": timeout})
        return self._responses.pop(0)

    def get(self, url, headers=None, params=None, timeout=None):
        self.gets.append({"url": url, "params": params, "headers": headers})
        return self._responses.pop(0)


def _make_client():
    client = TwitterClient.__new__(TwitterClient)
    client._auth_token = "tok"
    client._ct0 = "ct0"
    client._cookie_string = None
    client._client_transaction = None
    client._write_delay = lambda: None
    client._build_headers = lambda url="", method="GET": {
        "Cookie": "auth_token=tok; ct0=ct0",
        "Content-Type": "application/json",
    }
    return client


def _install_session(monkeypatch, responses):
    session = _FakeSession(responses)
    monkeypatch.setattr(client_mod, "_get_cffi_session", lambda: session)
    return session


@pytest.fixture()
def no_sleep(monkeypatch):
    """Make STATUS back-off instant and record requested waits."""
    waits = []
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: waits.append(s))
    return waits


def _write_file(tmp_path, name, size):
    path = tmp_path / name
    path.write_bytes(b"\0" * size)
    return str(path)


# â”€â”€ Video path â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def test_video_upload_chunks_into_4mb_segments(tmp_path, monkeypatch, no_sleep):
    """An 11 MB video must INIT, append ceil(11/4)=3 segments, then FINALIZE."""
    path = _write_file(tmp_path, "clip.mp4", 11 * 1024 * 1024)
    session = _install_session(
        monkeypatch,
        [
            _Resp(payload={"media_id_string": "m1"}),
            _Resp(payload={}),  # append 0
            _Resp(payload={}),  # append 1
            _Resp(payload={}),  # append 2
            _Resp(payload={}),  # finalize: already processed
        ],
    )

    media_id = _make_client().upload_media(path)

    assert media_id == "m1"
    init = session.posts[0]["data"]
    assert init["command"] == "INIT"
    assert init["media_category"] == "tweet_video"
    assert init["total_bytes"] == str(11 * 1024 * 1024)

    appends = [p["data"] for p in session.posts if p["data"]["command"] == "APPEND"]
    assert len(appends) == 3
    assert [a["segment_index"] for a in appends] == ["0", "1", "2"]
    # Every chunk is <= 4 MB and decodes back to the original byte count.
    sizes = [len(base64.b64decode(a["media_data"])) for a in appends]
    assert sizes == [FOUR_MB, FOUR_MB, 3 * 1024 * 1024]
    assert sum(sizes) == 11 * 1024 * 1024

    finalize = [p for p in session.posts if p["data"]["command"] == "FINALIZE"]
    assert len(finalize) == 1
    assert finalize[0]["data"]["media_id"] == "m1"


def test_video_status_polling_waits_until_succeeded(tmp_path, monkeypatch, no_sleep):
    """A pending video must be STATUS-polled until processing succeeds."""
    path = _write_file(tmp_path, "clip.mp4", 1024)
    session = _install_session(
        monkeypatch,
        [
            _Resp(payload={"media_id_string": "m1"}),
            _Resp(payload={}),  # append
            _Resp(payload={"processing_info": {"state": "pending", "check_after_secs": 2}}),
            _Resp(payload={"processing_info": {"state": "in_progress", "check_after_secs": 4}}),
            _Resp(payload={"processing_info": {"state": "succeeded"}}),
        ],
    )

    assert _make_client().upload_media(path) == "m1"

    assert len(session.gets) == 2
    assert [g["params"] for g in session.gets] == [
        {"command": "STATUS", "media_id": "m1"},
        {"command": "STATUS", "media_id": "m1"},
    ]
    # check_after_secs is honoured between polls.
    assert no_sleep == [2, 4]


def test_video_processing_failure_raises(tmp_path, monkeypatch, no_sleep):
    path = _write_file(tmp_path, "clip.mp4", 1024)
    _install_session(
        monkeypatch,
        [
            _Resp(payload={"media_id_string": "m1"}),
            _Resp(payload={}),
            _Resp(
                payload={
                    "processing_info": {
                        "state": "failed",
                        "error": {"code": 1, "name": "InvalidMedia", "message": "bad codec"},
                    }
                }
            ),
        ],
    )

    with pytest.raises(MediaUploadError, match="Video processing failed"):
        _make_client().upload_media(path)


def test_video_processing_timeout_raises(tmp_path, monkeypatch, no_sleep):
    path = _write_file(tmp_path, "clip.mp4", 1024)
    _install_session(
        monkeypatch,
        [
            _Resp(payload={"media_id_string": "m1"}),
            _Resp(payload={}),
            _Resp(payload={"processing_info": {"state": "pending", "check_after_secs": 3}}),
            # Next poll pushes us past the deadline.
            _Resp(payload={"processing_info": {"state": "pending", "check_after_secs": 3}}),
        ],
    )
    client = _make_client()
    client._VIDEO_PROCESS_TIMEOUT = 0  # already expired after the first poll

    with pytest.raises(MediaUploadError, match="timed out"):
        client.upload_media(path)


def test_video_synchronous_success_skips_status(tmp_path, monkeypatch, no_sleep):
    """If FINALIZE already reports success there is nothing to poll."""
    path = _write_file(tmp_path, "clip.mp4", 1024)
    session = _install_session(
        monkeypatch,
        [
            _Resp(payload={"media_id_string": "m1"}),
            _Resp(payload={}),
            _Resp(payload={"processing_info": {"state": "succeeded"}}),
        ],
    )

    assert _make_client().upload_media(path) == "m1"
    assert session.gets == []


def test_video_non_json_finalize_is_not_a_crash(tmp_path, monkeypatch, no_sleep):
    """A non-JSON FINALIZE body means 'no processing info', not a hard error."""
    path = _write_file(tmp_path, "clip.mp4", 1024)
    session = _install_session(
        monkeypatch,
        [
            _Resp(payload={"media_id_string": "m1"}),
            _Resp(payload={}),
            _Resp(text="<html>ok</html>"),
        ],
    )

    assert _make_client().upload_media(path) == "m1"
    assert session.gets == []


# â”€â”€ Guards â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def test_missing_file_raises():
    with pytest.raises(MediaUploadError, match="File not found"):
        _make_client().upload_media("does/not/exist.mp4")


def test_video_over_size_limit_raises(tmp_path):
    path = _write_file(tmp_path, "big.mp4", 1024)
    client = _make_client()
    client._MAX_VIDEO_SIZE = 512  # smaller than the file

    with pytest.raises(MediaUploadError, match="File too large"):
        client.upload_media(path)


def test_unsupported_video_container_raises(tmp_path):
    """A video/ file X cannot accept must fail before any network call."""
    path = _write_file(tmp_path, "clip.avi", 1024)
    with pytest.raises(MediaUploadError, match="Unsupported video format"):
        _make_client().upload_media(path)


def test_unsupported_image_format_raises(tmp_path):
    path = _write_file(tmp_path, "pic.bmp", 1024)
    with pytest.raises(MediaUploadError, match="Unsupported image format"):
        _make_client().upload_media(path)


def test_init_http_error_raises(tmp_path, monkeypatch):
    path = _write_file(tmp_path, "clip.mp4", 1024)
    _install_session(monkeypatch, [_Resp(status_code=500, text="boom")])

    with pytest.raises(MediaUploadError, match="INIT failed"):
        _make_client().upload_media(path)


def test_init_without_media_id_raises(tmp_path, monkeypatch):
    path = _write_file(tmp_path, "clip.mp4", 1024)
    _install_session(monkeypatch, [_Resp(payload={"media_id_string": ""})])

    with pytest.raises(MediaUploadError, match="did not return media_id"):
        _make_client().upload_media(path)


def test_append_failure_reports_segment(tmp_path, monkeypatch):
    path = _write_file(tmp_path, "clip.mp4", 5 * 1024 * 1024)
    _install_session(
        monkeypatch,
        [
            _Resp(payload={"media_id_string": "m1"}),
            _Resp(status_code=400, text="bad segment"),
        ],
    )

    with pytest.raises(MediaUploadError, match="APPEND segment 0 failed"):
        _make_client().upload_media(path)


def test_finalize_failure_raises(tmp_path, monkeypatch):
    path = _write_file(tmp_path, "clip.mp4", 1024)
    _install_session(
        monkeypatch,
        [
            _Resp(payload={"media_id_string": "m1"}),
            _Resp(payload={}),
            _Resp(status_code=503, text="unavailable"),
        ],
    )

    with pytest.raises(MediaUploadError, match="FINALIZE failed"):
        _make_client().upload_media(path)


def test_status_failure_raises(tmp_path, monkeypatch, no_sleep):
    path = _write_file(tmp_path, "clip.mp4", 1024)
    _install_session(
        monkeypatch,
        [
            _Resp(payload={"media_id_string": "m1"}),
            _Resp(payload={}),
            _Resp(payload={"processing_info": {"state": "pending", "check_after_secs": 1}}),
            _Resp(status_code=400, text="bad status"),
        ],
    )

    with pytest.raises(MediaUploadError, match="STATUS failed"):
        _make_client().upload_media(path)


# â”€â”€ Image path (regression) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def test_image_upload_single_segment(tmp_path, monkeypatch, no_sleep):
    """Images stay one APPEND segment with tweet_image category and no polling."""
    path = _write_file(tmp_path, "pic.png", 2048)
    session = _install_session(
        monkeypatch,
        [
            _Resp(payload={"media_id_string": "img1"}),
            _Resp(payload={}),
            _Resp(payload={}),
        ],
    )

    assert _make_client().upload_media(path) == "img1"

    assert session.posts[0]["data"]["media_category"] == "tweet_image"
    appends = [p["data"] for p in session.posts if p["data"]["command"] == "APPEND"]
    assert len(appends) == 1
    assert len(base64.b64decode(appends[0]["media_data"])) == 2048
    assert session.gets == []


# â”€â”€ CLI wiring (--video on post/reply/quote) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


class _StubClient:
    """Records upload_media calls; satisfies _upload_images' duck-typing."""

    def __init__(self):
        self.uploaded = []

    def upload_media(self, path):
        self.uploaded.append(path)
        return "media-%d" % len(self.uploaded)


def test_cli_rejects_images_and_video_together():
    from twitter_cli import cli

    with pytest.raises(click.UsageError, match="images OR a video, not both"):
        cli._upload_images(_StubClient(), ("a.png",), ("v.mp4",), rich_output=False)


def test_cli_rejects_more_than_one_video():
    from twitter_cli import cli

    with pytest.raises(click.UsageError, match="Too many videos"):
        cli._upload_images(_StubClient(), (), ("a.mp4", "b.mp4"), rich_output=False)


def test_cli_rejects_more_than_four_images():
    from twitter_cli import cli

    with pytest.raises(click.UsageError, match="Too many images"):
        cli._upload_images(
            _StubClient(), ("1.png", "2.png", "3.png", "4.png", "5.png"), (), rich_output=False
        )


def test_cli_uploads_single_video():
    from twitter_cli import cli

    client = _StubClient()
    media_ids = cli._upload_images(client, (), ("clip.mp4",), rich_output=False)

    assert media_ids == ["media-1"]
    assert client.uploaded == ["clip.mp4"]

