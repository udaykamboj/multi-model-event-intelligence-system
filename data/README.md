# `data/` — what each folder means and does

Datasets only. The captured **live** signals live one level up in
[`../live_feeds/`](../live_feeds), and the scripts that re-fetch everything are
in [`../collectors/`](../collectors).

```
data/
  regional/     Washington & Seattle records — directly relevant to this region
  reference/    static reference geometry and timetables used for spatial joins
  research/     large research corpora; historical/statistical context only
```

The rule that separates these three: **`regional/` can change an alert**,
`reference/` describes infrastructure that never changes, and `research/` may
only ever inform a baseline, a similarity comparison, or a statistical
expectation. Nothing in `research/` is allowed to assert a current fact.

---

## `data/regional/` — region-specific records

Files that describe Washington State and Seattle directly. These are the
historical records the platform compares live events against.

| File | Rows | What it is |
|---|---|---|
| `washington_focused/washington_state_protest_events.csv` | 10,462 | ACLED-format protest events in WA. Fields include `date`, `lat`/`lon`, `event_type`, `actors`, `arrests`, `injuries_crowd`, `injuries_police`, `chemical_agents`, `property_damage`, crowd size. **The core historical comparison set for this region.** |
| `washington_focused/washington_dc_protest_events.csv` | 10,220 | Same schema, Washington DC. Comparative: what protest events look like in a different capital city. |
| `washington_focused/seattle_spd_crimes_2020_unrest.csv` | 1,742 | Geocoded Seattle Police NIBRS reports from the 2020 unrest period. The empirical basis for *which infrastructure damage actually follows* a civil-unrest event. |
| `washington_focused/dc_mpd_crimes_2020_unrest.csv` | 939 | DC Metropolitan Police equivalent, same purpose, comparative. |
| `washington_focused/dc_mpd_crimes_2021_capitol.csv` | 209 | DC Capitol insurrection period. |

## `data/reference/` — static infrastructure reference

Never changes during an event. Loaded once and joined against, never polled.

| Path | Contents |
|---|---|
| `infrastructure_gis/seattle_critical_facilities.geojson` | 15 Point features — Level-1 trauma hospitals, fire stations, police, schools, civic buildings. Each has `name`, `category`, `city`, `state`. Drives "what critical infrastructure is near this event". |
| `infrastructure_gis/seattle_land_use_density.json` | 5 Polygons of neighbourhood character: `population_density_sq_mile`, `job_density_sq_mile`, `primary_zoning`, `land_use_mix`, `pedestrian_activity_index`, `protest_dispersion_vulnerability`. Used to weight crowd and dispersal estimates — a dense downtown core and a residential neighbourhood must not be scored alike. |
| `transit_gtfs/` | Full **King County Metro GTFS** feed, version `FAL26-161.2`, valid 2026-09-28 → 2027-03-26. `routes` 142 · `stops` 6,265 · `trips` 32,635 · `stop_times` 1,126,734 · `shapes` 167,087 · `calendar` 20 · `calendar_dates` 1,081, plus fare and network tables. Three agencies: Metro Transit (1), City of Seattle streetcar (23), Sound Transit (40). This is what makes "your bus" resolvable to real routes and stops. `google_transit.zip` is the same feed, still zipped. |

## `data/research/` — research corpora (context only)

Large third-party datasets. Useful for baselines, similarity and comparative
analysis. **Never a live detector, and never allowed to raise a notification.**

