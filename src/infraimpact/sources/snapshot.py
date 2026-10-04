"""Snapshot-backed source adapters (brief section 4).

Every signal in ``data/live_feeds`` was captured as a JSON or GeoJSON file on
disk rather than served from an endpoint. This module turns that convention
into a first-class adapter contract so the runtime loop cannot tell the
difference between a replayed snapshot and a live HTTP poll.

Three behaviours matter here, and each exists because the data demands it:

**Duplicate suppression.** Several snapshot filenames are byte-identical
copies of one another. Adapters are constructed from a
:class:`~infraimpact.sources.catalog.SignalSpec` and read only the first file
that exists, so a duplicate never double-counts into the ledger.

**Region filtering.** Some feeds are far larger than the region - the NWS
alert snapshot is nationwide, USGS is global, WSDOT is statewide. Records
whose geometry falls outside the region bounding box are dropped at the
adapter boundary, so a flood advisory for South Carolina never reaches the
resolver and can never be resolved into a Seattle event.

**Change-based polling.** The checkpoint is a signature of the backing files
(``mtime`` and size). A file that has not changed emits nothing, so the loop
stays cheap when re-reading a static snapshot while still picking up a file
that a collector refreshes underneath it.

**Format tolerance.** Snapshots are JSON, GeoJSON, CSV, tab-separated CSV, and
occasionally gzipped. ``.gz`` is decompressed transparently and GDELT's
``.CSV`` files are recognised as tab-delimited by their adapter, because three
of the captured files carry a misleading extension and reading them with the
wrong dialect silently produces one giant column instead of failing.
"""

from __future__ import annotations

import csv
import gzip
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from ..config import workspace_root as _workspace_root
from ..domain.enums import Authority, HealthState, ObservationType, SourceType
from ..domain.geo import bbox_of
from ..domain.ids import ensure_utc, parse_time, utcnow
from ..domain.schemas import Observation
from .adapter import RawRecord, SourceAdapter
from .catalog import SignalSpec

log = logging.getLogger(__name__)

#: Location precision used when a record carries no geometry at all. Large on
#: purpose: a neighbourhood-only permit must never look like a point fix.
UNLOCATED_PRECISION_M = 5000.0


#: Backwards-compatible alias. The path helper moved to :mod:`infraimpact.config`
#: when the LLM adapter needed the same root; it is re-exported here because
#: ``snapshot`` is where every catalogue ``subdir`` is resolved and importing it
#: from here reads correctly at the call sites that use it.
workspace_root = _workspace_root


#: Backwards-compatible alias. The raw observation store still lives under
#: ``data/`` even though the live snapshots do not.
def data_root() -> Path:
    return workspace_root() / "data"


#: Private payload key carrying the admission label from
#: :meth:`SnapshotAdapter.admit` into normalisation. Stripped before the record
#: reaches the ledger; it exists only so ``normalize`` knows whether a point is
#: inside the operating region or in a wider study area.
ADMISSION_KEY = "_admission"


def _inside(box: tuple[float, float, float, float], bounds: tuple[float, float, float, float]) -> bool:
    """Does a ``(min_lon, min_lat, max_lon, max_lat)`` box intersect the bounds?"""

    min_lon, min_lat, max_lon, max_lat = bounds
    return not (box[2] < min_lon or box[0] > max_lon or box[3] < min_lat or box[1] > max_lat)


def _parse_signatures(checkpoint: str | None) -> dict[str, str]:
    """Split a composite checkpoint back into ``filename -> signature``.

    A checkpoint written by an older single-file build is a bare
    ``name:mtime:size`` string with no separator; :meth:`SnapshotAdapter.poll`
    still handles it, it simply finds nothing matching and re-reads, which is
    the correct outcome for a changed detection scheme.
    """

    if not checkpoint:
        return {}
    out: dict[str, str] = {}
    for part in checkpoint.split("|"):
        name, sep, rest = part.partition(":")
        if sep:
            out[name] = part
    return out


