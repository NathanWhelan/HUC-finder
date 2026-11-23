#!/usr/bin/env python3
"""
Pull georeferenced GBIF occurrence records for a list of taxa, optionally
restricted to a set of US states.

INPUT
  A text file with one taxon per line (blank lines and lines starting with
  '#' are ignored). A line can be a name, with or without its authority
  ("Physa" or "Physa Draparnaud, 1801"), or a GBIF taxon key (digits only),
  which is handy when a name is shared by several taxa. A .csv file also
  works: the first column is used and a header row is skipped.

WHAT IT DOES
  1. Matches every name against the GBIF Backbone Taxonomy and prints a
     message for any name that is:
       NOT_FOUND         no match at all (misspelling, bad/unknown taxonomy)
       FUZZY_SKIPPED     only a fuzzy match (probable misspelling); the closest
                         GBIF name is suggested. Use --accept-fuzzy to keep it.
       HIGHER_RANK_ONLY  e.g. the species is unknown but the genus matched
       AMBIGUOUS         the name belongs to several taxa (homonyms)
     For these, the exact GBIF candidates are listed with their class and
     key. Fix them with a hint (--class Gastropoda, --kingdom Animalia), by
     adding the authority to the name, or by putting the key in the list.
     Synonyms are resolved to the accepted name, so records filed under any
     name for that taxon are included; a note says when that widens the
     scope (e.g. a subspecies synonym pulls the whole species). Qualifiers
     such as cf., aff. and sp. are ignored by GBIF, and are flagged.
     If one name in the list contains another (a genus and one of its
     species), each record is written once, under the more specific name.
  2. Downloads only records that HAVE COORDINATES and are presence records
     (GBIF also holds absence records with coordinates). Without --states,
     records GBIF flags with geospatial issues (0,0 coordinates, point
     outside the stated country, etc.) are excluded too.
  3. With --states, checks each record's coordinates against US Census Bureau
     TIGER/Line state boundaries and drops records outside the listed states.
     The state comes from the coordinates only, never the stateProvince
     field. States are given as postal codes (OH, IN, ...); the script exits
     with an error before doing anything if a code is not valid.
       - The boundaries are each state's legal area, so they include water:
         Great Lakes waters (split between states, out to the Canadian
         border) and coastal state waters (3 nautical miles offshore; 9 for
         Texas and Florida's Gulf coast).
       - GBIF's geospatial-issue filter is not used, because coordinates
         decide: a point inside a chosen state is kept even if the
         publisher's country disagrees (e.g. a Lake Erie record labelled
         Canada that plots in Ohio's waters). Broken coordinates such as 0,0
         never fall inside a state. The issues column shows any GBIF flags.
       - --buffer-km (default 1 km) also keeps records that fall in no US
         state at all but are within that distance of a chosen state (just
         past the coast, or a slightly-off point). A record inside another
         state is never pulled in by the buffer, and buffered records must
         have a US (or US territory) countryCode in GBIF, so points in
         Canada/Mexico near the border are not picked up.
     The boundary file (~10 MB) is downloaded once and cached in
     us_state_boundaries/ next to this script. To cut download size, GBIF is
     only asked for records in the bounding box around the chosen states (plus
     the buffer); the polygon test then makes the exact cut.

TWO RETRIEVAL MODES
  search (default)  Pages through the occurrence search API. No account
                    needed, but GBIF caps paging at 100,000 records per query,
                    so a taxon above that is skipped with a message.
  --download        Uses the GBIF download API: no record cap, and you get a
                    citable DOI (GBIF asks you to cite it in publications).
                    Needs a free gbif.org account, read from the GBIF_USER,
                    GBIF_PWD and GBIF_EMAIL environment variables. GBIF queues
                    the request; it can take minutes to hours.

EXAMPLES
  python3 gbif_pull.py taxa.txt
  python3 gbif_pull.py taxa.txt --states OH IN MI --class Gastropoda
  python3 gbif_pull.py taxa.txt --states OH,KY,WV -o snails
  python3 gbif_pull.py taxa.txt --states MI --buffer-km 3
  GBIF_USER=me GBIF_PWD=secret GBIF_EMAIL=me@uni.edu \\
      python3 gbif_pull.py taxa.txt --states OH --download

OUTPUT (in --outdir, default gbif_output/)
  taxon_summary.csv  one row per input name: status, matched and accepted
                     names, GBIF key, counts, and any message
  occurrences.csv    the records, with an input_name column. Search mode:
                     chosen columns. Download mode: written only with
                     --states (all GBIF columns). With --states, two columns
                     are added: state_from_coords (state the point is in, or
                     nearest to if kept by the buffer) and km_outside_state
                     (0 if the point is inside the state, else its distance
                     outside).
  <key>.zip          download mode: the raw GBIF SIMPLE_CSV download
                     (tab-delimited despite the name)
  The exit status is 1 if any taxon failed with a network/API error.

KNOWN LIMITATION
  The GBIF query uses one bounding box around all chosen states. Alaska's
  islands cross the 180 degree line, so with AK the box spans all longitudes
  in its latitude band. Results are still correct, but many more records are
  fetched and checked, and a large taxon may hit the 100,000 search cap.

Requires: Python 3.8+, requests
"""
import argparse
import csv
import io
import math
import os
import re
import struct
import sys
import time
import zipfile

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API = "https://api.gbif.org/v1"
BACKBONE_KEY = "d7dddbf4-2cf0-4f39-9b2a-bb099caae36c"  # GBIF Backbone Taxonomy
SEARCH_CAP = 100_000  # occurrence search: offset + limit may not exceed this
PAGE_SIZE = 300       # maximum page size for occurrence search
POLL_SECONDS = 30     # download mode: how often to check the download status
TIMEOUT = 120         # seconds per HTTP request (GBIF can be slow)

