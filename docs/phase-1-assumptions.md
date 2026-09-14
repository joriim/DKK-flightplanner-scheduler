# Phase 1 — assumptions made, and the §14 decisions that are still open

The spec's Appendix D asks for this list before any code is written. It is kept
here rather than in a commit message because the §14 items still need answers
from the project, and the assumptions need re-checking whenever the host moves.

---

## 1. What the spec asked for that already exists

**`altitude_for_gsd` is already implemented, under another name.** Spec §5 asks
to add `DroneConfig.altitude_for_gsd(gsd_cm)`. The host already has both
directions:

```python
DroneConfig.height_from_gsd(gsd_cm) -> float   # metres AGL for a required GSD
DroneConfig.gsd_from_height(height_m) -> float # GSD achieved at an altitude
```

So **no change was made to `DroneConfig`**. `season/gsd.py` calls the existing
methods through a `Protocol`, and applies only the resolution *policy* the spec
describes: clamp, then flag. Adding a third method that duplicated
`height_from_gsd` would have been exactly the "duplicate camera maths" §5
forbids.

The spec's sanity anchors check out against the shipped profiles, and the tests
pin them: M3M RGB 1.34 cm @ 50 m, MS 2.14 cm @ 50 m.

---

## 2. Assumptions about the host the spec did not pin down

| # | Assumption | Why, and what breaks if it is wrong |
|---|---|---|
| 1 | **Altitude ceiling** is `min(flight.max_height_agl_m, 120)`. | `EU_OPEN_CATEGORY_MAX_AGL_M = 120` exists in `config.py`; `max_height_agl_m` defaults to 110. |
| 2 | **No numeric UAS-zone altitude cap is available.** `zones.py` reports which zones a polygon intersects, not a ceiling in metres. | `gsd.resolve()` takes an optional `zone_cap_m` and nothing passes it yet. When the host grows a per-zone ceiling, wire it there — one argument, no restructuring. |
| 3 | **A folder is one path segment under `output.output_dir`**, resolved only through `job_store.resolve_folder_dir()`. | That function is the host's traversal guard, and the season API takes folder names straight off HTTP requests. A season plan at the output root is refused outright: `season_<year>.json` is per folder. |
| 4 | **Job centroids come from `best_polygon()` in EPSG:4326.** | The season module stores no geometry (§2) and re-reads the host's polygons when it needs a coordinate. |
| 5 | **One weather cell per folder** (mean of centroids, rounded to ~1 km), per §6.2. | A folder spanning more than `max_folder_span_km` warns rather than silently averaging two microclimates. Per-job cells are a Phase 2 refinement. |
| 6 | **The Open-Meteo archive endpoint is not in the host config**, so `season.archive_url` was added rather than hardcoded — alongside the existing `weather.open_meteo_url`, which the season module reads instead of picking its own forecast URL. |
| 7 | **History needs its own TTL.** `weather.cache_max_age_hours` (3 h) is right for forecasts and wrong for the past, which is immutable. History is cached under `season.history_ttl_days`, and a *finished calendar year* never expires. |
| 8 | **`job_store.write_json_atomic()` is the house atomic write**, reused via an import with an identical local fallback so the engine still works standalone. |
| 9 | **`[[crops]]` / `[[campaigns]]` follow the `[[drones]]` precedent**: any user entry replaces the entire built-in list. Built-ins ship in `season_defaults.toml`, exactly as drone profiles ship in `drones.toml`. |
| 10 | **`filelock` is available** (a host dependency) for the per-plan-file lock. Without it the lock degrades to a no-op; the atomic rename still means the file cannot be *corrupted*, only that one of two simultaneous edits can be lost. |

### One-line host change still needed

`config.py`'s `save_config()` does not know about `[season]`. Until `"season"`
is added to `_SAVE_SECTIONS` and `{"crops", "campaigns"}` to `_SAVE_SKIP`, the
**⚙ Settings** UI will not round-trip season settings. This was deliberately
**not** done here — it is a change to a host file, and the overlay's whole
premise is that the host diff stays to the wiring lines. Everything else is
already editable by hand in `config.toml`.

