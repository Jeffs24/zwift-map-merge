from fastapi.testclient import TestClient

from zwift_map_merge.config import Config
from zwift_map_merge.server import create_app


def client(tmp_path, password=None):
    cfg = Config.from_env()
    cfg.data_dir = tmp_path
    cfg.app_password = password
    cfg.garmin_email = cfg.garmin_password = None
    return TestClient(create_app(cfg))


def test_login_page_when_not_connected(tmp_path):
    with client(tmp_path) as c:
        r = c.get("/")
        assert r.status_code == 200
        assert "Garmin Connect login" in r.text
        assert c.get("/health").json() == {"ok": True, "garmin": False}


def test_dashboard_lists_pairs(tmp_path):
    with client(tmp_path) as c:
        (tmp_path / "garminconnect").mkdir()
        (tmp_path / "garminconnect" / "garmin_tokens.json").write_text("{}")
        store = c.app.state.service.store
        store.upsert_detected(1, 2, "Treadmill <b>", "Zwift - Watopia", "2026-11-20T12:00:00+00:00", 0.97)
        r = c.get("/")
        assert "Zwift - Watopia" in r.text
        assert "Treadmill &lt;b&gt;" in r.text  # escaped
        assert "/pairs/1/merge" in r.text


def test_password_protects_ui(tmp_path):
    with client(tmp_path, password="secret") as c:
        assert c.get("/").status_code == 401
        assert c.get("/", auth=("x", "secret")).status_code == 200
        assert c.get("/health").status_code == 200