# TIGER/Line (not the cartographic "cb" files): these include Great Lakes and
# coastal waters, where the cb files are clipped to the shoreline.
BOUNDARY_URL = "https://www2.census.gov/geo/tiger/TIGER2023/STATE/tl_2023_us_state.zip"
BOUNDARY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "us_state_boundaries")
DEFAULT_BUFFER_KM = 1.0
KM_PER_DEG_LAT = 110.574
KM_PER_DEG_LON_AT_EQUATOR = 111.320
# ISO country codes GBIF uses for US areas (territories have their own codes).
US_COUNTRY_CODES = {"US", "PR", "VI", "GU", "AS", "MP", "UM"}

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming",
    # Not states, but they have postal codes and are in the boundary file.
    "DC": "District of Columbia", "PR": "Puerto Rico", "VI": "U.S. Virgin Islands",
    "GU": "Guam", "AS": "American Samoa", "MP": "Northern Mariana Islands",
}

SYNONYM_STATUSES = {"SYNONYM", "HETEROTYPIC_SYNONYM", "HOMOTYPIC_SYNONYM",
                    "PROPARTE_SYNONYM", "MISAPPLIED"}
CLASSIFICATION_KEYS = ("kingdomKey", "phylumKey", "classKey", "orderKey",
                       "familyKey", "genusKey", "speciesKey")
INFRASPECIFIC_RANKS = {"SUBSPECIES", "VARIETY", "FORM", "INFRASPECIFIC_NAME"}
# Download mode: SIMPLE_CSV column holding each higher rank's name.
RANK_NAME_COLUMNS = {"GENUS": "genus", "FAMILY": "family", "ORDER": "order",
                     "CLASS": "class", "PHYLUM": "phylum", "KINGDOM": "kingdom"}
QUALIFIER = re.compile(r"(?:^|\s)(?:cf|aff|sp|spp|nr)\.?(?=\s|$)|\?", re.IGNORECASE)
MISMATCH_ISSUE = "COUNTRY_COORDINATE_MISMATCH"
HOW_TO_FIX = ("Fix with a hint (e.g. --class Gastropoda), by adding the authority to "
              "the name, or by putting the GBIF key on its own line in the list.")
HEADER_WORDS = {"name", "names", "taxon", "taxa", "species", "scientificname",
                "scientific_name", "taxon_name", "taxonname"}

# Occurrence fields written to occurrences.csv (search mode).
FIELDS = [
    "gbifID", "occurrenceID", "scientificName", "acceptedScientificName",
    "taxonRank", "taxonomicStatus", "taxonKey", "acceptedTaxonKey", "speciesKey",
    "kingdom", "phylum", "class", "order", "family", "genus", "species",
    "decimalLatitude", "decimalLongitude", "coordinateUncertaintyInMeters",
    "coordinatePrecision", "geodeticDatum", "country", "countryCode",
    "stateProvince", "locality", "eventDate", "year", "month", "day",
    "basisOfRecord", "occurrenceStatus", "individualCount", "recordedBy",
    "identifiedBy", "institutionCode", "collectionCode", "catalogNumber",
    "datasetKey", "datasetName", "publishingOrgKey", "license", "issues",
]
SUMMARY_FIELDS = ["input_name", "status", "match_type", "matched_name", "rank",
                  "taxonomic_status", "accepted_name", "accepted_rank", "gbif_key",
                  "gbif_record_count", "records_written", "records_kept_by_buffer",
                  "records_outside_states", "records_under_other_name", "message"]
STATE_COLUMNS = ["state_from_coords", "km_outside_state"]


def log(msg=""):
    print(msg, file=sys.stderr, flush=True)


def join_msgs(*parts):
    return "; ".join(p for p in parts if p)


def new_stats():
    """Per-taxon record counters. other_name = records skipped because they
    were already handled under a more specific name in the list."""
    return {"written": 0, "buffered": 0, "dropped": 0, "mismatch": 0,
            "other_name": 0, "other_name_written": 0, "retrieved": 0}


# --------------------------------------------------------------------------
# US state filtering
# --------------------------------------------------------------------------
def parse_states(values):
    """Turn ["OH,IN", "mi"] into ["OH", "IN", "MI"]; exit on any invalid code."""
    name_to_code = {name.lower(): code for code, name in US_STATES.items()}
    codes, bad = [], []
    for value in values:
        for token in value.split(","):
            token = token.strip()
            if not token:
                continue
            code = token.upper()
            if code in US_STATES:
                if code not in codes:
                    codes.append(code)
            else:
                hint = name_to_code.get(token.lower())
                bad.append(f"'{token}'" + (f" (did you mean {hint}?)" if hint else ""))
    if bad:
        sys.exit("Error: not a US state postal code abbreviation: " + ", ".join(bad)
                 + "\nValid codes: " + " ".join(sorted(US_STATES)))
    if not codes:
        sys.exit("Error: --states was given but no state codes were listed.")
    return codes


def read_dbf_column(data, field):
    """Read one column from a dBASE (.dbf) file, as a list of strings."""
    n_records, header_len, record_len = struct.unpack("<IHH", data[4:12])
    columns, pos, offset = {}, 32, 1  # offset 1 skips each record's deletion flag
    while data[pos] != 0x0D:
        name = data[pos:pos + 11].split(b"\0")[0].decode("ascii")
        length = data[pos + 16]
        columns[name] = (offset, length)
        offset += length
        pos += 32
    start, length = columns[field]
    values = []
    for i in range(n_records):
        rec = header_len + i * record_len
        values.append(data[rec + start:rec + start + length].decode("latin-1").strip())
    return values


