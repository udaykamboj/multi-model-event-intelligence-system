"""Command line entry point.

``pyproject.toml`` declares ``infraimpact = "infraimpact.cli:main"`` and nothing
implemented it, so the platform could be started only by importing it in a
REPL. That is not a runnable product, and it is the kind of gap that hides:
every module imports cleanly, the tests pass, and there is still no way to run
the thing.

Six commands, in the order an operator meets them:

``doctor``     Does this checkout have its data, its database and its keys?
``feeds``      What does each registered signal actually read right now?
``run``        Run the observation -> analysis -> exposure loop.
``serve``      Serve the public API.
``seed``       Insert a demo user so ``/v1/user`` has something to answer about.
``schema``     Write the PostGIS DDL for a production database.

``feeds --coverage`` is the one that earns its place. The catalogue declares 48
signals, but a declaration is a claim, not evidence, and several signals in
this corpus read payloads that do not match their filenames - one WSDOT file
backing four signals, a "Chicago 311 infrastructure damage" export that is
4,770 rows of graffiti removal. ``coverage`` resolves every signal against the
files actually on disk and prints what came out, so a gap is visible before it
becomes a silent one.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import traceback
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

from .config import Settings, get_region, get_settings, sqlite_path
from .domain.schemas import (
    Geometry,
    NotificationPreferences,
    RouteProfile,
    SavedPlace,
    UserContext,
)

# --------------------------------------------------------------------------
# presentation helpers
# --------------------------------------------------------------------------


def _rule(title: str = "", width: int = 96) -> str:
    if not title:
        return "=" * width
    pad = max(0, width - len(title) - 3)
    return f"== {title} " + "=" * pad


def _column_widths(rows: Sequence[Sequence[str]], headers: Sequence[str]) -> list[int]:
    if not rows:
        return [len(h) for h in headers]
    return [max(len(str(h)), *(len(str(r[i])) for r in rows)) for i, h in enumerate(headers)]


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    """Print a fixed-width table.

    No dependency on a table library: this is the one piece of output an
    operator reads to decide whether the platform has data, and it has to work
    in a bare ``python -m`` shell with nothing installed.
    """

    body = [[str(cell) for cell in row] for row in rows]
    widths = _column_widths(body, headers)
    line = "  ".join(str(h).ljust(widths[i]) for i, h in enumerate(headers))
    out = [line.rstrip(), "  ".join("-" * w for w in widths).rstrip()]
    for row in body:
        out.append("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    return "\n".join(out)


def _ok(flag: bool) -> str:
    return "ok" if flag else "MISSING"


def _truncate(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "â€¦"


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------


def _find_llm_env() -> tuple[Path | None, bool]:
    """Locate the ``llm/.env`` that ships beside the package, without reading it.

    Only the *presence* of a key is reported. Its value is never printed, never
    logged and never written to the database - section 61.
    """

    for candidate in (
        Path.cwd() / "llm" / ".env",
        Path(__file__).resolve().parents[2] / "llm" / ".env",
    ):
        if candidate.is_file():
            try:
                body = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return candidate, False
            has_key = any(
                line.strip().startswith(("OPENROUTER_API_KEY=", "OPENROUTER_KEY="))
                and len(line.split("=", 1)[1].strip()) > 20
                for line in body.splitlines()
            )
            return candidate, has_key
    return None, False


def cmd_doctor(args: argparse.Namespace) -> int:
    settings = get_settings()
    region = get_region(settings.region_id)
    problems: list[str] = []

    from .sources.catalog import SIGNALS
    from .sources.snapshot import workspace_root

    root = workspace_root()
    print(_rule("environment"))
    print(f"  python            {sys.version.split()[0]}  ({sys.executable})")
    print(f"  region            {region.region_id} - {region.display_name}")
    print(f"  bounds            {region.bounds}")
    print(f"  timezone          {region.timezone}")
    print(f"  database_url      {settings.database_url}")
    print(f"  raw_store         {settings.raw_store_path}")
    print(f"  bus               {settings.bus_backend}")

    print()
    print(_rule("data"))
    print(f"  workspace_root    {root}  [{_ok(root.is_dir())}]")
    for name in ("live_feeds", "data"):
        path = root / name
        exists = path.is_dir()
        count = sum(1 for _ in path.rglob("*") if _.is_file()) if exists else 0
        print(f"  {name:<17} {str(path)}  files={count}  [{_ok(exists)}]")
        if not exists:
            problems.append(f"{name}/ is missing from {root}")

    db_path = sqlite_path(settings.database_url)
    print()
    print(_rule("storage"))
    if settings.is_sqlite:
        print(f"  sqlite file       {db_path}  [{_ok(db_path.exists())}]")
        if not db_path.exists():
            print("                     (created on first write; `infraimpact seed`)")
    else:
        usable, reason = _postgres_status()
        print(f"  postgres          [{_ok(usable)}] {reason}")
        if not usable:
            problems.append(reason)

    print()
    print(_rule("credentials"))
    llm_path, llm_ok = _find_llm_env()
    print(f"  llm/.env          {llm_path or 'not found'}  [{_ok(llm_ok)}]")
    if not llm_ok:
        print("                     the platform still runs; interpretation is rule-based")
    for name in ("wsdot", "sdot", "onebusaway", "acled"):
        value = getattr(settings, f"{name}_api_key", "")
        needed = name != "acled"
        state = "set" if value else ("not needed" if not needed else "not set")
        print(f"  {name+'_api_key':<17} {state}")

    print()
    print(_rule("interpretation (section 18)"))
    _report_interpretation(settings, problems)

    print()
    print(_rule("catalogue"))
    usable_signals = [s for s in SIGNALS if s.usable]
    historical = [s for s in SIGNALS if s.usage == "historical_only"]
    print(f"  signals           {len(SIGNALS)} declared, {len(usable_signals)} usable, {len(historical)} historical_only")
    declared_files = {f for s in SIGNALS for f in s.files}
    present = 0
    missing: list[str] = []
    for name in sorted(declared_files):
        found = any((root / s.subdir / name).is_file() for s in SIGNALS if name in s.files)
        if found:
            present += 1
        else:
            missing.append(name)
    print(f"  declared files    {present}/{len(declared_files)} present on disk")
    for name in missing[:10]:
        print(f"                     absent: {name}")
    if missing:
        problems.append(f"{len(missing)} declared payload file(s) absent (first: {missing[0]})")

    print()
    if problems:
        print(_rule("problems"))
        for problem in problems:
            print(f"  - {problem}")
        print()
        print("The platform will still start. These are coverage gaps, not crashes.")
    else:
        print("No problems found.")
    return 0



def _report_interpretation(settings, problems: list[str]) -> None:
    """Report what the section-18 layer will actually do, not what it might do.

    A key file being present is not the same as the interpretation layer working.
    The supplied ``llm`` package can be found and still fail to import, and the
    platform's response to that is to fall back to rule-based extraction - which
    is correct behaviour, and completely invisible unless it is reported. This
    prints the *resolved* mode, so a deployment running on NullLlmClient looks
    different from one that is actually interpreting.
    """
    from .llm.client import build_llm_client
    from .llm.interpreter import InterpretationLayer

    layer = InterpretationLayer(
        client=build_llm_client(), enabled=settings.llm_enabled
    )
    client = layer.client
    described = client.describe() if hasattr(client, "describe") else {}
    mode = described.get("mode") or "unconfigured"

    print(f"  enabled           {settings.llm_enabled}  (budget {settings.llm_calls_per_cycle}/cycle)")
    print(f"  client            {type(client).__name__ if client else 'None'}")
    print(f"  mode              {mode}")
    if described.get("model"):
        print(f"  model             {described['model']}")
    if hasattr(client, "is_live"):
        print(f"  live              {client.is_live}")

    if not settings.llm_enabled:
        print("                     disabled by configuration; claims are rule-only")
        return
    if mode in ("live", "mock"):
        print(
            "                     prose interpretation active; every value stays INFERRED"
        )
        if mode == "mock":
            problems.append(
                "LLM is in mock mode - responses are the supplied deterministic "
                "fixtures, not a live endpoint"
            )
        return
    print(
        "                     NOT configured: claim extraction, evidence synthesis, "
        "capability advice, hypotheses and user explanation are all unavailable. "
        "The loop still runs rule-only."
    )
    problems.append(
        "section-18 interpretation layer is not configured "
        f"(mode={mode}); evidence synthesis and user explanation are unavailable"
    )



def _postgres_status() -> tuple[bool, str]:
    from .storage.postgres_driver import postgres_requirements_met

    return postgres_requirements_met()


# --------------------------------------------------------------------------
# feeds
# --------------------------------------------------------------------------


def cmd_feeds(args: argparse.Namespace) -> int:
    from .sources import registry as reg
    from .sources.catalog import SIGNALS

    settings = get_settings()
    specs = list(SIGNALS)
    if args.realtime_only:
        specs = [s for s in specs if s.usage != "historical_only"]
    if args.signal:
        specs = [s for s in specs if args.signal in s.source_id]

    print(_rule("signal coverage"))
    print(
        f"  {len(specs)} signal(s)  region={settings.region_id}  "
        f"retrieval={'live HTTP' if settings.enable_network_sources else 'snapshots'}"
    )
    print()

    rows: list[list[str]] = []
    totals = {"records": 0, "observations": 0, "unlocated": 0, "region": 0, "scope": 0}
    errors: list[str] = []

    for spec in specs:
        cls = reg.ADAPTERS.get(spec.adapter)
        if cls is None:
            rows.append(
                [spec.source_id, spec.adapter, "NO ADAPTER", "", "", "", "", ""]
            )
            errors.append(f"{spec.source_id}: no adapter registered for {spec.adapter!r}")
            continue
        adapter = cls(spec)
        paths = adapter.resolve_all() if hasattr(adapter, "resolve_all") else (
            [adapter.resolve()] if adapter.resolve() else []
        )
        declared = len(spec.files)
        if not paths:
            rows.append(
                [
                    spec.source_id,
                    spec.category,
                    "no file",
                    f"0/{declared}",
                    "",
                    "",
                    "",
                    _truncate(spec.note, 60),
                ]
            )
            continue
        try:
            records = list(adapter.poll(None))
            observations = [o for o in (adapter.normalize(r) for r in records) if o]
        except Exception as exc:  # noqa: BLE001 - report, never abort the sweep
            errors.append(f"{spec.source_id}: {type(exc).__name__}: {exc}")
            rows.append(
                [
                    spec.source_id,
                    spec.category,
                    "ERROR",
                    f"{len(paths)}/{declared}",
                    "",
                    "",
                    "",
                    f"{type(exc).__name__}: {_truncate(exc, 60)}",
                ]
            )
            continue

        unlocated = sum(1 for o in observations if o.geometry is None)
        admissions: dict[str, int] = {}
        for record in records:
            label = record.payload.get("_admission", "region")
            admissions[label] = admissions.get(label, 0) + 1
        authority = observations[0].provenance.authority.value if observations else ""
        truncated = "yes" if getattr(adapter, "_budget_exhausted", False) else ""

        totals["records"] += len(records)
        totals["observations"] += len(observations)
        totals["unlocated"] += unlocated
        totals["region"] += adapter.dropped_out_of_region
        totals["scope"] += adapter.dropped_out_of_scope

        rows.append(
            [
                spec.source_id,
                spec.category,
                f"{len(observations)} obs",
                f"{len(paths)}/{declared}",
                _admission_summary(admissions),
                str(adapter.dropped_out_of_region),
                authority + (f" +{truncated}" if truncated else ""),
                _truncate(spec.note or (adapter._message or ""), 66),
            ]
        )

    print(
        _table(
            [
                "signal",
                "category",
                "records",
                "files",
                "admission",
                "geo-drop",
                "authority",
                "note",
            ],
            rows,
        )
    )
    print()
    print(
        f"  totals: {totals['observations']} observations from {totals['records']} records; "
        f"{totals['unlocated']} unlocated, "
        f"{totals['region']} dropped outside bounds, "
        f"{totals['scope']} dropped outside study scope"
    )
    if errors:
        print()
        print(_rule("problems"))
        for error in errors:
            print(f"  - {error}")
    if args.verbose:
        print()
        print(_rule("per-signal telemetry"))
        for spec in specs:
            cls = reg.ADAPTERS.get(spec.adapter)
            if cls is None:
                continue
            adapter = cls(spec)
            try:
                list(adapter.poll(None))
            except Exception:  # noqa: BLE001
                continue
            if adapter._message:
                print(f"  {spec.source_id}: {adapter._message}")
    return 1 if errors else 0


def _admission_summary(admissions: dict[str, int]) -> str:
    if not admissions:
        return ""
    order = ("region", "comparator", "unlocated")
    parts = [f"{k}={admissions[k]}" for k in order if k in admissions]
    parts += [f"{k}={v}" for k, v in admissions.items() if k not in order]
    return " ".join(parts)


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------


def _build_runtime(settings: Settings, *, include_historical: bool, synthetic: bool):
    from .bus.event_bus import build_bus
    from .runtime import Runtime
    from .sources.registry import SourceRegistry
    from .storage import build_repository

    repository = build_repository(settings.database_url, settings.driver)
    bus = build_bus(settings.bus_backend, bootstrap=settings.kafka_bootstrap)
    region = get_region(settings.region_id)
    source_registry = SourceRegistry(
        region,
        settings,
        include_historical=include_historical,
        include_synthetic=synthetic,
    )
    return Runtime(repository, bus, settings, registry=source_registry)


def cmd_run(args: argparse.Namespace) -> int:
    settings = get_settings()
    if args.interval is not None:
        settings = _with(settings, loop_interval_s=args.interval)
    runtime = _build_runtime(
        settings,
        include_historical=args.include_historical,
        synthetic=args.synthetic or settings.enable_synthetic_sources,
    )

    sources = len(runtime.registry)
    realtime = len(runtime.registry.realtime())
    print(_rule("run"))
    print(f"  region      {runtime.region.region_id}")
    print(f"  sources     {sources} registered, {realtime} realtime (may notify)")
    print(f"  database    {settings.database_url}")
    print(f"  interval    {settings.loop_interval_s}s")
    print(f"  cycles      {args.cycles or 'until interrupted'}")

    async def drive() -> int:
        results = await runtime.run_forever(max_cycles=args.cycles)
        for result in results:
            print(
                f"  cycle {result.cycle}: {result.summary()}",
            )
        return 0

    try:
        return asyncio.run(drive())
    except KeyboardInterrupt:  # pragma: no cover - interactive
        print("\ninterrupted")
        return 130
    finally:
        runtime.repo.close()


def _with(settings: Settings, **changes: Any) -> Settings:
    from dataclasses import replace

    return replace(settings, **changes)


# --------------------------------------------------------------------------
# serve
# --------------------------------------------------------------------------


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .api.app import create_app

    settings = get_settings()
    print(_rule("serve"))
    print(f"  app         {settings.app_name} ({settings.environment})")
    print(f"  url         http://{args.host}:{args.port}")
    print(f"  docs        http://{args.host}:{args.port}/docs")
    print(f"  database    {settings.database_url}")
    app = create_app(settings, run_loop=not args.no_loop)
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


# --------------------------------------------------------------------------
# seed
# --------------------------------------------------------------------------

#: Two demo users placed against the real Puget Sound infrastructure graph.
#: Deliberately different: a commuter whose day depends on the ferry and a
#: driver whose day depends on the Aurora bridge, so ``/v1/user/impact`` returns
#: two genuinely different answers instead of one answer twice.
DEMO_USERS = (
    UserContext(
        user_id="demo-commuter-ferry",
        saved_places=(
            SavedPlace(
                place_id="home-ballard",
                name="Ballard",
                geometry=Geometry(type="Point", coordinates=(-122.384, 47.668)),
                kind="home",
            ),
            SavedPlace(
                place_id="work-downtown",
                name="Downtown Seattle",
                geometry=Geometry(type="Point", coordinates=(-122.335, 47.608)),
                kind="work",
            ),
        ),
        route_profiles=(
            RouteProfile(
                route_id="ferry-ballard-west Seattle",
                name="Bainbridge ferry + downtown",
                geometry=Geometry(
                    type="LineString",
                    coordinates=(
                        (-122.384, 47.668),
                        (-122.373, 47.629),
                        (-122.335, 47.608),
                    ),
                ),
                modes=("ferry", "walk", "transit"),
                typical_departure_local="07:30",
            ),
        ),
        transport_modes=("ferry", "walk", "transit"),
        preferences=NotificationPreferences(
            minimum_urgency="low", channels=("in_app",), max_alerts_per_hour=6
        ),
    ),
    UserContext(
        user_id="demo-commuter-bridge",
        saved_places=(
            SavedPlace(
                place_id="home-northgate",
                name="Northgate",
                geometry=Geometry(type="Point", coordinates=(-122.328, 47.708)),
                kind="home",
            ),
            SavedPlace(
                place_id="work-capitol",
                name="Capitol Hill",
                geometry=Geometry(type="Point", coordinates=(-122.319, 47.623)),
                kind="work",
            ),
        ),
        route_profiles=(
            RouteProfile(
                route_id="drive-i5",
                name="I-5 southbound to Capitol Hill",
                geometry=Geometry(
                    type="LineString",
                    coordinates=(
                        (-122.328, 47.708),
                        (-122.326, 47.662),
                        (-122.319, 47.623),
                    ),
                ),
                modes=("drive",),
                typical_departure_local="08:00",
            ),
        ),
        transport_modes=("drive",),
        preferences=NotificationPreferences(
            minimum_urgency="moderate", channels=("in_app",), max_alerts_per_hour=3
        ),
    ),
)


def cmd_seed(args: argparse.Namespace) -> int:
    from .bus.event_bus import build_bus
    from .runtime import Runtime
    from .storage import build_repository

    settings = get_settings()
    repository = build_repository(settings.database_url, settings.driver)
    print(_rule("seed"))
    print(f"  database    {settings.database_url}")

    for user in DEMO_USERS:
        repository.users.upsert(user)
        print(
            f"  user        {user.user_id}: "
            f"{len(user.saved_places)} places, {len(user.route_profiles)} routes, "
            f"modes={'/'.join(user.transport_modes)}"
        )

    if args.cycle:
        from .sources.registry import SourceRegistry

        bus = build_bus(settings.bus_backend, bootstrap=settings.kafka_bootstrap)
        runtime = Runtime(
            repository,
            bus,
            settings,
            registry=SourceRegistry(
                get_region(settings.region_id),
                settings,
                include_historical=args.include_historical,
                include_synthetic=args.synthetic or settings.enable_synthetic_sources,
            ),
        )

        async def drive() -> None:
            result = await runtime.cycle()
            print()
            print(f"  cycle       {result.summary()}")

        asyncio.run(drive())

    counts = {
        "observations": repository.observations.count(),
        "events": len(repository.events.all_events()),
        "users": len(repository.users.all()),
    }
    print()
    print(
        f"  ledger      {counts['observations']} observations, "
        f"{counts['events']} events, {counts['users']} users"
    )
    print()
    print("  next:  infraimpact run --cycles 1     then    infraimpact serve")
    repository.close()
    return 0


# --------------------------------------------------------------------------
# schema
# --------------------------------------------------------------------------


def cmd_schema(args: argparse.Namespace) -> int:
    from .storage.postgres import DDL
    from .storage.postgres_driver import write_schema

    if args.out:
        path = write_schema(args.out)
        print(f"wrote {path}")
        return 0
    print(DDL)
    return 0


# --------------------------------------------------------------------------
# argparse
# --------------------------------------------------------------------------


def _common_options() -> argparse.ArgumentParser:
    """Flags accepted both before and after the subcommand.

    ``infraimpact -v feeds`` and ``infraimpact feeds -v`` are both what people
    type. argparse only honours the first form unless the subparser repeats the
    flag, so it is repeated rather than explained in a help string.
    """

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="machine-readable output where supported")
    common.add_argument("--verbose", "-v", action="store_true", help="extra detail")
    return common


def build_parser() -> argparse.ArgumentParser:
    common = _common_options()
    parser = argparse.ArgumentParser(
        prog="infraimpact",
        description="Dynamic Infrastructure Impact Intelligence Platform",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Environment:\n"
            "  INFRAIMPACT_DATABASE_URL       default sqlite:///./data/infraimpact.db\n"
            "  INFRAIMPACT_REGION             default puget-sound\n"
            "  INFRAIMPACT_ENABLE_NETWORK     1 to use live HTTP connectors\n"
            "  INFRAIMPACT_ENABLE_SYNTHETIC   1 to include fabricated scenario events\n"
            "  INFRAIMPACT_LOOP_INTERVAL_S    seconds between cycles\n"
        ),
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output where supported")
    parser.add_argument("--verbose", "-v", action="store_true", help="extra detail")
    sub = parser.add_subparsers(dest="command")

    doctor = sub.add_parser("doctor", parents=[common], help="check data, database and credentials")
    doctor.set_defaults(func=cmd_doctor)

    feeds = sub.add_parser("feeds", parents=[common], help="report what every registered signal reads")
    feeds.add_argument("--coverage", action="store_true", help="included by default; kept for readability")
    feeds.add_argument("--signal", help="substring filter on source_id")
    feeds.add_argument(
        "--realtime-only", action="store_true", help="skip historical_only research corpora"
    )
    feeds.set_defaults(func=cmd_feeds)

    run = sub.add_parser("run", parents=[common], help="run the observation/analysis loop")
    run.add_argument("--cycles", type=int, default=None, help="stop after N cycles")
    run.add_argument("--interval", type=float, default=None, help="seconds between cycles")
    run.add_argument(
        "--include-historical",
        action="store_true",
        help="attach the research corpora (they can never notify)",
    )
    run.add_argument(
        "--synthetic", action="store_true", help="include the fabricated scenario generator"
    )
    run.set_defaults(func=cmd_run)

    serve = sub.add_parser("serve", parents=[common], help="serve the API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--log-level", default="info")
    serve.add_argument("--no-loop", action="store_true", help="serve without the background loop")
    serve.set_defaults(func=cmd_serve)

    seed = sub.add_parser("seed", parents=[common], help="insert demo users and optionally run one cycle")
    seed.add_argument("--cycle", action="store_true", help="also run one loop cycle")
    seed.add_argument("--include-historical", action="store_true")
    seed.add_argument("--synthetic", action="store_true")
    seed.set_defaults(func=cmd_seed)

    schema = sub.add_parser("schema", parents=[common], help="print or write the PostGIS DDL")
    schema.add_argument("--out", help="write to this path instead of stdout")
    schema.set_defaults(func=cmd_schema)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:  # pragma: no cover - interactive
        print("\ninterrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - the CLI reports, it does not traceback
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        # A one-line message is right for an operator and useless for whoever has
        # to fix it. Two real bugs shipped for months behind this handler because
        # "'bool' object is not callable" names neither the class nor the line,
        # and ``--verbose`` was accepted as a flag and then ignored here.
        if getattr(args, "verbose", False):
            traceback.print_exc()
        else:
            print("       (re-run with -v for the traceback)", file=sys.stderr)
        return 1


__all__ = ["build_parser", "main"]


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
