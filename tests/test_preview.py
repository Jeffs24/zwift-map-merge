from fastapi.testclient import TestClient

from zwift_map_merge.config import Config
from zwift_map_merge.merge import merge_track
from zwift_map_merge.preview import summarize
from zwift_map_merge.server import create_app

from fitbuilder import watch_fit, zwift_fit

START = 1_100_000_000


def test_summary_of_merged_file():
    merged, _ = merge_track(watch_fit(START, 120), zwift_fit(START, 120))
    pv = summarize(merged)
    assert pv.records == 120 and pv.records_with_position == 120
    lat, lon = pv.track[0]
    assert -11 < lat < -10.5 and 166 < lon < 167  # Watopia, Solomon Islands
    assert "Heart rate (bpm)" in pv.series
    assert pv.stats["GPS points"] == "120 / 120"
    assert pv.stats["Laps"] == "1"


def test_preview_page(tmp_path):
    cfg = Config.from_env()
    cfg.data_dir = tmp_path
    cfg.app_password = cfg.garmin_email = cfg.garmin_password = None
    with TestClient(create_app(cfg)) as c:
        store = c.app.state.service.store
        store.upsert_detected(1, 2, "Treadmill Run", "Zwift - Watopia", "2026-11-20T12:00:00+00:00", 0.97)
        r = c.get("/pairs/1/preview")
        assert "Simulate" in r.text and "first" in r.text  # nothing merged yet
        folder = tmp_path / "backups" / "1"
        folder.mkdir(parents=True)
        merged, _ = merge_track(watch_fit(START, 120), zwift_fit(START, 120))
        (folder / "1_merged.fit").write_bytes(merged)
        r = c.get("/pairs/1/preview")
        assert r.status_code == 200
        assert "L.polyline" in r.text and "Heart rate" in r.text
        assert "will delete the watch activity #1 and the Zwift copy #2" in r.text
        assert c.get("/pairs/99/preview").status_code == 404