def read_shp(data, wanted=()):
    """Read a polygon shapefile. Returns [(bbox, rings), ...] in record order.
    bbox is (xmin, ymin, xmax, ymax). rings (closed lists of (lon, lat)
    points) are parsed only for record numbers in `wanted`, else None."""
    records, pos = [], 100  # skip the 100-byte file header
    while pos + 8 <= len(data):
        _, content_words = struct.unpack(">ii", data[pos:pos + 8])
        content = data[pos + 8:pos + 8 + 2 * content_words]
        pos += 8 + 2 * content_words
        shape_type = struct.unpack("<i", content[:4])[0]
        if shape_type == 0:  # null shape
            records.append((None, None))
            continue
        if shape_type not in (5, 15, 25):
            raise ValueError(f"unexpected shapefile shape type {shape_type}")
        bbox = struct.unpack("<4d", content[4:36])
        rings = None
        if len(records) in wanted:
            n_parts, n_points = struct.unpack("<ii", content[36:44])
            parts = struct.unpack(f"<{n_parts}i", content[44:44 + 4 * n_parts])
            p0 = 44 + 4 * n_parts
            xy = struct.unpack(f"<{2 * n_points}d", content[p0:p0 + 16 * n_points])
            points = list(zip(xy[0::2], xy[1::2]))
            bounds = list(parts) + [n_points]
            rings = [points[bounds[i]:bounds[i + 1]] for i in range(n_parts)]
        records.append((bbox, rings))
    return records


def grow_bbox(bbox, km):
    """Expand a (xmin, ymin, xmax, ymax) lon/lat box by roughly km on each side."""
    xmin, ymin, xmax, ymax = bbox
    dlat = km / KM_PER_DEG_LAT
    coslat = max(0.01, math.cos(math.radians(min(89.0, max(abs(ymin), abs(ymax)) + dlat))))
    dlon = km / (KM_PER_DEG_LON_AT_EQUATOR * coslat)
    return (xmin - dlon, ymin - dlat, xmax + dlon, ymax + dlat)


def in_bbox(x, y, b):
    return b[0] <= x <= b[2] and b[1] <= y <= b[3]


def point_segment_km(ax, ay, bx, by):
    """Distance from the origin to segment A-B (coordinates already in km)."""
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / length2))
    return math.hypot(ax + t * dx, ay + t * dy)


class StatePolygon:
    """Point-in-polygon test and distance-to-boundary for one state (all its
    islands and holes).

    Uses the even-odd ray-casting rule over every ring. Edges are bucketed
    into latitude bands so each test only checks edges near the point."""

    def __init__(self, rings, n_bands=2000):
        xs = [x for ring in rings for x, _ in ring]
        ys = [y for ring in rings for _, y in ring]
        self.bbox = (min(xs), min(ys), max(xs), max(ys))
        self.ymin = self.bbox[1]
        self.n_bands = n_bands
        self.band_height = (self.bbox[3] - self.ymin) / n_bands or 1.0
        self.bands = [[] for _ in range(n_bands)]
        for ring in rings:
            for (x1, y1), (x2, y2) in zip(ring, ring[1:]):  # rings are closed
                for b in range(self._band(min(y1, y2)), self._band(max(y1, y2)) + 1):
                    self.bands[b].append((x1, y1, x2, y2))

    def _band(self, y):
        return min(self.n_bands - 1, max(0, int((y - self.ymin) / self.band_height)))

    def contains(self, x, y):
        if not in_bbox(x, y, self.bbox):
            return False
        inside = False
        for x1, y1, x2, y2 in self.bands[self._band(y)]:
            # Horizontal edges fail the first test, so never divide by zero.
            if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
        return inside

    def distance_km(self, x, y, max_km):
        """Distance in km from (x, y) to the nearest boundary edge, or None if
        it is farther than max_km. Uses a local flat-earth approximation,
        which is accurate for buffers of a few km."""
        kx = KM_PER_DEG_LON_AT_EQUATOR * math.cos(math.radians(y))
        dlat = max_km / KM_PER_DEG_LAT
        best = None
        for b in range(self._band(y - dlat), self._band(y + dlat) + 1):
            for x1, y1, x2, y2 in self.bands[b]:
                d = point_segment_km((x1 - x) * kx, (y1 - y) * KM_PER_DEG_LAT,
                                     (x2 - x) * kx, (y2 - y) * KM_PER_DEG_LAT)
                if d <= max_km and (best is None or d < best):
                    best = d
        return best


