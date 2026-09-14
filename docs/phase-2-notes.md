# Phase 2 — scheduling: what was built, and two more spec assumptions that did not hold

Phase 2 is the spec's "solar elevation, scoring, `season next/day`, batching via
existing route ordering, ICS export" — turning "which week" into "fly Thursday,
these parcels, in this order, two batteries".

Phase 1's notes are in [`phase-1-assumptions.md`](phase-1-assumptions.md); this
file covers only what is new or changed.

---

## 1. The finding that matters most

**Multispectral work is impossible at Seinäjoki after roughly 15 September —
six weeks earlier than the spec assumes.**

Maximum solar elevation at solar noon is `90 − latitude + declination`. At
62.79 °N:

| Date | Max elevation | Consequence |
|---|---|---|
| Summer solstice | 50.6° | everything possible |
| **Equinox** | **27.2°** | **already below the 30° multispectral floor** |
| Winter solstice | 3.8° | nothing radiometric possible |

So the configured floors bound the season like this:

| Floor | Usable span at Seinäjoki | Days |
|---|---|---|
| 30° (multispectral, thermal, `both`) | 28 Mar – 15 Sep | 171 |
| 20° (RGB-only structural) | 2 Mar – 11 Oct | 223 |

Spec §7.2 anticipates this for "late October" catch-crop flights. The wall
actually arrives at the September equinox, and **the shipped
`catch_crop_biomass` campaign targets straight into it**: it is multispectral,
and triggers 35 days after catch-crop establishment, which for a crop
established after a mid-August cereal harvest lands around 20 September.

This is verified end to end — the module reports:

> Nothing is flyable in this horizon because the sun never clears the elevation
> floor. At 62.8°N the sun clears 30° only between 2026-03-28 and 2026-09-15;
> RGB-only work at 20° has a wider season. This is a latitude limit, not a
> weather one — no forecast will change it.

**What to do about it is an agronomic decision, not a code one**, so nothing was
changed in the campaign library. The options are: accept that catch-crop
biomass is an early-September flight at the latest; lower
`min_solar_elevation_deg` if the radiometry genuinely tolerates it; or move the
campaign to RGB/structural at the 20° floor and give up the NDRE-based C:N.
Flag it to the agronomists before the first autumn season.

---

## 2. Two more spec claims that did not survive contact with the code

Phase 1 found one (`altitude_for_gsd` already existed as `height_from_gsd`).
Phase 2 found two more, both in §7.5's "reuse the existing … rather than
writing new ones".

**"The existing greedy nearest-neighbour route ordering" exists only in
JavaScript.** It is `greedyTSP` in
`templates/js/jobs/list-math.js`, used by the map view's drag-reorder. There is
no Python equivalent: `job_store.apply_route_order()` *applies* an explicit
order, it does not compute one. So `scheduling.greedy_route()` is a faithful
port — same northernmost-then-westernmost start, same nearest-next step, same
haversine — so that a field day planned in Python and a route dragged out in the
browser agree instead of quietly disagreeing. It is tested against the JS
algorithm's behaviour, not against itself.

**More importantly, the operator's own order wins.** A dragged-out route encodes
knowledge the algorithm does not have — where the gates are, which corner is
soft after rain. `fieldday` uses the folder's saved `sort_order` whenever every
parcel has one, and falls back to greedy only for an unrouted folder.
`order_source` on the plan says which was used, and the CLI prints it.

**The host's forecast does not cache hourly wind.** §7.1 says to consume
`flight_forecast` and not open a second weather client. `build_forecast` is
consumed — it owns the satellite passes, their clear-sky qualification and the
golden-day concept, all of which are expensive (MGRS grid + CelesTrak
propagation) and none of which is reimplemented. But `WeatherResult` caches only
*daily* aggregates plus hourly **cloud**; per-hour wind and precipitation are
not there, and §7.2's "contiguous hours meeting elevation, wind and cloud
thresholds" cannot be computed without them.

So `weather_history.fetch_hourly()` fetches the hourly block, sharing the host's
forecast TTL and URL. This is the same latitude the spec grants for history
("extend the existing cache layer if history is needed"), applied to resolution
rather than to time. The alternative — adding an `hourly_wind` field to the
host's `forecasting/weather.py` — is a better long-term home and a one-field
change; it was not done here because it is a host file and this overlay's
premise is that the host diff stays at the wiring lines.

---

## 3. Design decisions worth knowing

**Hard gates are not weights.** Outside the window, wind above limit, rain above
threshold, no qualifying solar hour, or `flight_ready: false` → the score is
zero, whatever the weighted sum would have said. A weighted sum alone would let
a good cloud score paper over unflyable wind. Each gate has a name
(`scheduling.GATE_*`) so surfaces report *which* one fired rather than just a
zero.

**A day's headline score is its best campaign, not the mean.** A day is worth
flying because *something* on it scores well. The mean is carried separately and
used only to break ties. The component breakdown printed beside the score
belongs to the campaign that set it — averaging components would produce a
breakdown that explains no campaign in particular and does not add up to the
number next to it.

**`satellite_coincidence` leaves the normalisation when a campaign ignores it.**
Otherwise an emergence count would be capped at 0.9 for failing to coincide with
a satellite pass it never wanted.

**Cloud gates no hours, only the score.** Overcast is poor for radiometry and
genuinely good for structural RGB — diffuse light removes shadows — so it
belongs in the weighted sum, not in the list of hours the drone can fly.

**Overflow is reported, and so is being passed over.** The field-day budget is
filled first-fit in urgency order, which packs more flying into the day but lets
a large, urgent parcel be skipped while smaller, less urgent ones fit around it.
That is usually the right trade, but it is never silent: the plan names the
deferred parcel, what it outranks, and what to do about it.

**ICS windows are all-day events spanning earliest→latest.** The window is the
truth; the target is a preference inside it. A timed 10:00 slot on one specific
day would claim a precision the uncalibrated stage thresholds do not have. The
target and the ± band go in the description. UIDs are stable per campaign, so
re-exporting after a re-plan updates the event rather than duplicating it.

---

## 4. Deviation from §11's file layout

The spec puts scoring, batching and field-day plans in one `scheduling.py`.
They are split:

- `solar.py` — ephemeris (as specified)
- `scheduling.py` — conditions, usable hours, scoring, ranking, route algorithm
- `fieldday.py` — batching, budget, launch sites *(new)*
- `opportunities.py` — orchestration over a folder *(new)*
- `ics.py` — calendar export (as specified)

One module carrying all of it would have run past 900 lines and past the
project's own `max-complexity = 10` ruff gate. The split is along the seam the
spec itself draws in §7.4 (scoring) versus §7.5 (batching).

---

## 5. Still not built

Phases 3 and 4, unchanged from the Phase 1 list except that scheduling has
moved out of it: the Season UI and Gantt, `campaign_apply` / `campaign_log`,
calibration fitting, the season PDF, and **the S2 pixel-snap helper** (§7.3's
"snap a sub-area to the S2 20 m grid and report how many full pixels the survey
polygon contains"), which spec §12 places in Phase 4.

Satellite *coincidence scoring* is built — it is part of §7.4's weighted sum —
including the `required` gate and the nearest-pass flag. It needs the
Sentinel-2 MGRS grid file, which is a ~20 MB download the host does not bundle;
without it the module warns once and coincidence simply scores zero rather than
failing the ranking.

The weather-event trigger for `lodging_survey` also remains Phase 4. The gust
history it needs is now fetched (`HourSample.gust_ms`), so the remaining work is
the trigger evaluation, not the data.
