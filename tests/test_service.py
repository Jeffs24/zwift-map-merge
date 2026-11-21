from datetime import datetime, timezone

from zwift_map_merge import fitfile as ff
from zwift_map_merge.config import Config
from zwift_map_merge.merge import extract_track
from zwift_map_merge.service import Service
from zwift_map_merge.store import Store

from fitbuilder import watch_fit, zwift_fit

START_FIT = 1_100_000_000
START_UTC = datetime.fromtimestamp(ff.unix_from_fit(START_FIT), timezone.utc)
GMT = START_UTC.strftime("%Y-%m-%d %H:%M:%S")


class FakeGarmin:
    def __init__(self, fail_import_merged=False):
        self.files = {1: watch_fit(START_FIT, 60), 2: zwift_fit(START_FIT, 60)}
        self.deleted, self.imported, self.restored = [], [], []
        self.fail_import_merged = fail_import_merged

    def activities(self, days):
        return [
            {"activityId": 1, "activityName": "Treadmill", "startTimeGMT": GMT, "duration": 60,
             "hasPolyline": False, "manufacturer": "GARMIN", "activityType": {"typeKey": "treadmill_running"}},
            {"activityId": 2, "activityName": "Zwift - Watopia", "startTimeGMT": GMT, "duration": 60,
             "hasPolyline": True, "manufacturer": "ZWIFT", "activityType": {"typeKey": "virtual_run"}},
        ]

    def activities_between(self, start, end):
        return self.activities(0)

    def original_fit(self, aid):
        return self.files[aid], self.files[aid]

    def activity(self, aid):
        return {"activityName": "Treadmill", "description": "notes",
                "activityTypeDTO": {"typeId": 18, "typeKey": "treadmill_running", "parentTypeId": 1}}

    def gear_uuids(self, aid):
        return ["shoe-uuid"]

    def delete(self, aid):
        self.deleted.append(aid)

    def import_fit(self, path, start, exclude):
        if self.fail_import_merged and path.name.endswith("_merged.fit"):
            raise RuntimeError("409 duplicate")
        self.imported.append(path.read_bytes())
        return 100 + len(self.imported)

    def restore(self, aid, **kw):
        self.restored.append((aid, kw))
        return []


def make(tmp_path, **garmin_kw):
    cfg = Config.from_env()
    cfg.data_dir = tmp_path
    g = FakeGarmin(**garmin_kw)
    store = Store(cfg.db_path)
    return Service(cfg, g, store), g, store


def test_scan_then_simulate_changes_nothing(tmp_path):
    svc, g, store = make(tmp_path)
    assert svc.scan() == 1
    prep = svc.simulate(1)
    assert store.get(1)["status"] == "simulated"
    assert g.deleted == [] and g.imported == []
    assert extract_track(ff.parse(prep.merged_path.read_bytes()))


def test_merge_deletes_imports_and_restores(tmp_path):
    svc, g, store = make(tmp_path)
    svc.scan()
    new_id = svc.merge(1)
    assert new_id == 101
    assert g.deleted == [2, 1]
    assert extract_track(ff.parse(g.imported[0]))
    aid, kw = g.restored[0]
    assert aid == 101 and kw["name"] == "Treadmill" and kw["gear"] == ["shoe-uuid"]
    row = store.get(1)
    assert row["status"] == "merged" and row["new_id"] == 101
    # merged pair and the new activity are never paired again
    assert {1, 2, 101} <= store.handled_ids()
    assert svc.scan() == 0


def test_failed_import_reimports_watch_original(tmp_path):
    svc, g, store = make(tmp_path, fail_import_merged=True)
    svc.scan()
    assert svc.merge(1) is None
    assert g.imported == [g.files[1]]
    row = store.get(1)
    assert row["status"] == "failed" and "re-imported" in row["message"]


def test_source_with_wrong_manufacturer_is_refused(tmp_path):
    svc, g, store = make(tmp_path)
    g.files[2] = watch_fit(START_FIT, 60)  # Garmin-made file posing as source
    svc.scan()
    assert svc.merge(1) is None
    assert g.deleted == []
    assert store.get(1)["status"] == "failed"


def test_uploaded_zwift_file_when_copy_was_deleted(tmp_path):
    import gzip

    svc, g, store = make(tmp_path)
    zwift_bytes = g.files.pop(2)
    g.activities = lambda days: FakeGarmin.activities(g, days)[:1]  # Zwift copy gone from Garmin
    msg = svc.add_source_file("zwift.fit.gz", gzip.compress(zwift_bytes))
    assert "Treadmill" in msg
    row = store.get(1)
    assert row["source_id"] == 0 and row["source_file"]
    assert svc.merge(1) == 101
    assert g.deleted == [1]  # only the watch original; nothing else on Garmin
    assert extract_track(ff.parse(g.imported[0]))
    # a later scan does not bring the pair back
    assert svc.scan() == 0
    assert "already handled" in svc.add_source_file("zwift.fit", zwift_bytes) or \
        "no watch activity" in svc.add_source_file("zwift.fit", zwift_bytes)


def test_uploaded_non_zwift_file_is_refused(tmp_path):
    svc, g, store = make(tmp_path)
    assert "not a Zwift file" in svc.add_source_file("watch.fit", g.files[1])
    assert store.all() == []
