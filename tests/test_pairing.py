from zwift_map_merge.pairing import find_pairs


def act(aid, name, start, duration, polyline, manufacturer="GARMIN", type_key="running"):
    return {"activityId": aid, "activityName": name, "startTimeGMT": start, "duration": duration,
            "hasPolyline": polyline, "manufacturer": manufacturer, "activityType": {"typeKey": type_key}}


def test_pairs_watch_and_zwift():
    acts = [
        act(1, "Treadmill Run", "2026-11-20 12:00:00", 3600, False),
        act(2, "Zwift - Watopia", "2026-11-20 12:01:30", 3500, True, "ZWIFT", "virtual_run"),
        act(3, "Morning Run", "2026-11-19 07:00:00", 1800, True),  # outdoor, has GPS
    ]
    pairs = find_pairs(acts)
    assert [(p.target.id, p.source.id) for p in pairs] == [(1, 2)]
    assert pairs[0].overlap > 0.95


def test_zwift_detected_by_name_when_manufacturer_missing():
    acts = [
        act(1, "Treadmill", "2026-11-20 12:00:00", 3600, False),
        act(2, "Zwift - London", "2026-11-20 12:00:10", 3600, True, ""),
    ]
    assert len(find_pairs(acts)) == 1


def test_no_pair_without_overlap_or_when_excluded():
    acts = [
        act(1, "Treadmill", "2026-11-20 12:00:00", 600, False),
        act(2, "Zwift", "2026-11-20 14:00:00", 600, True, "ZWIFT"),
    ]
    assert find_pairs(acts) == []
    acts[1]["startTimeGMT"] = "2026-11-20 12:00:00"
    assert find_pairs(acts, exclude_ids={1}) == []


def test_each_activity_used_once():
    acts = [
        act(1, "Treadmill A", "2026-11-20 12:00:00", 3600, False),
        act(4, "Treadmill B", "2026-11-20 12:05:00", 3000, False),
        act(2, "Zwift", "2026-11-20 12:00:00", 3600, True, "ZWIFT"),
    ]
    pairs = find_pairs(acts)
    assert len(pairs) == 1 and pairs[0].target.id == 1