def _as_float(value: Any) -> float | None:
    """Coerce feed numerics, which arrive as strings surprisingly often."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    f = _as_float(value)
    return int(f) if f is not None else None


#: Timestamps below this are treated as seconds, above it as milliseconds.
#: Both appear in the snapshot set: WSDOT and USGS send epoch milliseconds,
#: some transit payloads send epoch seconds as strings.
_MILLISECOND_THRESHOLD = 100_000_000_000


def parse_feed_time(value: Any) -> datetime | None:
    """Parse whichever timestamp dialect a snapshot happens to use.

    Five appear across the captured feeds and all of them are real:

    ==========================  =========================================
    Shape                       Seen in
    ==========================  =========================================
    ``"2026-10-03T19:55:00Z"``  NWS alerts, NWS observations, SDOT permits
    ``1779137846837`` (int)     WSDOT ``LastModifiedDate``, USGS ``time``
    ``"1791058261000"`` (str)   OneBusAway ``lastUpdateTime``
    ``"/Date(1791058295000-0700)/"``  Washington State Ferries
    ``"Tue, 29 Sep 2026 21:33:27 +0000"``  RSS ``pub_date``
    ==========================  =========================================

    Returns ``None`` for anything unrecognised rather than guessing, so an
    unparseable timestamp becomes "unknown" downstream instead of a fabricated
    one.
    """

    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return ensure_utc(value)

    # WSF .NET JSON dates: /Date(epoch_ms ±hhmm)/
    if isinstance(value, str) and value.startswith("/Date("):
        match = re.search(r"/Date\((-?\d+)", value)
        return _from_epoch_ms(int(match.group(1))) if match else None

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _from_epoch_ms(int(value))

    text = str(value).strip()
    if re.fullmatch(r"-?\d{11,}", text):
        return _from_epoch_ms(int(text))
    if re.fullmatch(r"-?\d{1,10}", text):
        return _from_epoch_seconds(int(text))

    # RSS ``pub_date`` is RFC 2822, which ``parse_time`` does not cover.
    try:
        return ensure_utc(datetime.strptime(text, "%a, %d %b %Y %H:%M:%S %z"))
    except ValueError:
        pass

    return parse_time(text)


def _from_epoch_ms(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000.0, tz=UTC)


def _from_epoch_seconds(value: int) -> datetime:
    return datetime.fromtimestamp(value, tz=UTC)


def geometry_from(value: Any) -> dict[str, Any] | None:
    """Coerce a feed geometry field into a GeoJSON geometry dict.

    Feeds are inconsistent: some nest ``{"type":..., "coordinates":...}``,
    some use a bare ``[lon, lat]`` pair, some wrap it as ``PointCoordinate``.
    Everything funnels through here so the rest of the pipeline only ever sees
    real geometry.
    """

    if not isinstance(value, dict):
        return None
    gtype = value.get("type")
    coords = value.get("coordinates")
    if gtype and coords is not None:
        if gtype == "Feature" and isinstance(value.get("geometry"), dict):
            return geometry_from(value["geometry"])
        if gtype == "FeatureCollection" and isinstance(value.get("features"), list):
            for feat in value["features"]:
                inner = geometry_from(feat)
                if inner:
                    return inner
            return None
        if gtype in {"Point", "LineString", "MultiPoint"}:
            return {"type": gtype, "coordinates": coords}
        if gtype in {"Polygon", "MultiLineString", "MultiPolygon"}:
            return {"type": gtype, "coordinates": coords}
        return None
    pair = value.get("PointCoordinate")
    if isinstance(pair, (list, tuple)) and len(pair) >= 2:
        # SDOT ships PointCoordinate as [lat, lon], not GeoJSON's [lon, lat].
        # Verified against sdot_traffic_cameras.json: Fauntleroy Way SW comes
        # back as [47.5267, -122.3927], so the *first* element is latitude.
        lat, lon = _as_float(pair[0]), _as_float(pair[1])
        if lon is not None and lat is not None:
            return {"type": "Point", "coordinates": [lon, lat]}
    return None


def point_from_latlon(lat: Any, lon: Any) -> dict[str, Any] | None:
    flat_lat, flat_lon = _as_float(lat), _as_float(lon)
    if flat_lat is None or flat_lon is None:
        return None
    # Reject the 0/0 sentinel agencies use for "location unknown".
    if abs(flat_lat) < 1e-6 and abs(flat_lon) < 1e-6:
        return None
    return {"type": "Point", "coordinates": [flat_lon, flat_lat]}


@dataclass
class SnapshotAdapter(SourceAdapter):
    """Base adapter for a signal backed by files under the data root.

    Subclasses set the class attributes below and implement :meth:`extract`
    (file payload -> record dicts) and :meth:`normalize` (one record -> an
    observation). Everything else - file resolution, duplicate suppression,
    region filtering, checkpointing, health - is handled here.
    """

    #: Filenames to try under ``subdir``, most canonical first.
    files: tuple[str, ...] = ()
    #: Directory under the workspace root holding ``files``. Live snapshots
    #: sit at ``live_feeds``; datasets sit under ``data/``.
    subdir: str = "live_feeds"
    #: Drop records whose geometry lies entirely outside the region bounds.
    region_filter: bool = True
    #: Location precision to assume when a record has no geometry.
    default_precision_m: float = 500.0
    #: Cap on records emitted per file, so a 1,000-row snapshot cannot
    #: monopolise a poll cycle. ``None`` means no cap.
    max_records: int | None = 2000
    #: Field delimiter for delimited files. ``None`` means *infer from the file's
    #: extension*, which is the only correct default for a signal whose payload
    #: set spans two dialects. GDELT exports carry a ``.CSV`` extension but are
    #: tab-separated, which is the case an explicit override exists for.
    csv_delimiter: str | None = None
    #: Flatten GeoJSON ``Feature`` objects into their properties plus geometry
    #: before normalisation, so subclasses see one consistent record shape.
    flatten_features: bool = True
    #: ``False`` for exports that carry no header row (GDELT). Positional keys
    #: ``col_0``..``col_n`` are used instead of column names.
    has_header: bool = True

    def __init__(
        self,
        spec: SignalSpec | None = None,
        *,
        root: Path | None = None,
        region_bounds: tuple[float, float, float, float] | None = None,
    ) -> None:
        super().__init__()
        self.spec = spec
        if spec is not None:
            # The catalogue owns the identity. An adapter that guessed its own
            # source_id would write observations the ledger could not attribute
            # to a signal, or would collide with the live connector of the same
            # signal.
            self.source_id = spec.source_id
            self.files = tuple(spec.files)
            self.subdir = spec.subdir
            self.reliability = spec.reliability
            self.expected_interval_s = spec.interval_s
            self.stale_after_s = spec.stale_after_s
            self.usage = spec.usage
            if spec.requires_key:
                self.requires_key = spec.requires_key
        self._root = root
        self._bounds = region_bounds
        self._signature: str | None = None
        #: Records dropped because their geometry lies outside the region.
        #: Tracked because "the feed returned nothing" and "the feed returned
        #: 71 statewide alerts of which none are ours" are different facts, and
        #: only one of them is a coverage gap.
        self.dropped_out_of_region = 0
        #: Records discarded by a corpus-specific relevance rule - a country
        #: filter, a study-area bound - as opposed to the region test. Realtime
        #: adapters never move this counter.
        self.dropped_out_of_scope = 0
        #: Records emitted by the most recent read. The budget check is against
        #: this, not against rows scanned, so a nationwide file cannot spend the
        #: whole per-poll allowance on records that are about to be discarded.
        self._emitted = 0
        #: Set when the per-poll record budget ran out before end of file, so
        #: health can say "truncated" rather than implying the file ended.
        self._budget_exhausted = False
        #: The file currently being read. Adapters whose payload set spans
        #: several *different* schemas (the six municipal crime exports) need to
        #: know which one they are looking at, and resolving that from
        #: ``resolve()`` would name the first file for all six.
        self._current_path: Path | None = None
        #: ``filename -> records emitted`` for the files read this poll.
        self._per_file_counts: dict[str, int] = {}
        #: ``filename -> error`` for files that failed to parse.
        self._read_errors: list[str] = []

    # -- configuration ----------------------------------------------------

    @property
    def root(self) -> Path:
        """Workspace root. ``subdir`` is resolved relative to this."""

        return self._root or workspace_root()

    @property
    def bounds(self) -> tuple[float, float, float, float] | None:
        if self._bounds is not None:
            return self._bounds
        try:
            from ..config import get_region

            return get_region().bounds
        except Exception:  # noqa: BLE001 - region is optional for standalone use
            return None

    def path_for(self, filename: str) -> Path | None:
        candidate = self.root / self.subdir / filename
        return candidate if candidate.is_file() else None

    def delimiter_for(self, path: Path) -> str:
        """Delimiter to parse ``path`` with.

        A signal's payload set can legitimately contain two dialects: the
        Crowd Counting Consortium ships ``ccc_phase3_public_2025_present.csv``
        (74 comma-separated columns) alongside ``ccc_compiled_20172020.tab`` and
        ``ccc_compiled_20212024.tab`` (62 and 72 tab-separated ones), and all
        three are one signal. A single adapter-level delimiter therefore
        parses one dialect correctly and the other two as *one column per row* -
        which does not raise, it just leaves every field lookup returning
        ``None``. The result is 165 MB of corpus silently contributing nothing
        while the feed reports healthy.

        An explicit :attr:`csv_delimiter` still wins, for the case where the
        extension lies (GDELT's ``.CSV``).
        """

        if self.csv_delimiter:
            return self.csv_delimiter
        return "\t" if _effective_suffix(path) in {".tab", ".tsv"} else ","

    def resolve(self) -> Path | None:
        """First existing backing file, or ``None`` when none are present."""

        found = self.resolve_all()
        return found[0] if found else None

    def resolve_all(self) -> list[Path]:
        """**Every** existing backing file, in declared order.

        ``SignalSpec.files`` is the payload set, not a candidate list. Six
        municipal open-data exports, three days of GDELT, three geographies of
        Google Mobility and two Crowd Counting Consortium releases are each one
        signal, and reading only the first file silently reports one sixth of
        the municipal corpus while health says the feed is fine. Returning the
        first match was the wrong abstraction for a snapshot adapter.

        Order is preserved so record ids stay stable between polls.
        """

        return [p for p in (self.path_for(name) for name in self.files) if p is not None]

    def available(self) -> bool:
        """A snapshot signal is available when at least one declared file exists.

        A method, not a property, for two reasons. The base contract in
        :mod:`infraimpact.sources.adapter` declares it as a method and every
        caller invokes it, so overriding it as a property leaves
        ``SnapshotAdapter`` (and the ~35 catalogue adapters built on it) with a
        ``bool`` where ``unavailable_reason`` expects a callable - which fails
        only when a credential-gated source is checked, so the gap hides behind
        the sources that happen not to need a key. And the check hits the
        filesystem: as a property, merely inspecting an adapter in a debugger or
        a ``--json`` dump would stat every declared file.

        Note the asymmetry with :meth:`unavailable_reason`: this says whether the
        payload can be read *now*; that says whether the connector could ever run
        in this deployment. A snapshot with no file on disk is available-but-empty
        for the first and "no backing file" for the second, which is the
        distinction that keeps a dropped export from reading as a missing
        credential.
        """

        return bool(self.resolve_all())

    def checkpoint(self) -> str | None:
        return self._signature

    # -- reading ----------------------------------------------------------

    def poll(self, checkpoint: str | None) -> Iterable[RawRecord]:
        """Read every declared file that has changed since ``checkpoint``.

        A signal's payload is a *set* of files, so the checkpoint is a set of
        per-file signatures and the comparison is per file rather than for the
        whole signal. That is what lets a six-file municipal corpus pick up one
        newly-dropped export without re-reading the other five, and what keeps a
        three-day GDELT signal from being read as if it were one file.

        One bad file does not lose the others: a read error is recorded against
        that file and the remaining files are still emitted.
        """

        paths = self.resolve_all()
        if not paths:
            self._message = f"no backing file for {self.source_id} (tried {list(self.files)})"
            return []

        previous = _parse_signatures(checkpoint)
        signatures: dict[str, str] = {}
        for path in paths:
            stat = path.stat()
            signatures[path.name] = f"{path.name}:{stat.st_mtime_ns}:{stat.st_size}"
        self._signature = "|".join(signatures[name] for name in signatures)

        stale = [p for p in paths if previous.get(p.name) != signatures[p.name]]
        self.dropped_out_of_region = 0
        self.dropped_out_of_scope = 0
        self._emitted = 0
        self._budget_exhausted = False
        self._per_file_counts = {}
        self._read_errors = []
        if not stale:
            # Unchanged snapshot(s). Nothing new to observe; the ledger already
            # holds these records from the previous cycle.
            return []

        records: list[RawRecord] = []
        for path in stale:
            # The record budget is per file. A shared budget would let the first
            # file in declaration order consume the whole allowance and silently
            # starve the rest of the payload set.
            self._emitted = 0
            self._current_path = path
            try:
                found = list(self._read(path))
            except Exception as exc:  # noqa: BLE001 - a bad file must not kill the loop
                self._read_errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
                log.warning("adapter %s could not read %s: %s", self.source_id, path.name, exc)
                continue
            self._per_file_counts[path.name] = len(found)
            records.extend(found)
        self._current_path = None

        self.record_success(len(records), 0.0)
        self._message = self._telemetry(records)
        return records

    def _telemetry(self, records: Sequence[RawRecord]) -> str | None:
        """Per-poll message so health explains *why* a feed is quiet."""

        parts: list[str] = []
        names = [p.name for p in self.resolve_all()]
        label = names[0] if len(names) == 1 else f"{len(names)} files"
        parts.append(f"{label} kept={len(records)}")
        if len(names) > 1:
            detail = " ".join(
                f"{name}={count}" for name, count in sorted(self._per_file_counts.items())
            )
            if detail:
                parts.append(f"per_file[{detail}]")
            absent = [n for n in self.files if n not in names]
            if absent:
                # A declared payload file that is not on disk is a coverage gap,
                # not an absence of news. Saying so is the difference between an
                # operator filing a ticket and the platform looking merely quiet.
                parts.append(f"declared_but_absent={len(absent)}:{','.join(absent[:4])}")
        if self.dropped_out_of_region:
            parts.append(f"dropped_out_of_region={self.dropped_out_of_region}")
        for err in self._read_errors:
            parts.append(f"read_error[{err}]")
        extra = self._scope_telemetry()
        if extra:
            parts.append(extra)
        if self._budget_exhausted and self.max_records is not None:
            parts.append(f"truncated_at_budget={self.max_records}/file")
        return " ".join(parts) if len(parts) > 1 or extra else None

    def _scope_telemetry(self) -> str | None:
        """Extra health detail from adapters that scope more than one region."""

        return None

    def _read(self, path: Path) -> Iterable[RawRecord]:
        suffix = _effective_suffix(path)
        if suffix in {".csv", ".tab", ".tsv"}:
            yield from self._read_csv(path)
        else:
            yield from self._read_json(path)

    @staticmethod
    def _open_text(path: Path):
        """Open a snapshot, transparently decompressing ``.gz``.

        Three files in the corpus ship gzipped - ``Google_Mobility_County_Daily.csv.gz``,
        the GDELT ZIP members - and each is a byte-for-byte duplicate of its
        uncompressed twin, so reading them must give identical records rather
        than a second, differently-shaped copy of the same dataset.
        """

        if path.suffix.lower() == ".gz":
            return gzip.open(path, "rt", encoding="utf-8", errors="replace", newline="")
        return path.open("r", encoding="utf-8", errors="replace", newline="")

    def _read_json(self, path: Path) -> Iterable[RawRecord]:
        with self._open_text(path) as handle:
            doc = json.load(handle)
        # A GeoJSON FeatureCollection flattens automatically; every other
        # envelope shape is the adapter's business via ``extract``.
        if (
            self.flatten_features
            and isinstance(doc, dict)
            and doc.get("type") == "FeatureCollection"
        ):
            items = flatten_features(doc, "features", "Features")
        else:
            items = self.extract(doc)
        for item in items:
            if not isinstance(item, dict):
                continue
            if self.max_records is not None and self._emitted >= self.max_records:
                self._budget_exhausted = True
                return
            geometry = self._geometry_of(item)
            admission = self.admit(item, geometry)
            if admission is None:
                continue
            record_id = self._record_id(item)
            if not record_id:
                continue
            if admission != "region":
                item = {**item, ADMISSION_KEY: admission}
            self._emitted += 1
            yield RawRecord(
                source_record_id=record_id,
                payload=item,
                event_time=self._event_time(item),
                observed_at=self._observed_at(item),
                source_url=self._source_url(item),
            )

    def _read_csv(self, path: Path) -> Iterable[RawRecord]:
        for row in self.iter_rows(path):
            if self.max_records is not None and self._emitted >= self.max_records:
                # Budget spent. Truncating *before* the region filter would let a
                # nationwide file spend the whole budget on records that are then
                # discarded, so the budget is applied to what is actually emitted.
                self._budget_exhausted = True
                return
            geometry = self._geometry_of(row)
            admission = self.admit(row, geometry)
            if admission is None:
                continue
            record_id = self._record_id(row)
            if not record_id:
                continue
            if admission != "region":
                row = {**row, ADMISSION_KEY: admission}
            self._emitted += 1
            yield RawRecord(
                source_record_id=record_id,
                payload=row,
                event_time=self._event_time(row),
                observed_at=self._observed_at(row),
                source_url=self._source_url(row),
            )

    def iter_rows(self, path: Path) -> Iterator[dict[str, Any]]:
        """Stream delimited rows from one file without normalising them.

        Used by the historical corpora, whose files are far too large to
        materialise (the ACLED workbook expands to ~500 MB of sheet XML) and
        whose rows are read once, in order, to build aggregates.

        A ``has_header = False`` adapter gets positional ``col_0``..``col_n``
        keys instead. GDELT's export has no header row at all, so a
        ``DictReader`` would take its first *record* as the header and shift
        every field by one - a failure that produces plausible-looking wrong
        data rather than an error.
        """

        with self._open_text(path) as handle:
            reader = csv.reader(handle, delimiter=self.delimiter_for(path))
            if self.has_header:
                header = next(reader, None)
                if header is None:
                    return
                names = [h.strip() for h in header]
                for row in reader:
                    yield {k: v for k, v in zip(names, row) if k}
            else:
                for row in reader:
                    if row:
                        yield {f"col_{i}": v for i, v in enumerate(row)}

    # -- subclass hooks ---------------------------------------------------

    def extract(self, doc: Any) -> list[dict[str, Any]]:
        """Flatten a decoded file payload into plain record dicts."""

        raise NotImplementedError

    def normalize(self, record: RawRecord) -> Observation | None:
        raise NotImplementedError

    # -- helpers for subclasses ------------------------------------------

    def _geometry_of(self, item: dict[str, Any]) -> dict[str, Any] | None:
        for key in ("geometry", "line_string", "report_location", "PointCoordinate", "location"):
            value = item.get(key)
            if isinstance(value, dict):
                found = geometry_from(value)
                if found:
                    return found
        return point_from_latlon(item.get("latitude") or item.get("lat"), item.get("longitude") or item.get("lon") or item.get("long"))

    def _in_region(self, geometry: dict[str, Any] | None) -> bool:
        """Keep geometry-free records; drop geometry that misses the region.

        A record with no coordinates is not necessarily irrelevant - a news
        headline or a regional service bulletin legitimately has none - so it
        passes through and is marked low-precision downstream. A record that
        *does* place itself outside the region is dropped: letting a South
        Carolina flood advisory into a Seattle resolver is how an event ends
        up resolved to the wrong place.
        """

        if not self.region_filter or geometry is None:
            return True
        bounds = self.bounds
        if bounds is None:
            return True
        box = bbox_of(geometry)
        if box is None:
            return True
        return _inside(box, bounds)

    def admit(self, item: dict[str, Any], geometry: dict[str, Any] | None) -> str | None:
        """Gate one row. ``None`` drops it; a label says why it was kept.

        Three labels are in use:

        ``"region"``       inside the operating region - the normal case.
        ``"comparator"``   outside it but inside a documented wider study area.
        ``"unlocated"``    no geometry at all, kept because it is an area- or
                           country-level aggregate rather than a point.

        The last two only exist for historical corpora, and they exist precisely
        so that a *baseline* can be built from records the platform will never
        treat as local events. The realtime default is a plain region test, and
        it never produces anything but ``"region"`` or a drop.
        """

        if self._in_region(geometry):
            return "region"
        self.dropped_out_of_region += 1
        return None

    def _record_id(self, item: dict[str, Any]) -> str:
        raise NotImplementedError

    def _event_time(self, item: dict[str, Any]) -> Any:
        return None

    def _observed_at(self, item: dict[str, Any]) -> Any:
        return self._event_time(item) or utcnow()

    def _source_url(self, item: dict[str, Any]) -> str | None:
        value = item.get("link") or item.get("url") or item.get("@id")
        return str(value) if isinstance(value, str) else None

    def _precision(self, geometry: dict[str, Any] | None) -> float:
        if geometry is None:
            return UNLOCATED_PRECISION_M
        return self.default_precision_m

    # -- shared normalisation --------------------------------------------

    def observation(
        self,
        record: RawRecord,
        *,
        item: dict[str, Any],
        geometry: dict[str, Any] | None,
        observation_type: ObservationType,
        headline: str,
        structured_payload: dict[str, Any] | None = None,
        authority: Authority | None = None,
        source_type: SourceType | None = None,
        precision_m: float | None = None,
        keep_raw: bool = True,
    ) -> Observation:
        """Build an observation with the shared bookkeeping filled in.

        ``keep_raw`` stores the originating payload for audit. It is on by
        default because the observation ledger has to be able to show where a
        fact came from, but adapters for very large or very noisy feeds turn
        it off to bound storage.
        """

        payload = dict(structured_payload or {})
        if keep_raw:
            payload["raw"] = item
        return self.build_observation(
            record,
            observation_type=observation_type,
            geometry=geometry,
            headline=headline[:300],
            structured_payload=payload,
            location_precision_m=precision_m if precision_m is not None else self._precision(geometry),
            authority=authority or self.authority,
            source_type=source_type,
        )

    def health(self):  # type: ignore[override]
        """Health that reflects the backing files, not just the last poll.

        A snapshot whose files are weeks old is stale no matter how recently the
        adapter was polled, so staleness is measured against file mtime too. For
        a multi-file signal the *oldest* file sets the age: a signal is only as
        current as its stalest component, and using the newest would let a
        three-day-old municipal export read as fresh because a newer file in the
        same signal exists.
        """

        base = super().health()
        paths = self.resolve_all()
        if not paths:
            base.state = HealthState.UNKNOWN
            base.message = base.message or "backing file not present"
            return base
        now = utcnow().timestamp()
        ages = {p.name: max(0.0, now - p.stat().st_mtime) for p in paths}
        oldest = max(ages, key=lambda n: ages[n])
        age_s = ages[oldest]
        absent = [n for n in self.files if n not in ages]
        label = oldest if len(ages) == 1 else f"{len(ages)} files, oldest={oldest}"
        base.message = f"{label} age={age_s / 3600:.1f}h records={base.records_received}"
        if absent:
            base.message += f" missing={len(absent)}/{len(self.files)} declared files"
        if self._read_errors:
            base.state = HealthState.DEGRADED
            base.message += f" read_errors={len(self._read_errors)}"
        if base.state is HealthState.HEALTHY and age_s > self.stale_after_s:
            base.state = HealthState.STALE if hasattr(HealthState, "STALE") else HealthState.OFFLINE
            base.freshness_seconds = age_s
        return base


def json_records(doc: Any, *keys: str) -> list[dict[str, Any]]:
    """Pull a list of record dicts out of the several envelope shapes used.

    Feeds arrive as a bare list, ``{"features": [...]}``, ``{"Features": [...]}``
    (SDOT capitalises it), ``{"result": [...]}`` or a single GeoJSON
    ``Feature`` (the NWS station-observation endpoint returns one). Callers
    name the keys they expect; bare lists and single features always work.
    """

    if isinstance(doc, list):
        return [d for d in doc if isinstance(d, dict)]
    if isinstance(doc, dict):
        if doc.get("type") == "Feature":
            return [doc]
        for key in keys:
            value = doc.get(key)
            if isinstance(value, list):
                return [d for d in value if isinstance(d, dict)]
    return []


def flatten_features(doc: Any, *keys: str) -> list[dict[str, Any]]:
    """Turn a GeoJSON ``FeatureCollection`` into one flat dict per feature.

    Properties are merged up to the feature level and the geometry is kept
    under ``geometry``, which is what ``SnapshotAdapter._geometry_of`` looks
    for. Without this every GeoJSON adapter would repeat the same unwrapping.
    """

    out: list[dict[str, Any]] = []
    for feature in json_records(doc, *keys):
        if feature.get("type") != "Feature" and "properties" not in feature:
            out.append(feature)
            continue
        properties = feature.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        flat = dict(properties)
        geometry = feature.get("geometry")
        if isinstance(geometry, dict):
            flat["geometry"] = geometry
        if feature.get("id") is not None:
            flat.setdefault("feature_id", feature["id"])
        if feature.get("bbox") is not None:
            flat.setdefault("bbox", feature["bbox"])
        out.append(flat)
    return out


def feature_geometry(item: dict[str, Any]) -> dict[str, Any] | None:
    """Geometry of an already-flattened feature, tolerating SDOT's shapes.

    Accepts both a GeoJSON geometry object and SDOT's bare
    ``PointCoordinate`` pair, which is ``[lat, lon]``.
    """

    geometry = geometry_from(item.get("geometry"))
    if geometry:
        return geometry
    raw = item.get("PointCoordinate")
    if isinstance(raw, (list, tuple)) and len(raw) >= 2:
        lat, lon = _as_float(raw[0]), _as_float(raw[1])
        if lat is not None and lon is not None:
            return {"type": "Point", "coordinates": [lon, lat]}
    return geometry_from(item.get("PointCoordinate"))


def _effective_suffix(path: Path) -> str:
    """Suffix after one layer of gzip, so ``x.csv.gz`` reads as ``.csv``."""

    name = path.name.lower()
    if name.endswith(".gz"):
        name = name[: -len(".gz")]
    return Path(name).suffix or name


def multiline(*parts: str) -> str:
    """Join non-empty fragments with a single space, for headline building."""

    return " ".join(p.strip() for p in parts if p and p.strip())


__all__ = [
    "SnapshotAdapter",
    "UNLOCATED_PRECISION_M",
    "data_root",
    "feature_geometry",
    "flatten_features",
    "geometry_from",
    "json_records",
    "multiline",
    "parse_feed_time",
    "point_from_latlon",
    "workspace_root",
]