class StateFilter:
    def __init__(self, codes, session, buffer_km=DEFAULT_BUFFER_KM):
        self.buffer_km = buffer_km
        zip_path = os.path.join(BOUNDARY_DIR, os.path.basename(BOUNDARY_URL))
        if not os.path.exists(zip_path):
            log("Downloading US state boundaries (US Census Bureau TIGER/Line, ~10 MB)...")
            os.makedirs(BOUNDARY_DIR, exist_ok=True)
            r = session.get(BOUNDARY_URL, timeout=TIMEOUT)
            r.raise_for_status()
            with open(zip_path + ".part", "wb") as f:
                f.write(r.content)
            os.replace(zip_path + ".part", zip_path)
        stem = os.path.splitext(os.path.basename(BOUNDARY_URL))[0]
        with zipfile.ZipFile(zip_path) as z:
            postal = read_dbf_column(z.read(stem + ".dbf"), "STUSPS")
            shp = z.read(stem + ".shp")
        index = {code: i for i, code in enumerate(postal)}
        missing = [c for c in codes if c not in index]
        if missing:
            sys.exit(f"Error: no boundary found for {', '.join(missing)} in {zip_path}")

        # Each chosen state's bounding box, grown by the buffer.
        bboxes = [bbox for bbox, _ in read_shp(shp)]
        self.grown = {c: grow_bbox(bboxes[index[c]], buffer_km) for c in codes}
        # Other states close enough that a buffered point could lie inside
        # them; such a point belongs to that state and must not be pulled in.
        chosen = {index[c] for c in codes}
        neighbors = []
        if buffer_km > 0:
            neighbors = [i for i, b in enumerate(bboxes)
                         if b and i not in chosen and any(
                             b[0] <= g[2] and g[0] <= b[2] and b[1] <= g[3] and g[1] <= b[3]
                             for g in self.grown.values())]
        records = read_shp(shp, chosen | set(neighbors))
        self.polygons = {c: StatePolygon(records[index[c]][1]) for c in codes}
        self.other_states = [StatePolygon(records[i][1]) for i in neighbors]

        # Box around all chosen states plus the buffer, used to pre-filter
        # the GBIF query. Every point we could keep is inside it.
        g = list(self.grown.values())
        self.bbox = (max(-180.0, round(min(b[0] for b in g) - 0.01, 4)),
                     max(-90.0, round(min(b[1] for b in g) - 0.01, 4)),
                     min(180.0, round(max(b[2] for b in g) + 0.01, 4)),
                     min(90.0, round(max(b[3] for b in g) + 0.01, 4)))

    def locate(self, lon, lat, country=""):
        """Returns (postal code, km outside the state) for a point to keep:
        km is 0 for a point inside a chosen state, or its distance outside
        when kept by the buffer. Returns None for a point to drop."""
        try:
            x, y = float(lon), float(lat)
        except (TypeError, ValueError):
            return None
        for code, poly in self.polygons.items():
            if poly.contains(x, y):
                return code, 0.0
        # Buffer: only for points in no US state at all, and only if GBIF
        # puts the record in the US (keeps out Canada/Mexico near the border;
        # a blank country does not count as US).
        if self.buffer_km <= 0 or (country or "").upper() not in US_COUNTRY_CODES:
            return None
        near = [c for c, g in self.grown.items() if in_bbox(x, y, g)]
        if not near or any(o.contains(x, y) for o in self.other_states):
            return None
        best = None
        for code in near:
            d = self.polygons[code].distance_km(x, y, self.buffer_km)
            if d is not None and (best is None or d < best[1]):
                best = (code, d)
        return best


# --------------------------------------------------------------------------
# GBIF: names
# --------------------------------------------------------------------------
def make_session():
    s = requests.Session()
    # Retry GETs on rate limiting / server errors / timeouts. POSTs are never
    # retried, so a download request can't be submitted twice.
    retry = Retry(total=6, backoff_factor=2,
                  status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset(["GET"]),
                  respect_retry_after_header=True)
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers["User-Agent"] = "gbif-pull-script/1.0 (python-requests)"
    return s


