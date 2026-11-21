from datetime import datetime, timezone

from zwift_map_merge import fitfile as ff
from zwift_map_merge.config import Config
from zwift_map_merge.garmin import DuplicateActivity
from zwift_map_merge.merge import extract_track
from zwift_map_merge.service import Service
from zwift_map_merge.store import Store

from fitbuilder import watch_fit, zwift_fit

START_FIT = 1_100_000_000
START_UTC = datetime.fromtimestamp(ff.unix_from_fit(START_FIT), timezone.utc)
GMT = START_UTC.strftime("%Y-%m-%d %H:%M:%S")


class FakeGarmin:
    """Stateful stand-in for GarminSession: activities really disappear when deleted."""

    def __init__(self, fail_import_merged=False, duplicate_while_original=False):
        self.files = {1: watch_fit(START_FIT, 60), 2: zwift_fit(START_FIT, 60)}
        self.alive = {1, 2}
        self.deleted, self.imported, self.restored = [], [], []
        self.fail_import_merged = fail_import_merged
        self.duplicate_while_original = duplicate_while_original

    def activities(self, days):
        acts = [
            {"activityId": 1, "activityName": "Treadmill", "startTimeGMT": GMT, "duration": 60,
             "hasPolyline": False, "manufacturer": "GARMIN", "activityType": {"typeKey": "treadmill_running"}},
            {"activityId": 2, "activityName": "Zwift - Watopia", "startTimeGMT": GMT, "duration": 60,
             "hasPolyline": True, "manufacturer": "ZWIFT", "activityType": {"typeKey": "virtual_run"}},
        ]
        return [a for a in acts if a["activityId"] in self.alive]

    def activities_between(self, start, end):
        return self.activities(0)

    def exists(self, aid, start):
        return aid in self.alive

    def original_fit(self, aid):
        if aid not in self.alive:
            raise RuntimeError("404")
        return self.files[aid], self.files[aid]

    def activity(self, aid):
        if aid not in self.alive:
            raise RuntimeError("404")
        return {"activityName": "Treadmill", "description": "notes",
                "activityTypeDTO": {"typeId": 18, "typeKey": "treadmill_running", "parentTypeId": 1}}

    def gear_uuids(self, aid):
        return ["shoe-uuid"]

    def delete(self, aid, start):
        self.deleted.append(aid)
        self.alive.discard(aid)

    def import_fit(self, path, start, exclude):
        if self.duplicate_while_original and 1 in self.alive:
            raise DuplicateActivity("Activity already exists (duplicate)")
        if self.fail_import_merged and path.name.endswith("_merged.fit"):
            raise RuntimeError("500 Server Error")
        self.imported.append(path.read_bytes())
        new_id = 100 + len(self.imported)
        self.alive.add(new_id)
        return new_id

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
    # imported first, originals deleted only afterwards
    assert g.deleted == [1, 2]
    assert extract_track(ff.parse(g.imported[0]))
    aid, kw = g.restored[0]
    assert aid == 101 and kw["name"] == "Treadmill" and kw["gear"] == ["shoe-uuid"]
    row = store.get(1)
    assert row["status"] == "merged" and row["new_id"] == 101
    # merged pair and the new activity are never paired again
    assert {1, 2, 101} <= store.handled_ids()
    assert svc.scan() == 0


def test_failed_import_deletes_nothing(tmp_path):
    svc, g, store = make(tmp_path, fail_import_merged=True)
    svc.scan()
    assert svc.merge(1) is None
    assert g.deleted == [] and g.alive == {1, 2}
    row = store.get(1)
    assert row["status"] == "failed" and "Nothing was deleted" in row["message"]


def test_duplicate_deletes_original_first_then_imports(tmp_path):
    svc, g, store = make(tmp_path, duplicate_while_original=True)
    svc.scan()
    assert svc.merge(1) == 101
    assert g.deleted == [1, 2]
    assert extract_track(ff.parse(g.imported[0]))
    assert store.get(1)["status"] == "merged"


def test_duplicate_then_failed_import_reimports_original(tmp_path):
    svc, g, store = make(tmp_path, duplicate_while_original=True, fail_import_merged=True)
    svc.scan()
    assert svc.merge(1) is None
    assert g.imported == [g.files[1]]  # the original is back
    assert 2 in g.alive  # Zwift copy kept
    row = store.get(1)
    assert row["status"] == "failed" and "re-imported" in row["message"]


def test_merge_again_recovers_an_activity_lost_by_an_earlier_attempt(tmp_path):
    svc, g, store = make(tmp_path)
    svc.scan()
    svc.simulate(1)
    g.delete(1, None)  # what the 504 did: watch activity gone, nothing imported
    store.set_status(1, "failed", "Delete on Garmin failed: API Error 504.")
    g.deleted.clear()
    assert svc.merge(1) == 101
    merged = ff.parse(g.imported[0])
    assert extract_track(merged)
    assert g.deleted == [2]  # only the Zwift copy was left to delete
    assert store.get(1)["status"] == "merged"


def test_merge_all(tmp_path):
    svc, g, store = make(tmp_path)
    svc.scan()
    assert svc.merge_all() == 1
    assert store.get(1)["status"] == "merged"


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
