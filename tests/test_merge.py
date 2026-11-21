import pytest

from zwift_map_merge import fitfile as ff
from zwift_map_merge.merge import MergeError, extract_track, manufacturer, merge_track

from fitbuilder import watch_fit, zwift_fit

START = 1_100_000_000  # FIT seconds


def records(data: bytes):
    return list(ff.parse(data).messages(ff.MESG_RECORD))


def test_roundtrip_is_lossless():
    data = watch_fit(START, 30)
    assert ff.encode(ff.parse(data)) == data


def test_bad_crc_is_rejected():
    data = bytearray(watch_fit(START, 5))
    data[20] ^= 0xFF
    with pytest.raises(ff.FitError):
        ff.parse(bytes(data))


def test_merge_adds_positions_and_keeps_watch_data():
    watch = watch_fit(START, 60)
    zwift = zwift_fit(START + 10, 40)  # Zwift started 10 s later and stopped earlier
    merged, stats = merge_track(watch, zwift)

    assert stats.target_records == 60
    assert stats.with_position == 40
    recs = records(merged)
    original = records(watch)
    for before, after in zip(original, recs):
        assert after.timestamp == before.timestamp
        assert after.get(3) == before.get(3)  # heart rate
        assert after.get(4) == before.get(4)  # cadence
        assert after.raw[-2:] == before.raw[-2:]  # developer field untouched
    with_pos = [r for r in recs if r.get(0) is not None]
    assert len(with_pos) == 40
    first = with_pos[0]
    assert first.timestamp == START + 10
    assert first.get(0) == -127_000_000 and first.get(1) == 1_990_000_000
    assert manufacturer(ff.parse(merged)) == 1  # still the watch


def test_merge_fills_lap_start_and_end():
    merged, _ = merge_track(watch_fit(START, 20), zwift_fit(START, 20))
    lap = next(ff.parse(merged).messages(ff.MESG_LAP))
    assert lap.get(3) == -127_000_000
    assert lap.get(5) == -127_000_000 + 19 * 100


def test_tolerance_matches_clock_offset():
    _, strict = merge_track(watch_fit(START, 20), zwift_fit(START + 3, 20), tolerance_s=0)
    assert strict.with_position == 17
    _, loose = merge_track(watch_fit(START, 20), zwift_fit(START + 1000, 20), tolerance_s=2)
    assert loose.with_position == 0


def test_altitude_copied_on_request():
    merged, stats = merge_track(watch_fit(START, 10), zwift_fit(START, 10), copy_altitude=True)
    assert stats.with_altitude == 10
    rec = records(merged)[0]
    assert rec.get(78) / 5 - 500 == pytest.approx(20)


def test_big_endian_target():
    merged, stats = merge_track(watch_fit(START, 10, big_endian=True), zwift_fit(START, 10))
    assert stats.with_position == 10
    assert records(merged)[0].get(0) == -127_000_000


def test_compressed_timestamp_records():
    watch = watch_fit(START, 40, compressed_every=3)
    assert ff.encode(ff.parse(watch)) == watch
    merged, stats = merge_track(watch, zwift_fit(START, 40))
    assert stats.with_position == 40
    assert len(extract_track(ff.parse(merged))) == 40


def test_source_without_gps_is_refused():
    with pytest.raises(MergeError):
        merge_track(watch_fit(START, 10), watch_fit(START, 10))


def test_merged_file_gets_a_new_time_created_but_same_device():
    watch = watch_fit(START, 10)
    merged, _ = merge_track(watch, zwift_fit(START, 10))
    before = next(ff.parse(watch).messages(ff.MESG_FILE_ID))
    after = next(ff.parse(merged).messages(ff.MESG_FILE_ID))
    assert after.get(4) == before.get(4) + 1  # time_created
    assert after.get(1) == before.get(1)  # manufacturer
    assert after.get(0) == before.get(0)  # file type
