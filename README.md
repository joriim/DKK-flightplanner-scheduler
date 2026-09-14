# Season planning for dkk-flightmanager

*Kasvukauden kuvaussuunnittelu* — the `season` module for
[SeAMKedu/dkk-flightmanager](https://github.com/SeAMKedu/dkk-flightmanager).

`dkk-flightmanager` answers **"how do I fly this parcel safely?"** — geometry,
terrain, keep-outs, UAS zones, route, KMZ.

This module answers the orthogonal question:
**"when in the growing season should I fly this parcel, and what for?"**

It turns a folder of parcel jobs plus crop and sowing information into a season
plan: an ordered set of **campaigns**, each a phenology-triggered flight window
with its own purpose, required GSD and sensor.

```
$ flightmanager season status --folder hiilisyke-2027

Season 2027, folder hiilisyke-2027 — 2027-06-04

Closing within 5 d (3)
  hiilisyke-2027/5241087453  Emergence / stand count  2027-06-01 → 2027-06-08  target 2027-06-04  ±2 d  [open, high, 0.6 cm, rgb]
  hiilisyke-2027/5241087454  Emergence / stand count  2027-06-01 → 2027-06-08  target 2027-06-04  ±2 d  [open, high, 0.6 cm, rgb]
  hiilisyke-2027/5241087455  Emergence / stand count  2027-06-01 → 2027-06-08  target 2027-06-04  ±2 d  [open, high, 0.6 cm, rgb]

Upcoming (33)
  hiilisyke-2027/5241087453  Early weed mapping       2027-06-12 → 2027-06-16  target 2027-06-14  ±3 d  [open, high, 0.4 cm, rgb]
  …
```

> **Development note — AI-assisted ("vibe coded").** Like the host project, this
> module was built largely through iterative prompting of an LLM coding agent
> rather than line-by-line hand authoring. It has a real test suite (268 tests),
> but it has not had a line-by-line human agronomic or security audit. **The
> stage thresholds it ships are explicitly placeholders** — see
> [Uncertainty is the feature](#uncertainty-is-the-feature).

---

## Status

**Phase 1 of four.** What ships: the phenology engine, the campaign library, the
plan store, and the `init` / `plan` / `status` surfaces on CLI, REST and MCP.

What does not ship yet: day-level opportunity scoring, solar elevation,
satellite coincidence, cross-parcel batching, the Season UI, and the calibration
loop. Those are Phases 2–4 and are **absent rather than stubbed** — a command
that answers "not implemented" is worse than one that is not in `--help`.

Design decisions, the host assumptions this rests on, and what is deliberately
refused are written up in [`docs/phase-1-assumptions.md`](docs/phase-1-assumptions.md).

---

## This repository is an overlay, not a fork

The module lives at the path it will occupy in the host:

```
src/flightmanager/season/     ← drops straight into the host's package
tests/season/
config.season.example.toml    ← append to your config.toml
docs/phase-1-assumptions.md
```

There is deliberately **no `src/flightmanager/__init__.py`**, so `flightmanager`
stays an implicit namespace package here and the directory merges into the
host's real package without a duplicate `__init__.py` to reconcile.

The engine imports nothing from the host at module scope, so it installs and
tests standalone:

```bash
python -m venv .venv && .venv/bin/pip install -e . && .venv/bin/pip install pytest responses
.venv/bin/python -m pytest tests/season -q      # 268 passed
```

### Installing into the host

```bash
cp -r src/flightmanager/season  <host>/src/flightmanager/season
cp -r tests/season              <host>/tests/season
cat config.season.example.toml >> <host>/config.toml
```

Then two wiring lines, and only two. `pipeline.py` is untouched.

```python
# src/flightmanager/cli.py
from flightmanager.season.cli import season_app
app.add_typer(season_app, name="season")

# src/flightmanager/web/server.py
from flightmanager.season.api import router as season_router
app.include_router(season_router)

# src/flightmanager/mcp_server.py
from flightmanager.season.mcp_tools import register
register(mcp)
```

One further one-line host change is needed before the **⚙ Settings** UI will
round-trip season settings: add `"season"` to `_SAVE_SECTIONS` and
`{"crops", "campaigns"}` to `_SAVE_SKIP` in `config.py`. Until then `[season]`
is edited in `config.toml` by hand, which works fine.

---

## How it works

```
CropProfile      crop + variety group → phenology parameters          (config)
    ↓
PhenologyModel   sowing date + daily temperatures → stage timeline    (derived)
    ↓
CampaignType     a purpose + trigger stage + GSD requirement          (config)
    ↓
Campaign         CampaignType instantiated for a job and a season     (persisted)
    ↓
Window           [earliest, target, latest], recomputed every run     (derived)
```

The separation matters: **CampaignType is agronomy** (stable, shared, config),
**Campaign is bookkeeping** (per parcel, per year, mutable), **Window is
derived** — never stored as truth, always recomputed. The one exception is a
window an operator pinned by hand, which survives every re-plan untouched. The
agronomist on the ground beats the model.

### Thermal time

Daily mean temperature, base +5 °C, optional upper cutoff — the Finnish
*tehoisa lämpösumma* convention — accumulated from sowing, or from establishment
for catch crops and autumn-sown cereals. Three sources are stitched into one
series, and the precedence is archive > forecast > normal so a published day is
never downgraded to a forecast value:

| Span | Source | Cache TTL |
|---|---|---|
| sowing → T−6 d | Open-Meteo historical archive | `history_ttl_days` (a finished year never expires) |
| T−6 d → T+14 d | Open-Meteo forecast | the host's 3 h forecast TTL |
| beyond | 30-year daily normal for the cell | `normals_ttl_days` |

### Uncertainty is the feature

**The shipped stage thresholds are placeholders.** They are order-of-magnitude
agronomic rules of thumb for Finnish conditions, not values from a verified
source. They ship with `confidence = "low"`, and the module is built so that
this cannot be hidden:

- every window carries `uncertainty_days`, a `basis` and a `confidence`;
- the band **collapses to zero** only for a stage already crossed on measured
  history — that date is known;
- it **widens monotonically** with projection distance. A season eight months
  out reports ±60 days, because that is the honest answer;
- `season plan`, the REST payload and every MCP tool repeat the caveat in
  words, so an assistant reading the tool cannot quote "24 May" without "±7 d,
  uncalibrated".

Calibration (Phase 4) is what fixes this: record observed BBCH when you fly,
fit, and `provenance.confidence` is upgraded with an `n` and a residual spread.
Until then, treat the dates as approximate — and **sanity-check them against one
real season of data before building anything on top of them.**

### Deriving flight parameters

No camera maths is duplicated. The host's drone profile already carries both
directions of the GSD ↔ altitude relation, so `season/gsd.py` only applies the
policy — clamp to `[min_altitude_m, min(max_height_agl_m, 120)]`, then flag the
two failure modes, which are asymmetric:

- **`gsd_unreachable`** — the altitude floor forces a *coarser* GSD than the
  campaign requires. The campaign cannot be flown as specified. The module names
  the drone profiles that could do it rather than proceeding quietly.
- **`low_altitude_workload`** — legal but low: narrow strips, more lines, less
  coverage per battery, more obstacle exposure. Flyable; the operator sees the cost.

Clamping *down* to the ceiling is neither — a coarse requirement met from a
lower altitude simply yields finer imagery than asked for.

---

## What this module does not do

**No spraying, ever.** Aerial application of plant protection products is
prohibited across the EU (Directive 2009/128/EC Art. 9), and Tukes does not
permit drone sprayers for plant protection in Finland because the Commission
treats drones as aerial vehicles. Campaigns whose purpose is plant-protection
related — `weed_map_early`, `disease_watch`, `anthesis_marker` — produce a
**prescription map for a ground sprayer**, never a spray route. Every surface
says so, in Finnish and English.

**No image processing.** The module plans acquisition; WebODM / Metashape /
Pix4D do the rest. It may record what was produced; it produces none of it.

**No agronomic recommendations.** It says "fly now to see X". It never says
"apply 40 kg N/ha".

**No pipeline lock.** Season planning is cheap and must not queue behind a
running export. `tests/season/test_no_pipeline_lock.py` asserts structurally
that no season module imports `pipeline` or names the host's job lock.

---

## Usage

```bash
# One crop and sowing date for every parcel in the folder…
flightmanager season init --folder hiilisyke-2027 --crop spring_barley --sowing 2027-05-14

# …or per parcel from the spreadsheet the farm handed over.
# Columns: job/parcel, crop, sowing. Finnish headers and DD.MM.YYYY also work.
flightmanager season init --folder hiilisyke-2027 --from-csv sowing.csv

# Compute every window. Idempotent; re-run it as the season progresses.
flightmanager season plan   --folder hiilisyke-2027

# What is open, what is closing, what was missed.
flightmanager season status --folder hiilisyke-2027
flightmanager season status --folder hiilisyke-2026 --season 2026 --missed

# Inspect the libraries and one campaign's reasoning.
flightmanager season crops
flightmanager season campaigns
flightmanager season windows --folder hiilisyke-2027 --campaign 2027-emergence_count-5241087453
```

`season plan` is idempotent and side-effect-free apart from the plan file.
Anything an operator typed — state, logged outcomes, notes, a manual window —
survives a re-plan; anything the model derived does not.

`--missed` is the point of the tool as much as `--closing` is: at season end it
shows which windows a one-drone operation actually lost to weather.

### REST

```
GET    /api/season/{folder}                → plan + computed windows + status
POST   /api/season/{folder}                → init / update crop & sowing
POST   /api/season/{folder}/plan           → recompute windows
GET    /api/season/{folder}/status         → open / closing / missed
GET    /api/season/-/crops                 → crop profiles and their provenance
GET    /api/season/-/campaign-types        → the campaign library
```

No SSE — these are fast, and they do not take the pipeline lock.

### MCP

Read tools: `season_status`, `campaign_detail`, `crop_profiles`. These queries
work end to end today:

```
When does the emergence window open for folder hiilisyke-2027?
Which parcels have a window closing in the next 5 days?
Which windows did we miss last season, and why?
```

---

## The campaign library

Twelve types ship, each tied to a decision someone actually makes. All of it is
config — `flightmanager season campaigns` prints the live list.

| id | Trigger | GSD | Sensor | Decision it serves |
|---|---|---|---|---|
| `bare_soil_baseline` | 2 wk before sowing | 2.5 cm | both | Soil zoning, sampling design, drainage and wet spots |
| `emergence_count` | emergence + 7 d | 0.6 cm | rgb | Stand density, gaps, resow decision |
| `weed_map_early` | crop 2–4 leaf | 0.4 cm | rgb | Patch-spray prescription — **for the ground sprayer** |
| `n_topdress_timing` | stem elongation | 3 cm | multispectral | Topdress N timing and rate zoning |
| `canopy_peak` | flag leaf | 3 cm | multispectral | Yield-potential signal, satellite cross-cal |
| `disease_watch` | booting → anthesis, every 7 d | 2 cm | both | Fungicide decision support |
| `anthesis_marker` | anthesis ±3 d | 3 cm | multispectral | FHB-critical window; hard and narrow |
| `lodging_survey` | gust > 15 m/s in 72 h | 2.5 cm | rgb | Lodging extent, combine routing, insurance |
| `maturity_forecast` | mid grain fill, every 10 d | 3 cm | multispectral | Harvest sequencing across parcels |
| `preharvest_reference` | yellow ripe | 3 cm | both | Pairs with yield-map validation |
| `catch_crop_biomass` | 4–6 wk after harvest | 3 cm | multispectral | Catch-crop biomass and C:N; carbon evidence |
| `s2_calibration` | every 200 °C·d | 4 cm | multispectral | 20 m pixel model calibration (Hiilisyke) |

Three are **repeatable**, modelled explicitly rather than by duplicating config
entries. `disease_watch` and `maturity_forecast` repeat on a calendar cadence;
`s2_calibration` repeats on **thermal** cadence — fly again once the crop has
moved on by another 200 °C·d, so a cold season stretches the interval instead of
marching down the calendar past a crop that has not changed.

`lodging_survey` is **event-triggered**. It is instantiated and carries its gust
threshold, but is listed with no window and an explicit reason until Phase 4
evaluates the trigger — listed so it is not forgotten, rather than silently dropped.

### ⚠ The `[[crops]]` / `[[campaigns]]` replace rule

Same as `[[drones]]`, and it has bitten people before:

> **Define any `[[crops]]` entry in `config.toml` and it REPLACES the entire
> built-in crop list. Same for `[[campaigns]]`. There is no per-entry merge.**

To tweak one crop, copy the whole built-in table first — it is in
`src/flightmanager/season/season_defaults.toml`. `flightmanager season crops`
always prints which list is in use, and every surface warns when yours has taken
over.

---

## Why these windows exist

Drones earn their place over Sentinel-2 in exactly two situations: **resolution**
(individual seedlings, weed patches, lodged areas, disease foci, which a 10–20 m
pixel cannot resolve) and **timing on demand** (the decision window is days wide
and cloud can close the satellite for a fortnight). Everything else — the
seasonal biomass and N trajectory at field scale — Sentinel-2 already serves more
cheaply.

The sharpest cases: cereal plant density needs very high resolution and degrades
distinctly as GSD coarsens; the FHB fungicide window spans a few days around
anthesis and one cloudy fortnight consumes it entirely; and for Hiilisyke,
upscaling ground plots straight to coarse pixels underestimated biomass by
8.9–17 % and GPP by 5.0–9.7 %, with the bias varying between areas and seasons —
a handful of well-timed UAV flights are the only practical way to quantify it.

Counterweight, deliberately kept in the library notes: UAV weed maps
systematically under-predict weeds relative to camera-based sprayers, with
omission rates of 41–65 % because the smallest weeds fall below image resolution.
Plan low, plan early, and treat the map as a prescription, not a survey.

Full rationale and sources are in the design spec's Appendices A and C.

---

## Licence

Same as the host project.
