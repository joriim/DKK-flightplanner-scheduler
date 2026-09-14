"""iCalendar export for season windows (spec §10.1, §10.2).

One VEVENT per campaign window, so the season plan lands in the calendar the
operator already lives in rather than a tool they have to remember to open.

Hand-rolled rather than pulling in a dependency: RFC 5545 for all-day events is
a short, stable subset, and the fiddly parts — CRLF line endings, 75-octet line
folding, and escaping commas and semicolons in text — are each a few lines and
are exactly what a library would be trusted for. They are tested.

Windows are written as **all-day events spanning earliest→latest**, not as a
timed slot on the target day. The window is the truth; the target is a
preference inside it, and a calendar entry at 10:00 on one specific day would
claim a precision the uncalibrated stage thresholds do not have. The target
date and the uncertainty band go in the description, where they read as
information rather than as a commitment.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
from typing import Iterable

from flightmanager.season.config import SeasonTables
from flightmanager.season.models import Campaign, SeasonPlan

_CRLF = "\r\n"
_PRODID = "-//SeAMK//dkk-flightmanager season//FI"

#: Campaign states worth putting in a calendar. A missed or cancelled window is
#: history; a flown one is recorded in the plan, not the diary.
_EXPORTABLE_STATES = ("planned", "open", "scheduled")


def _escape(text: str) -> str:
    """RFC 5545 §3.3.11 text escaping."""
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def _fold(line: str) -> str:
    """Fold to 75 octets per RFC 5545 §3.1, counting bytes and not characters.

    Finnish labels carry non-ASCII (``Orastumisen tiheyslaskenta`` is fine,
    ``kylvöpäivä`` is not), and folding on character count would split a
    multi-byte sequence across the boundary and corrupt it.
    """
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line

    chunks: list[bytes] = []
    start = 0
    limit = 75
    while start < len(raw):
        end = min(start + limit, len(raw))
        # Never split a UTF-8 continuation byte from its lead byte.
        while end > start and end < len(raw) and (raw[end] & 0xC0) == 0x80:
            end -= 1
        chunks.append(raw[start:end])
        start = end
        limit = 74  # continuation lines carry a leading space
    return (_CRLF + " ").join(c.decode("utf-8") for c in chunks)


def _stamp(when: _dt.datetime) -> str:
    return when.strftime("%Y%m%dT%H%M%SZ")


def _uid(plan: SeasonPlan, campaign: Campaign) -> str:
    """Stable per campaign, so re-exporting updates rather than duplicates."""
    seed = f"{plan.folder}/{plan.season}/{campaign.campaign_id}"
    digest = hashlib.sha1(seed.encode(), usedforsecurity=False).hexdigest()[:16]
    return f"{digest}@dkk-flightmanager.seamk.fi"


def _description(campaign: Campaign, tables: SeasonTables) -> str:
    """The body an operator reads on their phone: what, why, and how certain."""
    ct = tables.campaign_type(campaign.type_id)
    window = campaign.effective_window()
    lines: list[str] = []

    if ct is not None:
        if ct.purpose:
            lines.append(ct.purpose)
        if ct.decision_supported:
            lines.append(f"Decision: {ct.decision_supported}")
        lines.append(
            f"Acquisition: {ct.required_gsd_cm:.2f} cm/px, {ct.sensor}, "
            f"overlap {ct.overlap_front:.0%}/{ct.overlap_side:.0%}"
        )
    lines.append(f"Parcel(s): {', '.join(campaign.job_paths) or '—'}")

    if window is not None:
        lines.append(
            f"Target: {window.target.isoformat()} ({window.uncertainty_label()})"
        )
        if window.basis == "normal":
            lines.append(
                "Dates rest on a climatological normal beyond the forecast "
                "horizon and will move as the season's temperature sum diverges."
            )
        if window.confidence == "low":
            lines.append(
                "WINDOW UNCALIBRATED: the stage thresholds behind this date are "
                "agronomic rules of thumb, not measured values."
            )
    if ct is not None and (notice := ct.ground_sprayer_notice()):
        lines.append(notice)
    if campaign.manual_window is not None:
        lines.append(
            "Window set manually by the operator; the model's dates are ignored."
        )

    return "\n".join(lines)


def _summary(campaign: Campaign, tables: SeasonTables) -> str:
    ct = tables.campaign_type(campaign.type_id)
    parcel = campaign.job_paths[0].split("/")[-1] if campaign.job_paths else "?"
    label = ct.label_fi if ct else campaign.type_id
    return f"{label} — {parcel}"


def _event(
    plan: SeasonPlan, campaign: Campaign, tables: SeasonTables, now: _dt.datetime
) -> list[str] | None:
    window = campaign.effective_window()
    if window is None:
        return None
    ct = tables.campaign_type(campaign.type_id)

    return [
        "BEGIN:VEVENT",
        f"UID:{_uid(plan, campaign)}",
        f"DTSTAMP:{_stamp(now)}",
        # DTEND is exclusive for all-day events, so the last day needs +1.
        f"DTSTART;VALUE=DATE:{window.earliest.strftime('%Y%m%d')}",
        f"DTEND;VALUE=DATE:{(window.latest + _dt.timedelta(days=1)).strftime('%Y%m%d')}",
        f"SUMMARY:{_escape(_summary(campaign, tables))}",
        f"DESCRIPTION:{_escape(_description(campaign, tables))}",
        f"CATEGORIES:{_escape(campaign.type_id)}",
        # An uncalibrated window is a plan, not an appointment.
        f"STATUS:{'CONFIRMED' if campaign.state == 'scheduled' else 'TENTATIVE'}",
        "TRANSP:TRANSPARENT",
        f"PRIORITY:{_priority_value(ct.priority if ct else 'normal')}",
        "END:VEVENT",
    ]


def _priority_value(priority: str) -> int:
    """RFC 5545 priority: 1 is highest, 9 lowest, 0 undefined."""
    return {"high": 2, "normal": 5, "opportunistic": 8}.get(priority, 5)


def build_calendar(
    plan: SeasonPlan,
    tables: SeasonTables,
    *,
    states: Iterable[str] = _EXPORTABLE_STATES,
    now: _dt.datetime | None = None,
) -> str:
    """Render a folder's season plan as an iCalendar document.

    Returns text with CRLF line endings, ready to write to a ``.ics`` file or
    serve with ``text/calendar``.
    """
    now = now or _dt.datetime.now(tz=_dt.timezone.utc)
    wanted = set(states)

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{_PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_escape(f'{plan.folder} — kasvukausi {plan.season}')}",
        f"X-WR-CALDESC:{_escape(_calendar_description(plan, tables))}",
    ]
    for campaign in plan.campaigns:
        if campaign.state not in wanted:
            continue
        event = _event(plan, campaign, tables, now)
        if event:
            lines.extend(event)
    lines.append("END:VCALENDAR")

    return _CRLF.join(_fold(line) for line in lines) + _CRLF


def _calendar_description(plan: SeasonPlan, tables: SeasonTables) -> str:
    uncalibrated = sorted(
        {
            a.crop_id
            for a in plan.assignments
            if a.crop_id and a.crop_id in tables.uncalibrated_crops
        }
    )
    text = (
        f"Growing-season imaging windows for {plan.folder}, season {plan.season}. "
        f"Generated by dkk-flightmanager. Windows are recomputed as the season's "
        f"temperature sum diverges from normal — re-export after `season plan`."
    )
    if uncalibrated:
        text += (
            " Stage thresholds for " + ", ".join(uncalibrated) + " are "
            "UNCALIBRATED placeholders; treat every date as approximate."
        )
    return text


def event_count(calendar: str) -> int:
    """Number of VEVENTs in a rendered calendar — for tests and CLI output."""
    return calendar.count("BEGIN:VEVENT")