def get_json(session, url, params=None):
    r = session.get(url, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def read_taxa(path):
    names = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        if path.lower().endswith(".csv"):
            rows = [row[0] if row else "" for row in csv.reader(f)]
            if rows and rows[0].strip().lower() in HEADER_WORDS:
                rows = rows[1:]
        else:
            rows = f.read().splitlines()
    for raw in rows:
        name = " ".join(raw.split())  # trim and collapse internal whitespace
        if name and not name.startswith("#"):
            names.append(name)
    return names


# The match API and the species API name the same fields differently.
def usage_key(u):
    return u.get("usageKey") or u.get("key")


def usage_status(u):
    return u.get("status") or u.get("taxonomicStatus") or ""


def accepted_key(u):
    return u.get("acceptedUsageKey") or u.get("acceptedKey")


def describe(u):
    """Short label, e.g. "Physa Draparnaud, 1801 (GENUS, Gastropoda, key 9037827)"."""
    group = u.get("class") or u.get("kingdom") or "?"
    status = usage_status(u)
    extra = f", {status}" if status and status != "ACCEPTED" else ""
    return f"{u.get('scientificName')} ({u.get('rank')}, {group}{extra}, key {usage_key(u)})"


def is_same_name(canonical, name):
    """True if `name` is exactly `canonical`, optionally followed by an
    authority ("Pomacea" or "Pomacea Perry, 1810", but not "Pomacea fakea")."""
    if not canonical:
        return False
    if name.lower() == canonical.lower():
        return True
    if not name.lower().startswith(canonical.lower() + " "):
        return False
    rest = name[len(canonical):].strip()
    return rest[:1].isupper() or rest[:1] == "("


def rank_specificity(row):
    """Sort key putting the most specific taxa first."""
    return -len(row.get("_ancestors", ()))


def lookup_key(session, key_text, row, notes):
    """Look up a GBIF taxon key given in the input list. Returns the usage,
    or None (with row status/message filled in) if it can't be used."""
    try:
        u = get_json(session, f"{API}/species/{key_text}")
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            row.update(status="NOT_FOUND", message=f"no GBIF taxon has key {key_text}")
            return None
        raise
    if u.get("datasetKey") != BACKBONE_KEY:
        if u.get("nubKey"):
            notes.append(f"key {key_text} is from another checklist; using its GBIF "
                         f"Backbone equivalent (key {u['nubKey']})")
            return get_json(session, f"{API}/species/{u['nubKey']}")
        row.update(status="NOT_FOUND", message=f"key {key_text} is not in the GBIF Backbone "
                                               f"Taxonomy, so occurrences can't be searched with it")
        return None
    return u


def resolve(session, name, hints, accept_fuzzy=False):
    """Match one input line to the GBIF Backbone. Returns a summary-row dict;
    'gbif_key' (the accepted taxon's key) is set only if the name is usable."""
    row = {"input_name": name, "status": "", "match_type": "", "matched_name": "",
           "rank": "", "taxonomic_status": "", "gbif_key": None, "message": ""}
    notes = []
    if QUALIFIER.search(name):
        notes.append("name contains a qualifier (cf., aff., sp., ?) that GBIF ignores; "
                     "matched on the rest of the name")

    if name.isdigit():
        usage = lookup_key(session, name, row, notes)
        if usage is None:
            return row
        row["match_type"] = "KEY"
    else:
        m = get_json(session, f"{API}/species/match", {"name": name, "verbose": "true", **hints})
        mt = m.get("matchType", "NONE")
        row["match_type"] = mt
        alts = m.get("alternatives", [])
        exact = [a for a in alts if a.get("matchType") == "EXACT"][:4]
        candidates = (" Exact GBIF matches: " + "; ".join(describe(a) for a in exact)
                      + ". " + HOW_TO_FIX) if exact else ""

        if mt == "NONE":
            if "Multiple equal matches" in (m.get("note") or ""):
                row["status"] = "AMBIGUOUS"
                row["message"] = "name belongs to several different taxa." + (candidates or " " + HOW_TO_FIX)
            else:
                row["status"] = "NOT_FOUND"
                row["message"] = "no match in the GBIF Backbone Taxonomy. Check spelling/taxonomy."
                if exact:
                    row["message"] += candidates
                elif alts:
                    row["message"] += " Closest GBIF names: " + "; ".join(describe(a) for a in alts[:3])
            return row

        if mt == "HIGHERRANK":
            if is_same_name(m.get("canonicalName"), name):
                # e.g. "Potamopyrgus": GBIF says HIGHERRANK, but it matched the
                # very genus that was asked for.
                notes.append(f"GBIF labelled this a higher-rank match, but it is the "
                             f"{(m.get('rank') or '').lower()} you named")
            else:
                row["status"] = "HIGHER_RANK_ONLY"
                row["message"] = (f"not found at the requested rank; GBIF could only match the "
                                  f"{(m.get('rank') or '').lower()} '{m.get('scientificName')}'. "
                                  f"Not pulled." + (candidates or " Check spelling/taxonomy."))
                return row

        if mt == "FUZZY":
            if not accept_fuzzy:
                row["status"] = "FUZZY_SKIPPED"
                row["message"] = (f"no exact match, probable misspelling. Did you mean "
                                  f"{describe(m)}? Fix the name, or re-run with --accept-fuzzy.")
                return row
            notes.append(f"fuzzy match accepted: using {describe(m)}")
        usage = m

    key, status = usage_key(usage), usage_status(usage)
    row.update(matched_name=usage.get("scientificName", ""), rank=usage.get("rank", ""),
               taxonomic_status=status)
    accepted = usage
    if status in SYNONYM_STATUSES and accepted_key(usage):
        key = accepted_key(usage)
        accepted = get_json(session, f"{API}/species/{key}")
        notes.append(f"synonym of '{accepted.get('scientificName')}'; using the accepted taxon (key {key})")
        if status == "PROPARTE_SYNONYM":
            notes.append("pro parte synonym: GBIF maps it to only one of the taxa it was "
                         "split into; check that is the one you want")
        if accepted.get("rank") != usage.get("rank"):
            notes.append(f"the accepted taxon is a {(accepted.get('rank') or '?').lower()}, "
                         f"so records for all of it are pulled")
    elif status == "DOUBTFUL":
        notes.append("GBIF flags this name as DOUBTFUL; check the taxonomy")

    row.update(status="OK", gbif_key=key, accepted_name=accepted.get("scientificName", ""),
               accepted_rank=accepted.get("rank", ""), message=join_msgs(*notes))
    # Internal fields (not written out): used to spot names nested inside
    # other names, and to attribute download records to names.
    row["_canonical"] = accepted.get("canonicalName", "")
    row["_ancestors"] = {accepted.get(k) for k in CLASSIFICATION_KEYS} - {None, key}
    return row


# --------------------------------------------------------------------------
# GBIF: occurrences
# --------------------------------------------------------------------------
def occurrence_filters(geo_issue_filter, bbox=None):
    """Query parameters for the occurrence search API."""
    f = {"hasCoordinate": "true", "occurrenceStatus": "PRESENT"}
    if geo_issue_filter:
        f["hasGeospatialIssue"] = "false"
    if bbox:
        xmin, ymin, xmax, ymax = bbox
        f["decimalLatitude"] = f"{ymin},{ymax}"
        f["decimalLongitude"] = f"{xmin},{xmax}"
    return f


def count_records(session, key, filters):
    return get_json(session, f"{API}/occurrence/search",
                    {**filters, "taxonKey": key, "limit": 0})["count"]


def pull_records(session, key, filters, writer, input_name, total, state_filter, seen, stats):
    """Page through occurrence search for one taxon, writing rows as they
    arrive. `seen` maps gbifID -> whether it was written, for every record
    already handled under an earlier (more specific) name; it is shared
    across taxa so no record is written twice. `stats` is updated as rows
    are written, so it stays correct even if this raises partway through."""
    seen_here = set()
    offset = 0
    show_progress = sys.stderr.isatty()
    while offset < min(total, SEARCH_CAP):
        limit = min(PAGE_SIZE, SEARCH_CAP - offset)
        page = get_json(session, f"{API}/occurrence/search",
                        {**filters, "taxonKey": key, "limit": limit, "offset": offset})
        results = page.get("results", [])
        for rec in results:
            gid = rec["key"]
            if gid in seen_here:  # paging over a live index can repeat records
                continue
            seen_here.add(gid)
            stats["retrieved"] += 1
            if gid in seen:
                stats["other_name"] += 1
                stats["other_name_written"] += seen[gid]
                continue
            issues = rec.get("issues", [])
            row = {f: rec.get(f, "") for f in FIELDS}
            row["issues"] = ";".join(issues)
            if state_filter:
                hit = state_filter.locate(rec.get("decimalLongitude"), rec.get("decimalLatitude"),
                                          rec.get("countryCode"))
                if hit is None:
                    seen[gid] = False
                    stats["dropped"] += 1
                    continue
                row["state_from_coords"], km = hit
                row["km_outside_state"] = round(km, 3)
                stats["buffered"] += km > 0
            writer.writerow({"input_name": input_name, "query_taxon_key": key, **row})
            seen[gid] = True
            stats["written"] += 1
            stats["mismatch"] += MISMATCH_ISSUE in issues
        offset += len(results)
        if show_progress:
            print(f"\r      {len(seen_here):,} / {total:,} checked", end="", file=sys.stderr, flush=True)
        if page.get("endOfRecords") or not results:
            break
    if show_progress:
        print("\r" + " " * 50 + "\r", end="", file=sys.stderr)


def gbif_credentials():
    creds = tuple(os.environ.get(v) for v in ("GBIF_USER", "GBIF_PWD", "GBIF_EMAIL"))
    if not all(creds):
        sys.exit("Error: --download needs the GBIF_USER, GBIF_PWD and GBIF_EMAIL environment "
                 "variables (free account at https://www.gbif.org).")
    return creds


def request_download(session, creds, keys, geo_issue_filter, bbox, outdir):
    """Submit one GBIF download for all keys, wait for it, and save the zip."""
    user, pwd, email = creds
    predicates = [
        {"type": "in", "key": "TAXON_KEY", "values": [str(k) for k in keys]},
        {"type": "equals", "key": "HAS_COORDINATE", "value": "true"},
        {"type": "equals", "key": "OCCURRENCE_STATUS", "value": "PRESENT"},
    ]
    if geo_issue_filter:
        predicates.append({"type": "equals", "key": "HAS_GEOSPATIAL_ISSUE", "value": "false"})
    if bbox:
        xmin, ymin, xmax, ymax = bbox
        predicates += [
            {"type": "greaterThanOrEquals", "key": "DECIMAL_LATITUDE", "value": str(ymin)},
            {"type": "lessThanOrEquals", "key": "DECIMAL_LATITUDE", "value": str(ymax)},
            {"type": "greaterThanOrEquals", "key": "DECIMAL_LONGITUDE", "value": str(xmin)},
            {"type": "lessThanOrEquals", "key": "DECIMAL_LONGITUDE", "value": str(xmax)},
        ]
    body = {"creator": user, "notificationAddresses": [email], "sendNotification": True,
            "format": "SIMPLE_CSV",
            "predicate": {"type": "and", "predicates": predicates}}

    r = session.post(f"{API}/occurrence/download/request", json=body,
                     auth=(user, pwd), timeout=TIMEOUT)
    if not r.ok:
        sys.exit(f"Download request failed (HTTP {r.status_code}): {r.text.strip()[:500]}")
    dkey = r.text.strip()
    log(f"\nDownload requested: {dkey}")
    log(f"  Track it at https://www.gbif.org/occurrence/download/{dkey}")
    log("  (If you stop this script, the download still completes at GBIF and you get an email.)")

    while True:
        meta = get_json(session, f"{API}/occurrence/download/{dkey}")
        status = meta.get("status")
        if status == "SUCCEEDED":
            break
        if status in ("FAILED", "KILLED", "CANCELLED", "FILE_ERASED"):
            sys.exit(f"GBIF download {dkey} ended with status {status}.")
        log(f"  {time.strftime('%H:%M:%S')}  status: {status}")
        time.sleep(POLL_SECONDS)

    zip_path = os.path.join(outdir, f"{dkey}.zip")
    log(f"  Done: {meta.get('totalRecords', 0):,} records. Saving {zip_path} ...")
    with session.get(meta["downloadLink"], stream=True, timeout=TIMEOUT) as resp:
        resp.raise_for_status()
        with open(zip_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                f.write(chunk)
    return dkey, meta, zip_path


def filter_download(zip_path, out_path, state_filter, taxa):
    """Copy records from a GBIF SIMPLE_CSV zip whose coordinates fall in the
    chosen states (or within the buffer) into a regular CSV, tagging each with
    the most specific name in `taxa` (sorted most specific first) it belongs
    to. Returns (per-taxon stats keyed by GBIF key, overall totals)."""
    per_taxon = {t["gbif_key"]: new_stats() for t in taxa}
    totals = {**new_stats(), "malformed": 0, "unattributed": 0}
    with zipfile.ZipFile(zip_path) as z:
        member = next(n for n in z.namelist() if n.endswith(".csv"))
        with z.open(member) as raw, open(out_path, "w", newline="", encoding="utf-8") as out:
            # SIMPLE_CSV is tab-separated with no quoting and "\n" line ends.
            # Split on "\n" only, so a stray "\r" inside a field can't break a row.
            lines = io.TextIOWrapper(raw, encoding="utf-8", newline="\n")
            header = next(lines).rstrip("\r\n").split("\t")
            col = {name: i for i, name in enumerate(header)}
            if "decimalLatitude" not in col or "decimalLongitude" not in col:
                sys.exit(f"Error: {member} has no decimalLatitude/decimalLongitude columns.")

            def value(rec, column):
                i = col.get(column)
                return rec[i] if i is not None else ""

            def attribute(rec):
                for t in taxa:
                    rank, key = t.get("accepted_rank"), str(t["gbif_key"])
                    if rank == "SPECIES":
                        hit = value(rec, "speciesKey") == key
                    elif rank in INFRASPECIFIC_RANKS:
                        hit = value(rec, "taxonKey") == key
                    elif rank in RANK_NAME_COLUMNS:
                        hit = value(rec, RANK_NAME_COLUMNS[rank]) == t["_canonical"]
                    else:
                        hit = False
                    if hit:
                        return t
                return taxa[0] if len(taxa) == 1 else None

            writer = csv.writer(out)
            writer.writerow(["input_name"] + header + STATE_COLUMNS)
            for line in lines:
                if not line.strip():
                    continue
                rec = line.rstrip("\r\n").split("\t")
                if len(rec) != len(header):
                    totals["malformed"] += 1
                    continue
                taxon = attribute(rec)
                counters = [totals] + ([per_taxon[taxon["gbif_key"]]] if taxon else [])
                if taxon is None:
                    totals["unattributed"] += 1
                hit = state_filter.locate(value(rec, "decimalLongitude"), value(rec, "decimalLatitude"),
                                          value(rec, "countryCode"))
                if hit is None:
                    for c in counters:
                        c["dropped"] += 1
                    continue
                writer.writerow([taxon["input_name"] if taxon else ""] + rec
                                + [hit[0], round(hit[1], 3)])
                mismatch = MISMATCH_ISSUE in value(rec, "issue").split(";")
                for c in counters:
                    c["written"] += 1
                    c["buffered"] += hit[1] > 0
                    c["mismatch"] += mismatch
    return per_taxon, totals


def apply_stats(row, stats, states):
    """Copy a taxon's record counts into its summary row."""
    row["records_written"] = stats["written"]
    row["records_under_other_name"] = stats["other_name"]
    if states:
        row["records_outside_states"] = stats["dropped"]
        row["records_kept_by_buffer"] = stats["buffered"]
        # A name containing other listed names (a genus whose species are
        # also listed) may legitimately end up with none under its own name.
        if (row["status"] == "OK" and stats["written"] == 0
                and stats["other_name_written"] == 0 and not row.get("_contains")):
            row["status"] = "NO_RECORDS_IN_STATES"
            row["message"] = join_msgs(row["message"], f"{stats['dropped']:,} records near, but "
                                                       f"none inside, {', '.join(states)}")


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="Pull georeferenced GBIF occurrences for a list of taxa.",
        epilog="See the docstring at the top of this file for details.")
    ap.add_argument("taxa_file", help="text file, one taxon name or GBIF key per line (or a .csv, first column)")
    ap.add_argument("-o", "--outdir", default="gbif_output", help="output folder (default: gbif_output)")
    ap.add_argument("--states", nargs="+", metavar="XX",
                    help="keep only records whose coordinates fall in these US states, given as "
                         "postal codes: --states OH IN MI  or  --states OH,IN,MI")
    ap.add_argument("--buffer-km", type=float, default=None, metavar="KM",
                    help="with --states: also keep records in no US state but within this many "
                         f"km of a chosen state, e.g. just offshore (default {DEFAULT_BUFFER_KM:g}; "
                         "0 turns it off)")
    ap.add_argument("--class", dest="class_name", metavar="CLASS",
                    help="class hint for name matching, e.g. Gastropoda (sorts out names shared "
                         "with insects, plants, etc.)")
    ap.add_argument("--kingdom", help="kingdom hint for name matching, e.g. Plantae, Animalia, Fungi")
    ap.add_argument("--accept-fuzzy", action="store_true",
                    help="use fuzzy (probably misspelled) matches instead of skipping them")
    ap.add_argument("--include-geo-issues", action="store_true",
                    help="keep records GBIF flags with geospatial issues (with --states this is "
                         "always the case, since coordinates decide)")
    ap.add_argument("--download", action="store_true",
                    help="use the GBIF download API (no 100k cap, citable DOI; needs an account)")
    args = ap.parse_args()

    # ---- Check everything that can fail before doing any work --------------
    states = parse_states(args.states) if args.states else None  # exits on a bad code
    if args.buffer_km is not None and not states:
        log("Warning: --buffer-km only applies with --states; ignoring it.")
    buffer_km = DEFAULT_BUFFER_KM if args.buffer_km is None else args.buffer_km
    if not 0 <= buffer_km <= 100:
        sys.exit("Error: --buffer-km must be between 0 and 100.")
    creds = gbif_credentials() if args.download else None
    names = read_taxa(args.taxa_file)
    if not names:
        sys.exit(f"No taxon names found in {args.taxa_file}")
    os.makedirs(args.outdir, exist_ok=True)
    session = make_session()
    hints = {k: v for k, v in (("kingdom", args.kingdom), ("class", args.class_name)) if v}

    state_filter = None
    if states:
        state_filter = StateFilter(states, session, buffer_km)
        log(f"Keeping only records whose coordinates fall in: {', '.join(states)} "
            f"(including state waters; buffer {buffer_km:g} km)\n")
    bbox = state_filter.bbox if state_filter else None
    # With --states the coordinates decide, so GBIF's geospatial-issue flag
    # isn't used (it would drop e.g. Canada-labelled points in Ohio waters).
    geo_issue_filter = not args.include_geo_issues and not states
    filters = occurrence_filters(geo_issue_filter, bbox)

    # ---- Step 1: resolve names and count records --------------------------
    log(f"Matching {len(names)} names against the GBIF Backbone Taxonomy...\n")
    area = " in the states' bounding box" if states else ""
    rows, key_owner = [], {}
    for i, name in enumerate(names, 1):
        try:
            row = resolve(session, name, hints, args.accept_fuzzy)
            key = row["gbif_key"]
            if key is not None:
                if key in key_owner:
                    row["status"] = "DUPLICATE"
                    row["message"] = join_msgs(row["message"], f"same GBIF taxon as "
                                               f"'{key_owner[key]}' (key {key}); not pulled twice")
                else:
                    n = count_records(session, key, filters)
                    key_owner[key] = name
                    row["gbif_record_count"] = n
                    if n == 0:
                        row["status"] = "NO_RECORDS"
                        row["message"] = join_msgs(
                            row["message"], f"taxon exists but has no records matching the filters{area}")
                    elif n > SEARCH_CAP and not args.download:
                        row["status"] = "OVER_SEARCH_LIMIT"
                        row["message"] = join_msgs(
                            row["message"],
                            f"{n:,} records{area} is over the search API's {SEARCH_CAP:,} cap; "
                            f"re-run with --download to get them all")
        except requests.RequestException as e:
            row = {"input_name": name, "status": "ERROR", "message": f"request failed: {e}"}
        rows.append(row)

        label = f"[{i}/{len(names)}] {name}"
        if row["status"] == "OK":
            log(f"{label} -> {row['accepted_name']} (key {row['gbif_key']}): "
                f"{row['gbif_record_count']:,} records{area}"
                + (f"\n      note: {row['message']}" if row["message"] else ""))
        else:
            log(f"{label}\n  !! {row['status']}: {row['message']}")

    # Names inside other names in the list (e.g. a species and its genus):
    # pull the most specific first, so each record goes under that name.
    ok_rows = [r for r in rows if r["status"] == "OK"]
    nested = [(inner, outer) for inner in ok_rows for outer in ok_rows
              if outer is not inner and outer["gbif_key"] in inner["_ancestors"]]
    if nested:
        log("\nOverlapping names (each record is written once, under the more specific name):")
        for inner, outer in nested:
            log(f"  '{inner['input_name']}' is inside '{outer['input_name']}'")
            inner["message"] = join_msgs(inner["message"], f"inside '{outer['input_name']}' (also in "
                                         f"your list); shared records are written under this name")
            outer["message"] = join_msgs(outer["message"], f"contains '{inner['input_name']}' (also in "
                                         f"your list); records written under that name are not repeated here")
            outer["_contains"] = True
    ok_rows.sort(key=rank_specificity)

    # ---- Step 2: pull records (and drop those outside the states) ---------
    dl = None
    totals = None
    occ_path = os.path.join(args.outdir, "occurrences.csv")
    if not ok_rows:
        log("\nNo usable taxa, nothing to pull.")
    elif args.download:
        dl = request_download(session, creds, [r["gbif_key"] for r in ok_rows],
                              geo_issue_filter, bbox, args.outdir)
        if state_filter:
            log(f"  Checking coordinates against state boundaries -> {occ_path}")
            per_taxon, totals = filter_download(dl[2], occ_path, state_filter, ok_rows)
            for r in ok_rows:
                apply_stats(r, per_taxon[r["gbif_key"]], states)
    else:
        log(f"\nPulling records for {len(ok_rows)} taxa -> {occ_path}")
        columns = ["input_name", "query_taxon_key"] + FIELDS + (STATE_COLUMNS if states else [])
        seen = {}
        totals = new_stats()
        with open(occ_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=columns)
            writer.writeheader()
            for r in ok_rows:
                log(f"  {r['input_name']} ...")
                stats = new_stats()
                try:
                    pull_records(session, r["gbif_key"], filters, writer, r["input_name"],
                                 r["gbif_record_count"], state_filter, seen, stats)
                    expected = min(r["gbif_record_count"], SEARCH_CAP)
                    if stats["retrieved"] != expected:
                        warning = (f"GBIF reported {expected:,} records but {stats['retrieved']:,} "
                                   f"were retrieved (GBIF's index probably changed during paging)")
                        r["message"] = join_msgs(r["message"], warning)
                        log(f"  !! {r['input_name']}: {warning}")
                except requests.RequestException as e:
                    r["status"] = "ERROR_PARTIAL" if stats["written"] else "ERROR"
                    r["message"] = join_msgs(r["message"], f"failed partway through: {e}; "
                                             f"{stats['written']:,} records were written before the "
                                             f"failure. Re-run for complete data")
                    log(f"  !! ERROR pulling {r['input_name']}: {e}")
                apply_stats(r, stats, states)
                for k in totals:
                    totals[k] += stats[k]

    # ---- Step 3: summary --------------------------------------------------
    summary_path = os.path.join(args.outdir, "taxon_summary.csv")
    with open(summary_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SUMMARY_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    problems = [r for r in rows if r["status"] != "OK"]
    noted = [r for r in rows if r["status"] == "OK" and r.get("message")]
    log("\n" + "=" * 70)
    log(f"{len(names)} names in list, {len(rows) - len(problems)} OK, "
        f"{len(problems)} with problems")
    if dl:
        dkey, meta, zip_path = dl
        log(f"GBIF download: {meta.get('totalRecords', 0):,} records in {zip_path}")
        if meta.get("doi"):
            log(f"Cite this download as: GBIF.org ({time.strftime('%d %B %Y')}) "
                f"GBIF Occurrence Download https://doi.org/{meta['doi']}")
    if totals:
        log(f"Records written: {totals['written']:,} -> {occ_path}")
        if states:
            log(f"  kept by the {buffer_km:g} km buffer: {totals['buffered']:,}")
            log(f"  kept despite GBIF country mismatch (inside a chosen state by coordinates): "
                f"{totals['mismatch']:,}")
            log(f"Records dropped (coordinates outside {', '.join(states)}): {totals['dropped']:,}")
        if totals.get("malformed"):
            log(f"Malformed rows in the GBIF file, skipped (see the zip): {totals['malformed']:,}")
        if totals.get("unattributed"):
            log(f"Records not matched to a name in your list (input_name blank): "
                f"{totals['unattributed']:,}")
    log(f"Per-taxon summary: {summary_path}")
    if noted:
        log("\nNotes on matched names:")
        for r in noted:
            log(f"  {r['input_name']}: {r['message']}")
    if problems:
        log("\nNames needing attention:")
        for r in problems:
            log(f"  {r['status']:<20} {r['input_name']}: {r['message']}")
    sys.exit(1 if any(r["status"] in ("ERROR", "ERROR_PARTIAL") for r in rows) else 0)


if __name__ == "__main__":
    main()
