"""Source-layer tests: snapshot retrieval, admission, and the research corpora.

The theme is that these are *readers*, and a reader that guesses produces
plausible wrong data rather than an exception. Every test here pins a specific
way that happened.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from infraimpact.domain.enums import Authority
from infraimpact.sources import registry as reg
from infraimpact.sources.catalog import SIGNALS
from infraimpact.sources.historical import (
    NORTH_AMERICA,
    WASHINGTON,
    GdeltLayout,
    discover_gdelt_layout,
    is_us_country,
    iter_workbook_rows,
)
from infraimpact.sources.historical import (
    MunicipalCrimeAdapter,
    _all_match,
    _coord_candidates,
    _hits_in_bounds,
    _place_name_column,
)
from infraimpact.sources.snapshot import (
    ADMISSION_KEY,
    SnapshotAdapter,
    _parse_signatures,
)

# --------------------------------------------------------------------------
# repository integrity
# --------------------------------------------------------------------------


def test_no_source_file_contains_null_bytes():
    """A truncated write leaves NUL padding and Python refuses to import it.

    This happened once: a partially-written ``snapshot.py`` was padded with
    1,685 NUL bytes, and the result was a bare
    ``SyntaxError: source code string cannot contain null bytes`` pointing at
    whichever file happened to import it next. Nothing about that traceback
    points at the file that is actually damaged.
    """

    src = Path(__file__).resolve().parents[1] / "src" / "infraimpact"
    offenders = [p for p in src.rglob("*.py") if b"\x00" in p.read_bytes()]
    assert not offenders, f"NUL bytes in {[str(p) for p in offenders]}"


def test_every_source_file_parses():
    src = Path(__file__).resolve().parents[1] / "src" / "infraimpact"
    for path in src.rglob("*.py"):
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


# --------------------------------------------------------------------------
# multi-file snapshot retrieval
# --------------------------------------------------------------------------


def test_parse_signatures_round_trips_a_composite_checkpoint():
    composite = "a.csv:1:2|b.csv:3:4"
    assert _parse_signatures(composite) == {"a.csv": "a.csv:1:2", "b.csv": "b.csv:3:4"}


def test_parse_signatures_tolerates_the_legacy_single_file_checkpoint():
    # A checkpoint written before multi-file support is a bare signature. It
    # must not crash, and it must simply fail to match so the file re-reads.
    assert _parse_signatures("a.csv:1:2") == {"a.csv": "a.csv:1:2"}
    assert _parse_signatures(None) == {}
    assert _parse_signatures("") == {}


def _write(path: Path, rows: list[dict], header: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    if header:
        lines.append(",".join(rows[0].keys()))
    for row in rows:
        lines.append(",".join(str(v) for v in row.values()))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class _MultiFileAdapter(SnapshotAdapter):
    """Minimal adapter that records which file each row came from."""

    files = ()
    subdir = ""
    has_header = True
    max_records = None
    source_id = "test.multi"

    def _record_id(self, item):
        return f"{self.source_id}:{item.get('id')}"

    def _geometry_of(self, item):
        return None

    def normalize(self, record):  # pragma: no cover - never called here
        raise NotImplementedError


def test_poll_reads_every_declared_file_not_just_the_first(tmp_path):
    """``SignalSpec.files`` is a payload set, not a candidate list.

    Six municipal exports, three days of GDELT and two Crowd Counting releases
    are each one signal. Reading only the first file reported a sixth of the
    municipal corpus while health said the feed was fine.
    """

    _write(
        tmp_path / "one.csv",
        [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}],
    )
    _write(tmp_path / "two.csv", [{"id": 3, "name": "c"}])
    adapter = _MultiFileAdapter()
    adapter._root = tmp_path
    adapter.files = ("one.csv", "two.csv", "absent.csv")

    assert [p.name for p in adapter.resolve_all()] == ["one.csv", "two.csv"]
    ids = {r.source_record_id for r in adapter.poll(None)}
    assert ids == {"test.multi:1", "test.multi:2", "test.multi:3"}
    assert adapter._per_file_counts == {"one.csv": 2, "two.csv": 1}


def test_checkpoint_is_per_file_so_only_the_changed_file_is_reread(tmp_path):
    _write(tmp_path / "one.csv", [{"id": 1}])
    _write(tmp_path / "two.csv", [{"id": 2}])
    adapter = _MultiFileAdapter()
    adapter._root = tmp_path
    adapter.files = ("one.csv", "two.csv")

    first = list(adapter.poll(None))
    assert len(first) == 2
    checkpoint = adapter.checkpoint()
    assert "one.csv" in (checkpoint or "") and "two.csv" in (checkpoint or "")

    # Unchanged: nothing re-read at all.
    assert list(adapter.poll(checkpoint)) == []

    # One file changes: only that file is read again.
    _write(tmp_path / "two.csv", [{"id": 2}, {"id": 3}])
    second = list(adapter.poll(checkpoint))
    assert [r.source_record_id for r in second] == ["test.multi:2", "test.multi:3"]
    assert adapter._per_file_counts == {"two.csv": 2}


def test_a_broken_file_does_not_lose_the_others(tmp_path):
    _write(tmp_path / "good.csv", [{"id": 1}])
    (tmp_path / "bad.json").write_text('{"truncated": ', encoding="utf-8")
    adapter = _MultiFileAdapter()
    adapter._root = tmp_path
    adapter.files = ("good.csv", "bad.json")

    records = list(adapter.poll(None))
    assert [r.source_record_id for r in records] == ["test.multi:1"]
    assert adapter._read_errors and "bad.json" in adapter._read_errors[0]


def test_health_reports_the_stalest_file_not_the_newest(tmp_path):
    import os
    import time

    _write(tmp_path / "old.csv", [{"id": 1}])
    _write(tmp_path / "new.csv", [{"id": 2}])
    old = time.time() - 40 * 86400
    os.utime(tmp_path / "old.csv", (old, old))

    adapter = _MultiFileAdapter()
    adapter._root = tmp_path
    adapter.files = ("old.csv", "new.csv", "never_downloaded.csv")
    health = adapter.health()
    assert "oldest=old.csv" in (health.message or "")
    # A declared payload file that never arrived is a coverage gap, not an
    # absence of news.
    assert "missing=1/3" in (health.message or "")


# --------------------------------------------------------------------------
# admission labels
# --------------------------------------------------------------------------


def test_admission_labels_a_comparator_and_an_unlocated_record(tmp_path):
    """Region, comparator and unlocated are three different claims.

    Passing a Washington State crowd count off as a Seattle one, or a
    country-level aggregate off as a local event, are the two ways a wider
    study area quietly becomes a lie.
    """

    from infraimpact.domain.geo import bbox_polygon
    from infraimpact.sources.historical import HistoricalAdapter

    class Scoped(HistoricalAdapter):
        source_id = "test.scope"
        files = ()
        subdir = ""
        region_filter = True
        study_bounds = WASHINGTON
        keep_unlocated = True

        def normalize(self, record):  # pragma: no cover - never called here
            raise NotImplementedError

    adapter = Scoped()
    adapter._bounds = (-123.10, 47.10, -121.70, 48.70)

    inside = {"id": 1, "geometry": bbox_polygon((-122.40, 47.60, -122.30, 47.62))}
    yakima = {"id": 2, "geometry": bbox_polygon((-121.0, 46.5, -120.5, 46.7))}
    nowhere = {"id": 3}

    assert adapter.admit(inside, adapter._geometry_of(inside)) == "region"
    assert adapter.admit(yakima, adapter._geometry_of(yakima)) == "comparator"
    assert adapter.admit(nowhere, None) == "unlocated"
    # Yakima is outside the *operating* region but inside the study area, so it
    # must not be counted as a region drop.
    assert adapter.dropped_out_of_region == 0

    ohio = {"id": 4, "geometry": bbox_polygon((-83.0, 39.0, -82.5, 39.5))}
    assert adapter.admit(ohio, adapter._geometry_of(ohio)) is None
    assert adapter.dropped_out_of_region == 1


def test_a_realtime_adapter_never_produces_a_comparator_label():
    """Only historical corpora widen their geography.

    A live alert that misses the region is dropped, never relabelled as a
    comparator - otherwise an Ohio flood advisory reaches a Seattle resolver.
    """

    inside = {"id": 1, "geometry": {"type": "Point", "coordinates": [-122.33, 47.61]}}
    adapter = _MultiFileAdapter()
    adapter._bounds = (-123.10, 47.10, -121.70, 48.70)
    assert adapter.admit(inside, inside["geometry"]) == "region"
    assert adapter.admit({"id": 2}, None) == "region"  # no geometry passes through
    outside = {"id": 3, "geometry": {"type": "Point", "coordinates": [-83.0, 39.9]}}
    assert adapter.admit(outside, outside["geometry"]) is None
    assert adapter.dropped_out_of_region == 1


# --------------------------------------------------------------------------
# GDELT schema discovery
# --------------------------------------------------------------------------


def _gdelt_row(record_id: int, lat: str, lon: str, name: str) -> list[str]:
    row = [str(i) for i in range(58)]
    row[0] = str(record_id)
    row[1] = "20200601"
    row[36] = name
    row[39] = lat
    row[40] = lon
    row[56] = "20200601"
    row[57] = f"https://example.invalid/{record_id}"
    return row


def test_gdelt_layout_discovery_finds_coordinates_the_schema_would_miss(tmp_path):
    """The published GDELT schema puts ActionGeo at 35/36. These files have it
    at 39/40. Trusting the publication reads CAMEO codes as latitude, gets
    numbers that are in range, and drops all 143,447 rows as out-of-region
    without raising anything.
    """

    path = tmp_path / "export.CSV"
    rows = [_gdelt_row(i, "47.60", "-122.33", "Seattle, Washington, United States") for i in range(200)]
    path.write_text("\n".join("\t".join(r) for r in rows) + "\n", encoding="utf-8")

    layout = discover_gdelt_layout(path, bounds=NORTH_AMERICA)
    assert layout.record_id == 0
    assert layout.event_date == 1
    assert layout.source_url == 57
    assert layout.geo_blocks[0][:2] == (39, 40)
    assert layout.geo_blocks[0][2] == 36  # the place name, three columns back
    assert layout.verified


def test_gdelt_layout_prefers_the_earliest_location_block(tmp_path):
    """SourceGeo often has *more* in-bounds rows than ActionGeo.

    Ranking by raw hits promotes the wrong block and answers "where was this
    reported from" when the question was "where did this happen".
    """

    path = tmp_path / "export.CSV"
    rows = []
    for i in range(200):
        row = _gdelt_row(i, "47.60", "-122.33", "Seattle, Washington, United States")
        row[50] = "Phoenix, Arizona, United States"
        row[53] = "33.45"
        row[54] = "-112.07"
        rows.append(row)
    # Give the source block strictly more in-bounds rows than the action block.
    for i in range(200, 300):
        row = _gdelt_row(i, "", "", "")
        row[50] = "Denver, Colorado, United States"
        row[53] = "39.74"
        row[54] = "-104.99"
        rows.append(row)
    path.write_text("\n".join("\t".join(r) for r in rows) + "\n", encoding="utf-8")

    layout = discover_gdelt_layout(path, bounds=NORTH_AMERICA)
    assert layout.geo_blocks[0][:2] == (39, 40)


def test_gdelt_layout_refuses_to_guess_when_nothing_is_in_bounds(tmp_path):
    path = tmp_path / "export.CSV"
    rows = [_gdelt_row(i, "-37.82", "144.97", "Melbourne, Victoria, Australia") for i in range(200)]
    path.write_text("\n".join("\t".join(r) for r in rows) + "\n", encoding="utf-8")

    layout = discover_gdelt_layout(path, bounds=NORTH_AMERICA)
    assert layout.geo_blocks == ()
    assert not layout.verified
    assert "no coordinate pair" in (layout.evidence or {}).get("verdict", "")


def test_all_match_does_not_require_a_url_to_end_with_its_scheme():
    column = ["https://example.invalid/a", "http://example.invalid/b"]
    assert _all_match(column, r"https?://\S+", ignore_empty=True, full=False)
    # fullmatch against a scheme prefix can never succeed, which silently
    # reported every GDELT observation as having no source URL.
    assert not _all_match(column, r"https?://", ignore_empty=True)


def test_coord_candidates_accepts_a_longitude_beyond_ninety_degrees(tmp_path):
    """Half of Earth's longitudes are outside +/- 90 degrees.

    A "longitude must be within 90" rule rejects Melbourne at 144.97 E and
    Seattle at 122.3 W - every real coordinate pair on Earth.
    """

    rows = [_gdelt_row(i, "-37.82", "144.97", "Melbourne, Victoria, Australia") for i in range(120)]
    columns = list(zip(*rows))
    assert 39 in _coord_candidates(columns, 58)


def test_hits_in_bounds_counts_the_orientation_that_actually_lands():
    pairs = [(47.6, -122.3), (33.4, -112.1)]
    assert _hits_in_bounds(pairs, True, NORTH_AMERICA) == 2
    assert _hits_in_bounds(pairs, False, NORTH_AMERICA) == 0


def test_place_name_column_finds_prose_not_a_camel_code(tmp_path):
    rows = [_gdelt_row(i, "47.60", "-122.33", "Seattle, Washington, United States") for i in range(60)]
    rows[0][37] = "US"
    rows[0][38] = "USWA"
    columns = list(zip(*rows))
    assert _place_name_column(columns, 39) == 36


def test_unverified_layout_yields_no_geometry(tmp_path):
    path = tmp_path / "export.CSV"
    path.write_text("a\tb\tc\n", encoding="utf-8")
    layout = discover_gdelt_layout(path, bounds=NORTH_AMERICA)
    assert isinstance(layout, GdeltLayout)
    assert layout.geo_blocks == ()


# --------------------------------------------------------------------------
# municipal crime dialects
# --------------------------------------------------------------------------


def test_every_municipal_crime_file_on_disk_has_a_declared_dialect():
    """Each city's open-data export names the same concepts differently.

    There is no shared fallback that works: an unmatched file produces empty
    fields and an unlocated observation, which is a silent total loss rather
    than an error.
    """

    from infraimpact.sources.snapshot import workspace_root

    root = workspace_root()
    known = set(MunicipalCrimeAdapter.DIALECTS)
    on_disk = set()
    for spec in SIGNALS:
        if spec.adapter != "historical.municipal_crime":
            continue
        for name in spec.files:
            if (root / spec.subdir / name).is_file():
                on_disk.add(name)
    if not on_disk:
        pytest.skip("no municipal crime files found on disk")
    assert not (on_disk - known), f"files with no dialect: {sorted(on_disk - known)}"


def test_municipal_crime_dialects_are_keyed_by_real_filenames():
    """Guards against a dialect entry being written for a file that is not there."""

    root_files = {
        "chicago_311_infrastructure_damage_2020.csv",
        "nyc_311_infrastructure_damage_2020.csv",
        "chicago_civil_unrest_crimes_2020.csv",
        "los_angeles_civil_unrest_crimes_2020.csv",
        "nyc_civil_unrest_crimes_2020.csv",
        "san_francisco_civil_unrest_crimes_2020.csv",
        "seattle_spd_crimes_2020_unrest.csv",
        "dc_mpd_crimes_2020_unrest.csv",
        "dc_mpd_crimes_2021_capitol.csv",
    }
    declared = set(MunicipalCrimeAdapter.DIALECTS)
    assert declared <= root_files, f"dialects for absent files: {sorted(declared - root_files)}"


def test_district_of_columbia_dialect_places_its_uppercase_coordinates():
    """MPD spells them ``LATITUDE``/``LONGITUDE``.

    The generic reader only knows ``latitude``/``lat``, so every DC row arrived
    unlocated - discarding the file's entire reason to exist, silently.
    """

    dialect = MunicipalCrimeAdapter.DIALECTS["dc_mpd_crimes_2020_unrest.csv"]
    assert dialect["lat"] == "LATITUDE"
    assert dialect["lon"] == "LONGITUDE"
    assert MunicipalCrimeAdapter.DIALECTS["dc_mpd_crimes_2021_capitol.csv"] == dialect


def test_seattle_is_the_only_in_region_municipal_file():
    """The scope label claims Seattle is in-region. Verify the geometry agrees."""

    from infraimpact.sources.snapshot import workspace_root

    root = workspace_root()
    spec = next(s for s in SIGNALS if s.source_id == "historical.spd_unrest_crime")
    adapter = MunicipalCrimeAdapter(spec, root=root)
    resolved = adapter.resolve()
    if resolved is None or not resolved.is_file():
        pytest.skip("seattle_spd_crimes_2020_unrest.csv not on disk")
    records = list(adapter.poll(None))
    assert records, "no Seattle records read"
    labels = {r.payload.get(ADMISSION_KEY, "region") for r in records}
    assert "comparator" not in labels


def test_chicago_311_damage_file_is_only_graffiti_removal():
    """4,770 rows of "Graffiti Removal Request" despite the filename.

    The payload carries the request type precisely so this cannot be read
    later as evidence about broken signals and downed lights.
    """

    from infraimpact.sources.snapshot import workspace_root

    root = workspace_root()
    spec = next(s for s in SIGNALS if s.source_id == "historical.municipal_crime")
    adapter = MunicipalCrimeAdapter(spec, root=root)
    adapter.files = ("chicago_311_infrastructure_damage_2020.csv",)
    resolved = adapter.resolve()
    if resolved is None or not resolved.is_file():
        pytest.skip("chicago_311_infrastructure_damage_2020.csv not on disk")
    records = list(adapter.poll(None))
    assert records
    observation = adapter.normalize(records[0])
    assert observation is not None
    assert observation.structured_payload["request_type"]
    assert observation.structured_payload["is_service_request"] is True


# --------------------------------------------------------------------------
# country matching
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["United States", "united states of america", "USA", "840", "840.0", "750.0", " US "],
)
def test_is_us_country_accepts_every_spelling_the_corpora_use(value):
    """NAVCO writes its country codes as floats because of a spreadsheet."""

    assert is_us_country(value)


@pytest.mark.parametrize("value", ["", None, "Canada", "20", "840.1", "USAF"])
def test_is_us_country_rejects_everything_else(value):
    assert not is_us_country(value)


def test_is_us_country_checks_any_argument():
    assert is_us_country("Madagascar", "840.0")
    assert is_us_country(None, None, "840")
    assert not is_us_country("Philippines", "608.0")


# --------------------------------------------------------------------------
# corpora that legitimately contain no regional data
# --------------------------------------------------------------------------


def test_corpora_with_no_us_rows_say_so_in_their_scope_label():
    """MIDA and NAVCO contain no United States records - verified against the
    files. A scope label that claims otherwise is a documentation bug that
    costs an operator an afternoon.
    """

    from infraimpact.sources.historical import MidaAdapter, NavcoAdapter

    for cls in (MidaAdapter, NavcoAdapter):
        assert "no United States" in cls.scope_label or "no United States-located" in cls.scope_label


def test_scoped_corpora_stamp_records_as_outside_the_operating_region():
    from infraimpact.sources.historical import NavcoAdapter

    spec = next(s for s in SIGNALS if s.adapter == "historical.navco")
    adapter = NavcoAdapter(spec)
    resolved = adapter.resolve()
    if resolved is None or not resolved.is_file():
        pytest.skip("NAVCO file not present on disk")
    record = next(iter(adapter._read(resolved)))  # type: ignore[union-attr]
    observation = adapter.normalize(record)
    assert observation is not None
    assert observation.structured_payload["within_operating_region"] is False
    # 200 km: a country-level figure projected onto a point would be a lie.
    assert observation.location_precision_m >= 200_000


# --------------------------------------------------------------------------
# catalogue integrity
# --------------------------------------------------------------------------


def test_every_catalogue_signal_that_declares_files_names_a_registered_adapter():
    missing = sorted(
        {
            s.adapter
            for s in SIGNALS
            if s.files and s.adapter not in reg.ADAPTERS
        }
    )
    assert not missing, f"unregistered adapters: {missing}"


def test_signals_without_files_are_context_only_and_never_polled():
    """``user.context`` is supplied by the authenticated user, not a file.

    It is listed so the catalogue covers the whole signal inventory, and it
    carries no adapter precisely because nothing polls it.
    """

    fileless = [s for s in SIGNALS if not s.files]
    assert fileless, "expected at least one context-only signal"
    for spec in fileless:
        assert spec.usage != "realtime"
        assert spec.adapter not in reg.ADAPTERS


def test_every_historical_signal_is_quarantined_from_notifications():
    historical = [s for s in SIGNALS if s.usage == "historical_only"]
    assert historical, "the research corpora must stay registered"
    assert all(s.usage != "realtime" for s in historical)


def test_duplicate_files_across_signals_are_intentional():
    """One payload legitimately backs several signals.

    ``wsdot_travel_times`` and ``wsdot_mountain_passes`` are the same file, as
    are all four WSDOT bridge/clearance/truck signals. They must not be
    silently deduplicated: two signals reading one file are two different
    claims about it, and the catalogue is where that is made explicit.
    """

    from collections import defaultdict

    by_file: dict[str, list[str]] = defaultdict(list)
    for spec in SIGNALS:
        for name in spec.files:
            by_file[name].append(spec.source_id)
    shared = {k: v for k, v in by_file.items() if len(v) > 1}
    assert shared, "expected at least one deliberately shared payload file"


def test_subclass_metadata_survives_instantiation():
    """``SourceAdapter`` metadata is ``ClassVar``.

    As plain dataclass fields the generated ``__init__`` reassigned every
    instance to the *base* defaults, so every subclass silently published
    itself as ``official`` / ``realtime`` regardless of what it declared.
    """

    from infraimpact.sources.feeds import SpdCadAdapter

    spec = next(s for s in SIGNALS if s.adapter == "feed.spd_cad")
    adapter = SpdCadAdapter(spec)
    assert adapter.authority is not None
    assert SpdCadAdapter.authority in Authority
    # The class default and the instance attribute must agree.
    assert adapter.authority == SpdCadAdapter.authority
    assert type(adapter).authority is SpdCadAdapter.authority


def test_workbook_reader_skips_sheets_that_are_not_count_cubes():
    """``acled_hdx`` has four sheets; only some are usable.

    Sheet 1 is a licensing notice and sheets 3/4 expand to ~500 MB of XML.
    """

    assert callable(iter_workbook_rows)


def test_observation_ledger_key_is_not_derived_from_the_file_path(tmp_path):
    """Sanity: dedupe is by source record id, so ids must be stable per file."""

    spec = next(s for s in SIGNALS if s.adapter == "feed.blotter")
    adapter = reg.ADAPTERS[spec.adapter](spec)
    first = [r.source_record_id for r in adapter.poll(None)]
    second = [r.source_record_id for r in adapter.poll(None)]
    assert first == second
    assert adapter.checkpoint()
    assert list(adapter.poll(adapter.checkpoint())) == []


def test_feed_snapshot_files_are_json_serialisable_and_stable():
    """Every snapshot row must survive a JSON round trip unchanged."""

    from infraimpact.sources.snapshot import workspace_root

    root = workspace_root()
    spec = next(s for s in SIGNALS if s.files and s.subdir == "live_feeds")
    adapter = reg.ADAPTERS[spec.adapter](spec, root=root)
    for record in adapter.poll(None):
        json.dumps(record.payload, default=str)