---

## 3. The §14 decisions — how each was handled

These are the questions the spec says block coding. None of them turned out to
block Phase 1; each was resolved with a default that is cheap to change.

**1. Sowing date source.** Assumed **farmer-reported**, entered via
`season init --sowing` or a per-parcel CSV (`--from-csv`, English and Finnish
headers, `YYYY-MM-DD` or `DD.MM.YYYY`). *Still open:* if Datakasvukunto already
holds sowing dates for the 57 parcels, wire to that instead of retyping —
`planner.upsert_assignment()` is the single seam to feed.

**2. Folder = season?** Assumed **yes**. `season_<year>.json` lives in the
folder directory, and `--season` overrides the year, so one folder can carry
several seasons and `season status --season 2026 --missed` still works.
`store.list_seasons()` enumerates them. *Still open:* confirm this matches how
the 57 parcels are actually foldered.

**3. Whose windows?** Took the spec's recommendation: **an `audience` tag**
(`farmer` | `researcher` | `both`) on `CampaignType`, with `--audience`
filtering on the CLI, REST and MCP. One plan, not two. `s2_calibration` is
tagged `researcher`; `canopy_peak` and `bare_soil_baseline` are `both`.

**4. Repeatable cadence.** Implemented **both**, because the spec is right that
adaptive is more defensible and it turned out to cost nothing — the thermal
series already exists. `mode = "cadence"` steps by `every_days`;
`mode = "thermal"` steps by `every_gdd`, so a cold season stretches the interval
instead of marching down the calendar past a crop that has not changed.
`s2_calibration` ships thermal; `disease_watch` and `maturity_forecast` ship
calendar cadence.

**5. Normals source.** **Open-Meteo archive**, behind `fetch_normals()` as a
deliberate seam. FMI slots in there without touching a caller, per the README's
existing note that FMI is a planned alternative.

---

## 4. Deliberately not built in Phase 1

Appendix D says Phase 1 only, and these are absent rather than stubbed — a
command that answers "not implemented" is worse than one that is not in
`--help`.

- **Opportunity scoring, solar elevation, satellite coincidence, batching**
  (Phase 2) — `season next` / `season day`, `scheduling.py`, `solar.py`.
  `[season.weights]` and the solar thresholds are in the config schema already
  so that shipping Phase 2 is not a second settings migration.
- **UI** (Phase 3) — `season-view.js`, the Gantt, the opportunity strip.
- **`campaign_apply`, `campaign_log`, calibration fitting, the S2 pixel-snap
  helper, ICS export, the season PDF** (Phase 3/4).
- **Weather-event triggers.** `lodging_survey` is instantiated and carries its
  gust threshold, but is listed with **no window** and an explicit reason rather
  than being silently dropped. Evaluating it needs the gust history query, which
  is Phase 4.

The spec's own caution stands: **do not start Phase 3 before Phase 1's window
dates have been sanity-checked against one real season of data.** The stage
thresholds are placeholders, and the module's honesty about that is the reason
it is safe to ship early — not a reason to trust the dates.

---

## 5. What Phase 1 deliberately refuses to do

- **Guess a sowing date.** No sowing date → campaigns exist in state `planned`
  with a `sowing_date_required` flag and no window.
- **Present an uncalibrated date as precise.** Every window carries
  `uncertainty_days`, a `basis` (`observed` / `forecast` / `normal`) and a
  `confidence`. The band collapses to zero only for a stage already crossed on
  measured history, and widens monotonically with projection distance. A season
  eight months out reports ±60 days, which is the honest answer.
- **Plan a spray route.** Campaigns whose purpose is plant-protection-related
  (`weed_map_early`, `disease_watch`, `anthesis_marker`) carry a mandatory
  ground-sprayer notice on every surface. Aerial application of plant protection
  products is prohibited (Directive 2009/128/EC Art. 9; Tukes does not permit
  drone sprayers in Finland). The module plans imaging only.
- **Take the pipeline lock.** `tests/season/test_no_pipeline_lock.py` asserts
  structurally that no season module imports `pipeline` or names the host's job
  lock, so a season request can never queue behind a running export.
