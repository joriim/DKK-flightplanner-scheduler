"""Growing-season flight planning — *kasvukauden kuvaussuunnittelu*.

The rest of ``dkk-flightmanager`` answers "how do I fly this parcel safely?".
This module answers the orthogonal question: **"when in the growing season
should I fly this parcel, and what for?"**

It turns a folder of parcel jobs plus crop and sowing information into a season
plan: an ordered set of *campaigns*, each a phenology-triggered flight window
with its own purpose, required GSD and sensor.

Deliberately out of scope
-------------------------
**Spraying.**  Aerial application of plant protection products is prohibited in
Finland and across the EU (Directive 2009/128/EC Art. 9; Tukes treats drones as
aerial vehicles).  Campaigns whose purpose is spraying-related produce a map for
a **ground sprayer**, never a spray route.  Every surface states this.

**Image processing.**  The module plans acquisition; WebODM / Metashape / Pix4D
do the rest.  It may record what was produced, but it produces none of it.

**Agronomic recommendations.**  It says "fly now to see X"; it never says
"apply 40 kg N/ha".

Layout
------
``models``           Pydantic types for crops, campaign types, campaigns, plans
``config``           the ``[season]`` table and the crop/campaign libraries
``phenology``        thermal time → stage timeline with an uncertainty band
``weather_history``  archive + forecast + 30-year normals, long-TTL cached
``gsd``              campaign GSD requirement → altitude and strip geometry
``campaigns``        campaign instantiation and window derivation
``store``            atomic, versioned ``season_<year>.json``
``planner``          init / recompute / status orchestration
``integration``      the narrow, read-only seam onto the host application
``cli`` / ``api`` / ``mcp_tools``   the three surfaces

Never imports ``pipeline`` and never takes the pipeline lock: season planning is
cheap and must not queue behind a running export.
"""

from __future__ import annotations

__all__ = [
    "SCHEMA_VERSION",
    "Campaign",
    "CampaignType",
    "CropProfile",
    "SeasonPlan",
    "Window",
]

from flightmanager.season.models import (  # noqa: E402
    SCHEMA_VERSION,
    Campaign,
    CampaignType,
    CropProfile,
    SeasonPlan,
    Window,
)
