import io
from datetime import datetime, timedelta, timezone

import pytest

from zwift_map_merge import fitfile as ff
from zwift_map_merge.zwift import (ZwiftActivity, ZwiftAuthRequired, ZwiftClient, ZwiftError,
                                   parse_activity)

from fitbuilder import zwift_fit
from test_service import GMT, START_FIT, START_UTC, make


class Resp:
    def __init__(self, status=200, body=None, content=b""):
        self.status_code = status
        self._body = body
        self.content = content

    def iter_content(self, size):
        buf = io.BytesIO(self.content)
        while chunk := buf.read(size):
            yield chunk

    def json(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeSession:
    def __init__(self, pages, fit=b"FIT", token_status=200):
        self.pages = pages
        self.fit = fit
        self.token_status = token_status
        self.posts, self.gets = [], []

    def post(self, url, data=None, **kw):
        self.posts.append(data)
        if self.token_status != 200:
            return Resp(self.token_status, {"error": "invalid_grant"})
        return Resp(200, {"access_token": "AT", "refresh_token": "RT", "expires_in": 86400000})

    def get(self, url, params=None, headers=None, **kw):
        self.gets.append((url, params, headers))
        if url.endswith("/api/profiles/me"):
            return Resp(200, {"id": 42})
        if "/activities" in url and "amazonaws" not in url:
            i = (params or {}).get("start", 0) // 30
            return Resp(200, self.pages[i] if i < len(self.pages) else [])
        if "amazonaws" in url:
            return Resp(200, content=self.fit)
        return Resp(404)


def raw(aid, start, bucket="s3-fit-prd-uswest2-zwift", key="prod/42/abc"):
    return {"id_str": str(aid), "name": f"Zwift - Run {aid}", "startDate": start,
            "sport": "RUNNING", "fitFileBucket": bucket, "fitFileKey": key}


def test_login_stores_tokens_not_password(tmp_path):
    s = FakeSession([])
    c = ZwiftClient(tmp_path / "z.json", s)
    c.login("me@example.com", "hunter2")
    text = (tmp_path / "z.json").read_text()
    assert "RT" in text and "hunter2" not in text
    assert s.posts[0]["grant_type"] == "password"
    # expires_in is in milliseconds: the token must be valid for about a day, not 1000 days
    again = ZwiftClient(tmp_path / "z.json", s)
    assert again.connected()
    assert again._tokens["expires_at"] < datetime.now().timestamp() + 2 * 86400


def test_rejected_login(tmp_path):
    c = ZwiftClient(tmp_path / "z.json", FakeSession([], token_status=401))
    with pytest.raises(ZwiftAuthRequired):
        c.login("me@example.com", "bad")
    assert not c.connected()


def test_history_pages_until_older_than_since(tmp_path):
    pages = [
        [raw(i, f"2026-11-{20 - i:02d}T12:00:00.000+0000") for i in range(30)][:19]
        + [raw(100 + i, "2025-12-01T12:00:00.000+0000") for i in range(11)],
        [raw(200, "2025-01-01T12:00:00Z")],
    ]
    s = FakeSession(pages)
    c = ZwiftClient(tmp_path / "z.json", s)
    c.login("a@b.c", "p")
    got = list(c.history(datetime(2026, 11, 1, tzinfo=timezone.utc), pause_s=0))
    assert [a.id for a in got] == [str(i) for i in range(19)]
    # the second page was read (first page had recent items), then paging stopped
    assert sum(1 for u, *_ in s.gets if u.endswith("/activities")) == 2


def test_download_validates_bucket(tmp_path):
    c = ZwiftClient(tmp_path / "z.json", FakeSession([], fit=b"DATA"))
    c.login("a@b.c", "p")
    ok = ZwiftActivity("1", "x", None, "RUNNING", "s3-fit-prd-uswest2-zwift", "prod/1")
    assert c.download_fit(ok) == b"DATA"
    bad = ZwiftActivity("1", "x", None, "RUNNING", "evil.com/x?", "k")
    with pytest.raises(ZwiftError):
        c.download_fit(bad)


def test_parse_activity_dates():
    a = parse_activity(raw(1, "2026-11-20T12:00:00.000+0000"))
    assert a.start == datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
    assert parse_activity({"id": 5, "startDate": 1763640000000}).start.year == 2025


class FakeZwift:
    def __init__(self, acts):
        self.acts = acts
        self.downloads = []

    def history(self, since):
        return [a for a in self.acts if a.start >= since]

    def download_fit(self, a):
        self.downloads.append(a.id)
        return zwift_fit(START_FIT, 60)


def test_sync_history_pairs_and_skips(tmp_path):
    svc, g, store = make(tmp_path)
    g.activities = lambda days: [a for a in type(g).activities(g, days) if a["activityId"] == 1]
    old = START_UTC - timedelta(days=30)
    zw = FakeZwift([
        ZwiftActivity("z1", "Zwift - Watopia", START_UTC, "RUNNING", "b", "k"),
        ZwiftActivity("z2", "Zwift - London", old, "RUNNING", "b", "k"),  # no watch activity then
    ])
    counts = svc.sync_zwift(zw, START_UTC - timedelta(days=60))
    assert counts["paired"] == 1 and counts["downloaded"] == 1
    assert zw.downloads == ["z1"]  # z2 never downloaded
    row = store.get(1)
    assert row["source_file"] and row["source_name"].startswith("Zwift:")
    # second run: nothing new to download
    again = svc.sync_zwift(zw, START_UTC - timedelta(days=60))
    assert again["downloaded"] == 0 and zw.downloads == ["z1"]
    # merging uses the downloaded file and deletes only the watch original
    assert svc.merge(1) == 101
    assert g.deleted == [1]
    assert ff.parse(g.imported[0])


def test_sync_keeps_pair_found_by_scan(tmp_path):
    svc, g, store = make(tmp_path)
    svc.scan()  # pair 1 + Garmin Zwift copy 2
    zw = FakeZwift([ZwiftActivity("z1", "Zwift", START_UTC, "RUNNING", "b", "k")])
    svc.sync_zwift(zw, START_UTC - timedelta(days=1))
    assert store.get(1)["source_id"] == 2  # still deletes the Garmin copy on merge
    assert GMT