| Folder | Size | Rows × cols | What it is |
|---|---|---|---|
| `acled_hdx/demonstration_events.xlsx` | 39 MB | 4 sheets | **Contains no events.** Sheet 1 is a licensing notice; sheets 2–4 are pre-aggregated counts — sheet 2 `Country,Month,Year,Events` (30,556), sheets 3–4 `Country,Admin1,Admin2,ISO3,…,Month,Year,Events` (601,734 / 408,879). Useful only for event *rates* by region-month. Uncompressed sheet XML is ~500 MB, so read it streaming. Event-level ACLED data for this region is in `regional/washington_focused/` instead. |
| `crowd_counting_consortium/` | 202 MB | 72,181×62 · 139,823×72 · 63,147×74 | ACLED events enriched with crowd-size estimates (`size_low`/`size_high`/`size_mean`/`size_cat`), valence, participant measures, police measures, deaths. Three eras: 2017–2020, 2021–2024, 2025–present. **The best available empirical link between a gathering and its size**, which is what makes crowd estimates defensible rather than invented. |
| `mass_mobilization/` | 15 MB | 17,145×31 | Mass Mobilization (MIDA) dataset, 1990–2020. `participants`, `participants_category`, `protesterviolence`, up to four `protesterdemand*` and `stateresponse*` fields. Long-horizon comparison. |
| `gdelt_events/` | 150 MB | 99,028 · 96,469 · 143,446 | GDELT GKG/event exports for 2020-05-30, 05-31, 06-01 — the George Floyd protest period. **Tab-delimited despite the `.CSV` extension**; each file has a `.zip` sibling holding the same bytes. Global coverage. |
| `ucdp_conflict_events/` | 273 MB | 349,733×49 | UCDP Georeferenced Event Dataset v24.1. Armed conflict with `deaths_a`/`deaths_b`/`deaths_civilians`, `type_of_violence`, `where_coordinates`, lat/lon. A `ged241-csv.zip` holds the same CSV. Out-of-region; comparative only. |
| `navco_campaigns/` | 21 MB | 2,717×102 · 112,382×37 | Two different things. `NAVCO2-1_ForPublication.tab` is **protest campaigns**: goals, targets, size, repression, participant diversity, outcome. `navco3-0full.xlsx` is **not** campaigns despite the folder name — it is GDELT/CAMEO event data (`coder, event_desc, country_name, cowcode, date, verb_*, cameo_actor_*`). |
| `mobility_and_traffic/` | 136 MB | 51,145 · 2,438,660 · 49,215 | Google Community Mobility daily change-from-baseline, 7 `gps_*` series (retail, grocery, parks, **transit stations**, workplaces, residential, away-from-home). City / county / state. The county file also exists gzipped — same data. 2020 only. |
| `economic_opportunity_insights/` | 75 MB | 48,706 · 1,616,585 · 119,391 · 81,968 · 53 | Affinity card spend (city/county), county employment & wage change by industry, Womply merchants/revenue, and `GeoIDs_City.csv` — the **`cityid` → lat/lon lookup table required to place any Affinity row on a map**. |
| `municipal_crime_and_damage/` | 7 MB | 4,770 · 3,087 · 4,475 · 2,524 · 2,112 · 690 | 2020 civil-unrest comparators: Chicago and NYC 311 infrastructure-damage reports plus crime records, LA and SF crime records. Column names differ per city — `latitude`/`longitude` (Chicago, NYC, SF) vs `lat`/`lon` (LA). All out-of-region. |
| `violence_and_fatalities/` | 2 MB | 10,430×19 | Fatal police shootings, with `threat_type`, `armed_with`, and an explicit `location_precision` field. Contains WA rows. |

### Regional relevance, stated plainly

Only these hold Washington rows: `regional/washington_focused/*`, the crowd
counting and mass mobilization corpora, and `violence_and_fatalities`.
UCDP, NAVCO, GDELT and the municipal 311/crime files cover DC, Chicago, NYC,
LA, SF or the globe. They are comparative baselines only, and are filtered out
of regional analysis by design rather than by accident.

---

## Not part of `data/`

| Location | What it is |
|---|---|
| `../live_feeds/` | The ~46 captured live regional signals. Snapshots, not a live connection. |
| `../collectors/` | `download_all_sources.py`, `download_all_sources_extended.py` — re-fetch `live_feeds/` and the datasets. Tracked in git. |
| `../unused/` | Parked prior work: the superseded `platform_engine/` implementation, a stale virtualenv, `raw/` observation dumps, a stray LightGBM model, and prior-run SQLite databases. Not tracked. |
| `../data/live_system_state.json` → `../unused/` | Telemetry from the earlier `platform_engine` run: 50 signals configured, 47 healthy, 3,969 observations, 1,710 "active events". Useful only as a record of that run. |

## Refresh

```powershell
python collectors/download_all_sources.py            # the 46 live feeds
python collectors/download_all_sources_extended.py   # + datasets and research corpora
```

These payloads are gitignored because they are large and reproducible. This
README plus `../live_feeds/`'s contents in git history is the contract for what
should be present.
