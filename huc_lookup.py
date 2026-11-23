#!/usr/bin/env python
"""Assign USGS Hydrologic Unit Codes (HUCs) to specimen records from lat/lon.

Reads a table of records (.txt/.tsv/.csv/.xlsx/.xls), finds the latitude and
longitude columns, looks each point up in the USGS Watershed Boundary Dataset
(WBD), and writes the same table back out with HUC code/name columns and a
note column appended. Every input row appears in the output, in the same order.

Examples
--------
    python huc_lookup.py sample_data.txt                      # HUC8 (default)
    python huc_lookup.py records.xlsx --levels 4,8,12
    python huc_lookup.py records.csv --summarize-by species
    python huc_lookup.py --download                           # fetch the WBD once

Coordinates are assumed to be WGS84/NAD83 decimal degrees (simple DMS strings
such as 35°56'12"N are also accepted). The WBD covers the U.S. and its
territories, plus the parts of shared watersheds that extend into Canada and
Mexico; points elsewhere get no HUC.
"""

import argparse
import csv
import io
import math
import os
import re
import sys
import urllib.request
import warnings
import zipfile
from datetime import date
from pathlib import Path

import pyproj

# Without `conda activate`, the GDAL bundled with pyogrio cannot find PROJ's
# database (e.g. when called by full path from a SLURM script); point it there
# before pyogrio loads.
os.environ.setdefault("PROJ_DATA", pyproj.datadir.get_data_dir())
if os.path.isdir(os.path.join(sys.prefix, "share", "gdal")):
    os.environ.setdefault("GDAL_DATA", os.path.join(sys.prefix, "share", "gdal"))

import geopandas as gpd  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pyogrio  # noqa: E402

WBD_URL = ("https://prd-tnm.s3.amazonaws.com/StagedProducts/Hydrography/"
           "WBD/National/GDB/WBD_National_GDB.zip")
DATA_DIR = Path(__file__).resolve().parent / "data" / "wbd"
DEFAULT_WBD = DATA_DIR / "WBD_National_GDB.gdb"
SOURCE_FILE = "WBD_SOURCE.txt"

LEVELS = (2, 4, 6, 8, 10, 12)
# HU10/HU12 have ~19k/~103k polygons; reading all of HU12 takes ~7 GB of RAM,
# so for those levels points are grouped into CHUNK_DEGREES cells and each cell
# is read separately. (The national GDB's spatial indexes are not usable by
# GDAL, so each read scans the layer -- about a second -- and many small cells
# would be slow.)
CHUNK_FINER_THAN = 8
CHUNK_DEGREES = 5

# GDAL notices that are expected with the national WBD and not actionable.
warnings.filterwarnings("ignore", message=r"Cannot use .*\.spx", category=RuntimeWarning)
warnings.filterwarnings("ignore", message=r"organizePolygons\(\)", category=RuntimeWarning)

EXCEL_EXTS = {".xlsx", ".xlsm", ".xls"}
TEXT_EXTS = {".txt", ".tsv", ".tab", ".csv"}

# Accepted column headings, compared after lowercasing and dropping every
# non-alphanumeric character ("Lat_DD" -> "latdd"). Earlier tiers win; within a
# tier exactly one column must match, otherwise the user must pick one.
LAT_TIERS = [
    ["decimallatitude"],
    ["latitude", "lat", "decimallat", "declat", "latdec", "latdecimal",
     "latdd", "latitudedd", "latitudedecimal", "latitudedecimaldegrees",
     "ddlat", "gpslatitude", "gpslat", "latwgs84", "latitudewgs84",
     "latnad83", "latitudenad83", "sitelatitude", "sitelat", "latn",
     "latituden"],
    ["verbatimlatitude", "verbatimlat"],
    ["y", "ycoord", "ycoordinate"],
]
LON_TIERS = [
    ["decimallongitude"],
    ["longitude", "long", "lon", "lng", "decimallon", "decimallong",
     "decimallng", "declon", "declong", "declng", "londec", "longdec",
     "londecimal", "longdecimal", "londd", "longdd", "lngdd", "longitudedd",
     "longitudedecimal", "longitudedecimaldegrees", "ddlon", "ddlong",
     "gpslongitude", "gpslong", "gpslon", "lonwgs84", "longwgs84",
     "longitudewgs84", "lonnad83", "longnad83", "longitudenad83",
     "sitelongitude", "sitelong", "sitelon", "longw", "lonw", "longitudew"],
    ["verbatimlongitude", "verbatimlong", "verbatimlon"],
    ["x", "xcoord", "xcoordinate"],
]


class HucError(Exception):
    """A problem worth reporting to the user without a traceback."""


def log(msg=""):
    print(msg, file=sys.stderr)


# --------------------------------------------------------------------------
# Reading and writing tables
# --------------------------------------------------------------------------

def _sniff_text(path):
    """Return (text, encoding, delimiter, line terminator) for a delimited file."""
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    # A BOM-less UTF-8 file is written back without a BOM.
    if enc == "utf-8-sig" and not raw.startswith(b"\xef\xbb\xbf"):
        enc = "utf-8"
    header = next((ln for ln in text.splitlines() if ln.strip()), "")
    counts = {d: header.count(d) for d in ("\t", ",", ";", "|")}
    delim = max(counts, key=counts.get)
    if counts[delim] == 0:
        delim = "," if path.suffix.lower() == ".csv" else "\t"
    newline = "\r\n" if b"\r\n" in raw[:100_000] else "\n"
    return text, enc, delim, newline


def _drop_trailing_empty_rows(df):
    """Drop all-empty rows at the end of the table (blank lines, stray tabs)."""
    filled = (df.fillna("").apply(lambda col: col.astype(str).str.strip()) != "").any(axis=1)
    rows = np.flatnonzero(filled.values)
    return df.iloc[: rows[-1] + 1 if len(rows) else 0]


def _bad_field_counts(text, delim, quoting):
    """Records whose field count does not fit the header.

    Returns (header field count, [(line number, field count), ...]), or None if
    the text cannot be split with this quoting. Blank records are fine, and so
    are extra fields that are empty (a trailing delimiter); a record with too
    few fields, or extra non-empty ones, would put values in the wrong column.
    """
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delim, quoting=quoting)
    ncols, bad = None, []
    try:
        for rec in reader:
            if all(f.strip() == "" for f in rec):
                continue
            if ncols is None:
                ncols = len(rec)
            elif len(rec) < ncols or any(f.strip() for f in rec[ncols:]):
                bad.append((reader.line_num, len(rec)))
    except csv.Error:
        return None
    return ncols, bad


def read_table(path, sheet=None):
    """Read the input table. Returns (DataFrame, format info dict)."""
    path = Path(path)
    if not path.is_file():
        raise HucError(f"Input file not found: {path}")
    ext = path.suffix.lower()
    if ext in EXCEL_EXTS:
        # dtype=object keeps the cell values as Excel stored them (numbers,
        # dates, text), so the output round-trips cleanly; keep_default_na=False
        # stops text such as "NA" from being turned into blanks.
        try:
            df = pd.read_excel(path, sheet_name=sheet if sheet is not None else 0,
                               dtype=object, keep_default_na=False)
        except ValueError as e:
            raise HucError(f"Could not read sheet {sheet!r} from {path}: {e}")
        df = _drop_trailing_empty_rows(df)
        info = {"kind": "excel", "desc": "Excel"
                + (f", sheet {sheet!r}" if sheet is not None else ", first sheet")}
    else:
        text, enc, delim, newline = _sniff_text(path)
        # Tab/pipe files are read without quote processing: a stray " in a
        # locality would otherwise swallow the following lines into one row.
        # (Excel's quoted fields then pass through verbatim; GBIF downloads
        # are meant to be read this way.) CSV needs quote processing.
        quoting = csv.QUOTE_MINIMAL if delim in ",;" else csv.QUOTE_NONE
        # Every record must have as many fields as the header; otherwise
        # values (and HUCs) would land in the wrong column or row silently.
        checked = _bad_field_counts(text, delim, quoting)
        multiline = False
        if checked and checked[1] and quoting == csv.QUOTE_NONE:
            # Excel writes a cell containing a line break as "two\nlines";
            # without quote processing that one record becomes several short
            # rows. If quote processing makes every record fit, use it.
            quoted = _bad_field_counts(text, delim, csv.QUOTE_MINIMAL)
            if quoted and not quoted[1]:
                quoting, checked, multiline = csv.QUOTE_MINIMAL, quoted, True
                log(f"Note: {path.name} has quoted cells containing line breaks (as "
                    "Excel exports them); reading it with quote processing so each "
                    "record stays one row, as in the spreadsheet.")
        if checked and checked[1]:
            ncols, bad = checked
            shown = ", ".join(f"line {ln} ({n} fields)" for ln, n in bad[:5])
            raise HucError(
                f"{path.name}: the header has {ncols} fields, but {len(bad)} row(s) "
                f"do not: {shown}{', ...' if len(bad) > 5 else ''}. A cell containing "
                "a line break or the delimiter, or a missing or extra delimiter, "
                "would shift values into the wrong columns or rows, so nothing was "
                "written. Fix those lines (or export the sheet as .xlsx) and re-run.")
        # Everything is read as text so values round-trip unchanged (no "NA"
        # -> NaN, no stripped leading zeros). Blank lines are kept as rows so
        # output rows stay aligned with the input's lines. index_col=False:
        # a trailing delimiter on data rows must not turn the first column
        # into the index (shifting every value left); the empty extra fields
        # it creates are dropped.
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", pd.errors.ParserWarning)
                df = pd.read_csv(path, sep=delim, dtype=str, keep_default_na=False,
                                 encoding=enc, skip_blank_lines=False, quoting=quoting,
                                 index_col=False)
        except pd.errors.ParserError as e:
            raise HucError(f"Could not parse {path}: {e}"
                           + (" (a double quote (\") opened in a field but never closed?)"
                              if "inside string" in str(e) else ""))
        df = _drop_trailing_empty_rows(df.fillna(""))
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        while lines and lines[-1].strip(delim + " \t") == "":
            lines.pop()
        if not multiline and len(lines) - 1 != len(df):
            log(f"Warning: {path.name} has {len(lines) - 1} data lines but {len(df)} "
                "rows were read. Quoted fields spanning several lines, or a stray "
                "double quote (\"), merge lines into one row; check that the output "
                "lines up with your input.")
        names = {"\t": "tab", ",": "comma", ";": "semicolon", "|": "pipe"}
        info = {"kind": "text", "encoding": enc, "delimiter": delim,
                "newline": newline, "ext": ext, "quoting": quoting,
                "desc": f"{names[delim]}-delimited, {enc}"}
    df.columns = [str(c) for c in df.columns]
    return df, info


def write_table(df, path, info, text_columns=()):
    """Write df to path; the format follows the output file extension."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext == ".xls":
        raise HucError("Writing .xls is not supported; use .xlsx for the output.")
    if ext in EXCEL_EXTS:
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Sheet1")
            ws = writer.sheets["Sheet1"]
            # Codes are already Python strings (so Excel stores them as text);
            # the '@' format keeps them text if someone edits the cells.
            for j, col in enumerate(df.columns, start=1):
                if col in text_columns:
                    for row in ws.iter_rows(min_row=2, min_col=j, max_col=j):
                        row[0].number_format = "@"
        return
    if info["kind"] == "text":
        enc, newline = info["encoding"], info["newline"]
    else:
        enc, newline = "utf-8", "\n"
    quoting = csv.QUOTE_MINIMAL
    if info["kind"] == "text" and ext == info["ext"]:
        delim, quoting = info["delimiter"], info["quoting"]  # same kind of file as the input
    else:
        delim = "," if ext == ".csv" else "\t"
    # errors="replace": a WBD name a legacy encoding can't hold (e.g. Hawaiian
    # okina in cp1252) becomes "?" instead of aborting the write.
    df.to_csv(path, sep=delim, index=False, encoding=enc, lineterminator=newline,
              quoting=quoting, errors="strict" if enc.startswith("utf") else "replace")


# --------------------------------------------------------------------------
# Finding and parsing the coordinate columns
# --------------------------------------------------------------------------

def _norm(name):
    n = re.sub(r"[^0-9a-z]", "", str(name).lower())
    return n[3:] if n.startswith("dwc") else n  # Darwin Core prefixes (dwc:)


def _pick_column(columns, requested, tiers, what, flag):
    if requested is not None:
        if requested in columns:
            return requested
        hits = [c for c in columns if _norm(c) == _norm(requested)]
        if len(hits) == 1:
            return hits[0]
        raise HucError(f"{flag} {requested!r} does not match any column. "
                       f"Columns are: {', '.join(map(repr, columns))}")
    for tier in tiers:
        hits = [c for c in columns if _norm(c) in tier]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise HucError(f"Several columns could be the {what}: "
                           f"{', '.join(map(repr, hits))}. Choose one with {flag}.")
    raise HucError(f"Could not find a {what} column. Columns are: "
                   f"{', '.join(map(repr, columns))}. Name it with {flag}.")


def find_coord_columns(df, lat_col=None, lon_col=None):
    cols = list(df.columns)
    lat = _pick_column(cols, lat_col, LAT_TIERS, "latitude", "--lat-col")
    lon = _pick_column(cols, lon_col, LON_TIERS, "longitude", "--lon-col")
    if lat == lon:
        raise HucError(f"Latitude and longitude resolved to the same column {lat!r}.")
    return lat, lon


_DMS_ALLOWED = re.compile(r"[\d\s.°º˚'′’\"″”:]*")
_NUM = re.compile(r"\d+(?:\.\d*)?|\.\d+")
_MISSING_TEXT = {"na", "n/a", "nan", "null", "none", "-"}


def parse_coord(value, axis):
    """Parse one coordinate. Returns (float or None, problem or None).

    Accepts decimal degrees ("-87.098", "36") and simple degree/minute/second
    forms with optional hemisphere letters ("35°56'12\"N", "86 30.5 W",
    "86.5W"). axis is "lat" or "lon"; only N/S (lat) or E/W (lon) are allowed.
    """
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None, "missing"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value), None
    s = str(value).strip().strip('"').strip()  # quotes from quoted text exports
    if s == "" or s.lower() in _MISSING_TEXT:
        return None, "missing"
    try:
        v = float(s)
        return (v, None) if math.isfinite(v) else (None, "unparseable")
    except ValueError:
        pass
    s = s.upper()
    sign, hemi = 1.0, None
    if s[0] in "NSEW":
        hemi, s = s[0], s[1:].strip()
    elif s[-1] in "NSEW":
        hemi, s = s[-1], s[:-1].strip()
    if hemi and hemi not in ("NS" if axis == "lat" else "EW"):
        return None, "unparseable"
    if hemi in ("S", "W"):
        sign = -1.0
    if s[:1] == "-":
        if hemi in ("N", "E"):
            return None, "unparseable"  # contradictory, e.g. "-86.5 E"
        sign, s = -1.0, s[1:]
    elif s[:1] == "+":
        s = s[1:]
    if not _DMS_ALLOWED.fullmatch(s):
        return None, "unparseable"
    parts = _NUM.findall(s)
    if not 1 <= len(parts) <= 3 or any("." in p for p in parts[:-1]):
        return None, "unparseable"
    vals = [float(p) for p in parts]
    if any(v >= 60 for v in vals[1:]):
        return None, "unparseable"
    deg = vals[0] + sum(v / 60 ** i for i, v in enumerate(vals[1:], start=1))
    return sign * deg, None


def parse_coordinates(df, lat_col, lon_col):
    """Add _lat/_lon (floats, NaN if unusable) and _note columns to df."""
    lats, lons, notes = [], [], []
    for raw_lat, raw_lon in zip(df[lat_col], df[lon_col]):
        lat, lat_prob = parse_coord(raw_lat, "lat")
        lon, lon_prob = parse_coord(raw_lon, "lon")
        note = ""
        if lat_prob == "missing" and lon_prob == "missing":
            note = "missing coordinates"
        elif lat_prob or lon_prob:
            bad = [f"{p} {a}" for p, a in ((lat_prob, "latitude"), (lon_prob, "longitude")) if p]
            note = "; ".join(bad)
        elif not (-90 <= lat <= 90 and -180 <= lon <= 180):
            note = "coordinates out of range"
            if -90 <= lon <= 90 and -180 <= lat <= 180:
                note += " (possible lat/lon swap)"
        elif lat == 0 and lon == 0:
            note = "coordinates are 0,0 (likely a placeholder)"
        if note:
            lat = lon = float("nan")
        lats.append(lat)
        lons.append(lon)
        notes.append(note)
    df["_lat"], df["_lon"], df["_note"] = lats, lons, notes


# --------------------------------------------------------------------------
# The WBD
# --------------------------------------------------------------------------

def download_wbd(dest_dir=DATA_DIR, url=WBD_URL):
    """Download and unzip the national WBD geodatabase into dest_dir."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    zip_path = dest_dir / Path(url).name
    req = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(req, timeout=60) as resp:
        total = int(resp.headers.get("Content-Length", 0))
        last_modified = resp.headers.get("Last-Modified", "unknown")
    if zip_path.is_file() and zip_path.stat().st_size == total:
        log(f"Using already-downloaded {zip_path}")
    else:
        log(f"Downloading {url}\n  ({total / 1e9:.1f} GB) to {zip_path}")
        part = zip_path.with_suffix(".part")
        done, last_pct = 0, -1
        with urllib.request.urlopen(url, timeout=60) as resp, open(part, "wb") as fh:
            while chunk := resp.read(1 << 22):
                fh.write(chunk)
                done += len(chunk)
                pct = int(100 * done / total) if total else 0
                if pct != last_pct:
                    sys.stderr.write(f"\r  {done / 1e9:.2f} / {total / 1e9:.2f} GB ({pct}%)")
                    last_pct = pct
        sys.stderr.write("\n")
        if total and part.stat().st_size != total:
            raise HucError("Download incomplete; run --download again.")
        part.replace(zip_path)
    log("Unzipping...")
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest_dir)
        gdb_names = {n.split("/")[0] for n in zf.namelist() if ".gdb/" in n}
    if len(gdb_names) != 1:
        raise HucError(f"Expected one .gdb in {zip_path}, found {sorted(gdb_names)}")
    gdb = dest_dir / gdb_names.pop()
    (dest_dir / SOURCE_FILE).write_text(
        "USGS Watershed Boundary Dataset (WBD), national file geodatabase\n"
        f"url: {url}\n"
        f"source last modified: {last_modified}\n"
        f"downloaded: {date.today().isoformat()}\n"
        f"path: {gdb.name}\n")
    zip_path.unlink()
    log(f"WBD ready: {gdb}")
    return gdb


def wbd_source_note(wbd):
    """One-line description of the WBD copy in use, for the log."""
    src = Path(wbd).parent / SOURCE_FILE
    if not src.is_file():
        return str(wbd)
    fields = dict(line.split(": ", 1) for line in src.read_text().splitlines()
                  if ": " in line)
    return (f"{wbd} (source dated {fields.get('source last modified', '?')}, "
            f"downloaded {fields.get('downloaded', '?')})")


class WBD:
    """Thin wrapper around the WBD layers WBDHU2 ... WBDHU12."""

    def __init__(self, path):
        self.path = str(path)
        try:
            layers = {name for name, _ in pyogrio.list_layers(self.path)}
        except Exception as e:
            raise HucError(f"Could not open WBD at {path}: {e}")
        self.layers = {name.upper(): name for name in layers}
        self._fields = {}

    def layer(self, level):
        key = f"WBDHU{level}"
        if key not in self.layers:
            raise HucError(f"Layer {key} not found in {self.path}")
        return self.layers[key]

    def fields(self, level):
        """(code field, name field, CRS) for a level; field names vary in case."""
        if level not in self._fields:
            info = pyogrio.read_info(self.path, layer=self.layer(level))
            by_lower = {f.lower(): f for f in info["fields"]}
            try:
                code, name = by_lower[f"huc{level}"], by_lower["name"]
            except KeyError:
                raise HucError(f"Layer {self.layer(level)} lacks huc{level}/name fields")
            # The national WBD uses a compound CRS (NAD83 + NAVD88 height);
            # only its horizontal part matters here.
            crs = pyproj.CRS(info["crs"]) if info["crs"] else pyproj.CRS("EPSG:4269")
            if crs.is_compound:
                crs = crs.sub_crs_list[0]
            self._fields[level] = (code, name, crs)
        return self._fields[level]

    def polygons(self, level, bbox):
        code, name, crs = self.fields(level)
        gdf = pyogrio.read_dataframe(self.path, layer=self.layer(level),
                                     columns=[code], bbox=tuple(bbox))
        return gdf.set_crs(crs, allow_override=True).rename(columns={code: "code"})

    def names(self, level):
        """Series mapping code -> name for every unit at this level."""
        code, name, _ = self.fields(level)
        df = pyogrio.read_dataframe(self.path, layer=self.layer(level),
                                    columns=[code, name], read_geometry=False)
        return df.drop_duplicates(code).set_index(code)[name]


def locate(wbd, lat, lon, level):
    """Find the level-`level` HUC for each point.

    lat/lon are equal-length sequences. Returns a DataFrame with one row per
    point (same order) and columns: code (None if not found) and also
    (other codes when the point sits on a boundary between units).
    """
    out = pd.DataFrame({"code": pd.Series([None] * len(lat), dtype=object),
                        "also": [""] * len(lat)})
    if len(lat) == 0:
        return out
    _, _, crs = wbd.fields(level)
    pts = gpd.GeoDataFrame({"_pid": range(len(lat))},
                           geometry=gpd.points_from_xy(lon, lat),
                           crs="EPSG:4326").to_crs(crs)
    if level > CHUNK_FINER_THAN:
        cells = [np.floor(np.asarray(lon) / CHUNK_DEGREES),
                 np.floor(np.asarray(lat) / CHUNK_DEGREES)]
        groups = [g for _, g in pts.groupby(cells)]
    else:
        groups = [pts]
    hits = []
    eps = 1e-6
    for g in groups:
        xmin, ymin, xmax, ymax = g.total_bounds
        polys = wbd.polygons(level, (xmin - eps, ymin - eps, xmax + eps, ymax + eps))
        if not polys.empty:
            j = gpd.sjoin(g, polys, how="inner", predicate="intersects")
            hits.append(j[["_pid", "code"]])
    if hits:
        allhits = pd.concat(hits).drop_duplicates()
        # Codes must be text of exactly `level` digits: a numeric field (e.g.
        # in a .gpkg made with R or from a spreadsheet) has lost its leading
        # zeros, and its prefixes (HUC2 "60" for "06...") would be wrong.
        ok = allhits["code"].map(lambda c: isinstance(c, str) and len(c) == level
                                 and c.isdigit())
        if not ok.all():
            raise HucError(
                f"Layer {wbd.layer(level)} in {wbd.path} has huc{level} values that are "
                f"not {level}-digit text (e.g. {allhits.loc[~ok, 'code'].iloc[0]}). A "
                "numeric field drops leading zeros, which would give wrong codes; "
                f"store huc{level} as a text field.")
        codes = allhits.sort_values(["_pid", "code"]).groupby("_pid")["code"].agg(list)
        out.loc[codes.index, "code"] = codes.str[0]
        out.loc[codes.index, "also"] = codes.map(lambda c: ", ".join(c[1:]))
    return out


# --------------------------------------------------------------------------
# Main workflow
# --------------------------------------------------------------------------

def parse_levels(text):
    try:
        levels = sorted({int(t.lower().replace("huc", ""))
                         for t in re.split(r"[,\s]+", text.strip()) if t})
    except ValueError:
        raise HucError(f"--levels must be numbers like 8 or 2,4,8; got {text!r}")
    bad = [lv for lv in levels if lv not in LEVELS]
    if bad or not levels:
        raise HucError(f"HUC levels must be among {', '.join(map(str, LEVELS))}; got {text!r}")
    return levels


def output_column_names(df_columns, levels):
    """Names for the appended columns, suffixed if the input already has them."""
    base = []
    for lv in levels:
        base += [f"HUC{lv}", f"HUC{lv}_name"]
    base.append("huc_note")
    existing = set(df_columns)
    suffix, n = "", 1
    while any(c + suffix in existing for c in base):
        suffix = "_wbd" if n == 1 else f"_wbd{n}"
        n += 1
    if suffix:
        log(f"Note: input already has some of {base[:-1]}; new columns end in '{suffix}'.")
    return {c: c + suffix for c in base}


def assign_hucs(df, wbd, levels, fix_lon_sign=False):
    """Fill HUC code/name columns for each row of df (which has _lat/_lon/_note)."""
    finest = max(levels)
    valid = df["_lat"].notna()
    uniq = df.loc[valid, ["_lat", "_lon"]].drop_duplicates().reset_index(drop=True)
    log(f"Looking up {len(uniq)} unique coordinate pairs "
        f"({int(valid.sum())} rows) in WBDHU{finest}...")
    found = locate(wbd, uniq["_lat"].tolist(), uniq["_lon"].tolist(), finest)
    uniq["code"], uniq["also"] = found["code"], found["also"]
    uniq["note"] = ""
    miss = uniq["code"].isna()
    uniq.loc[miss, "note"] = "outside WBD coverage"

    pos = miss & (uniq["_lon"] > 0)
    if pos.any():
        if fix_lon_sign:
            sub = uniq[pos]
            retry = locate(wbd, sub["_lat"].tolist(), (-sub["_lon"]).tolist(), finest)
            for idx, code, also in zip(sub.index, retry["code"], retry["also"]):
                if code is not None:
                    uniq.at[idx, "code"], uniq.at[idx, "also"] = code, also
                    uniq.at[idx, "note"] = (f"longitude sign flipped for lookup "
                                            f"(used {-uniq.at[idx, '_lon']})")
                else:
                    uniq.at[idx, "note"] += " (also not found with longitude negated)"
        else:
            uniq.loc[pos, "note"] += (" (positive longitude: possible missing '-' "
                                      "sign; see --fix-lon-sign)")
    has_also = uniq["also"] != ""
    uniq.loc[has_also, "note"] = ("on HUC boundary; also in " + uniq.loc[has_also, "also"]
                                  + uniq.loc[has_also, "note"].map(lambda s: f"; {s}" if s else ""))

    merged = df.merge(uniq[["_lat", "_lon", "code", "note"]], on=["_lat", "_lon"],
                      how="left", validate="many_to_one")
    assert len(merged) == len(df) and (merged["_row_id"].values == df["_row_id"].values).all()
    codes = merged["code"]
    notes = ["; ".join(filter(None, (a, b)))
             for a, b in zip(df["_note"], merged["note"].fillna(""))]

    result = {}
    for lv in levels:
        lv_codes = codes.map(lambda c: c[:lv] if isinstance(c, str) else None)
        names = wbd.names(lv)
        result[lv] = (lv_codes, lv_codes.map(lambda c: names.get(c) if c else None))
    return result, notes, uniq


def summarize(out_df, by, levels, colnames):
    """One row per `by` value x HUC with record counts.

    n_records counts every record with a HUC; n_sign_flipped and n_on_boundary
    say how many of them were placed only by --fix-lon-sign or lie on a unit
    boundary (lowest code used), so uncertain placements stay visible.
    """
    finest = colnames[f"HUC{max(levels)}"]
    note = colnames["huc_note"]
    has_huc = out_df[finest].notna() & (out_df[finest] != "")
    keep = [by] + [colnames[c] for lv in levels for c in (f"HUC{lv}", f"HUC{lv}_name")]
    sub = out_df.loc[has_huc, keep].copy()
    sub[by] = sub[by].map(lambda v: "(blank)" if v is None or str(v).strip() == ""
                          or (isinstance(v, float) and math.isnan(v)) else v)
    sub[by] = sub[by].astype(str)
    notes = out_df.loc[has_huc, note].fillna("").astype(str)
    sub["_flip"] = notes.str.contains("longitude sign flipped", regex=False)
    sub["_edge"] = notes.str.startswith("on HUC boundary")
    summary = (sub.groupby(keep, dropna=False)
               .agg(n_records=("_flip", "size"), n_sign_flipped=("_flip", "sum"),
                    n_on_boundary=("_edge", "sum"))
               .reset_index().sort_values([by, finest]).reset_index(drop=True))
    return summary, int((~has_huc).sum())


def default_output(path):
    path = Path(path)
    ext = ".xlsx" if path.suffix.lower() == ".xls" else path.suffix
    return path.with_name(f"{path.stem}_huc{ext}")


def run(args):
    if args.download:
        if DEFAULT_WBD.exists():
            log(f"WBD already present at {DEFAULT_WBD}; delete it to download again.")
        else:
            download_wbd()
        if args.input is None:
            return
    if args.input is None:
        raise HucError("No input file given (see --help).")
    wbd_path = Path(args.wbd) if args.wbd else DEFAULT_WBD
    if not wbd_path.exists():
        raise HucError(f"WBD not found at {wbd_path}.\n"
                       "Run once with --download to fetch it (2.8 GB), or point "
                       "--wbd at an existing WBD .gdb/.gpkg.")
    levels = parse_levels(args.levels)
    out_path = Path(args.output) if args.output else default_output(args.input)
    if out_path.resolve() == Path(args.input).resolve():
        raise HucError("Output file would overwrite the input; choose another -o.")
    if out_path.suffix.lower() not in (EXCEL_EXTS | TEXT_EXTS) - {".xls"}:
        raise HucError(f"Output must be .txt, .tsv, .csv or .xlsx; got {out_path.name}")
    wbd = WBD(wbd_path)

    df, info = read_table(args.input, args.sheet)
    log(f"Read {len(df)} rows from {args.input} ({info['desc']})")
    lat_col, lon_col = find_coord_columns(df, args.lat_col, args.lon_col)
    log(f"Latitude column: {lat_col!r}   Longitude column: {lon_col!r}")
    if args.summarize_by is not None:
        args.summarize_by = _pick_column(list(df.columns), args.summarize_by, [],
                                         "summary", "--summarize-by")
    log(f"WBD: {wbd_source_note(wbd_path)}")

    colnames = output_column_names(df.columns, levels)
    # Only the coordinates go into the working table, so input columns named
    # like the internal ones (note, code, _row_id, ...) cannot collide.
    work = pd.DataFrame({"_row_id": range(len(df)), "_raw_lat": df[lat_col].to_numpy(),
                         "_raw_lon": df[lon_col].to_numpy()})
    parse_coordinates(work, "_raw_lat", "_raw_lon")
    result, notes, uniq = assign_hucs(work, wbd, levels, args.fix_lon_sign)

    out = df.copy()
    for lv in levels:
        codes, names = result[lv]
        out[colnames[f"HUC{lv}"]] = codes.values
        out[colnames[f"HUC{lv}_name"]] = names.values
    out[colnames["huc_note"]] = notes
    assert len(out) == len(df)

    text_cols = {colnames[f"HUC{lv}"] for lv in levels}
    write_table(out, out_path, info, text_cols)

    # Report
    finest_col = colnames[f"HUC{max(levels)}"]
    assigned = out[finest_col].notna()
    note_s = pd.Series(notes)
    n_flip = int(note_s.str.contains("longitude sign flipped", regex=False).sum())
    n_outside = int(note_s.str.contains("outside WBD coverage").sum())
    n_pos = int(note_s.str.contains("positive longitude").sum())
    n_badcoord = int((work["_lat"].isna()).sum())
    n_boundary = int(note_s.str.startswith("on HUC boundary").sum())
    log("")
    log(f"  {int(assigned.sum()):>6} rows assigned a HUC{max(levels)}"
        + (f" ({n_flip} after flipping longitude sign)" if n_flip else ""))
    if n_badcoord:
        log(f"  {n_badcoord:>6} rows with missing/invalid coordinates")
    if n_outside:
        log(f"  {n_outside:>6} rows outside WBD coverage"
            + (f" ({n_pos} with positive longitude; see --fix-lon-sign)" if n_pos else ""))
    if n_boundary:
        log(f"  {n_boundary:>6} rows on a HUC boundary (first code used; see huc_note)")
    for lv in levels:
        log(f"  {out.loc[assigned, colnames[f'HUC{lv}']].nunique():>6} distinct HUC{lv}s")
    log(f"Wrote {out_path}")

    if args.summarize_by is not None:
        summary, n_skipped = summarize(out, args.summarize_by, levels, colnames)
        sum_path = out_path.with_name(f"{out_path.stem}_summary{out_path.suffix}")
        write_table(summary, sum_path, info, text_cols)
        log(f"Wrote {sum_path} ({len(summary)} {args.summarize_by} x HUC combinations"
            + (f"; {n_skipped} rows without a HUC left out" if n_skipped else "") + ")")
        n_flag = int(((summary["n_sign_flipped"] > 0) | (summary["n_on_boundary"] > 0)).sum())
        if n_flag:
            log(f"  {n_flag} summary rows include records placed by a longitude sign flip "
                "or on a HUC boundary; see the n_sign_flipped / n_on_boundary columns.")


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Assign USGS HUC codes (HUC8 by default) to records with lat/lon, "
                    "using the USGS Watershed Boundary Dataset.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Output: the input table with HUC<N>, HUC<N>_name and huc_note columns "
               "appended,\nwritten to <input>_huc.<ext> unless -o is given.")
    p.add_argument("input", nargs="?", help="input table (.txt, .tsv, .csv, .xlsx, .xls)")
    p.add_argument("-o", "--output", help="output file; format follows its extension")
    p.add_argument("--levels", default="8",
                   help="HUC level(s) to report, e.g. 8 or 2,4,6,8,10,12 (default 8)")
    p.add_argument("--lat-col", help="latitude column name (default: auto-detect)")
    p.add_argument("--lon-col", help="longitude column name (default: auto-detect)")
    p.add_argument("--sheet", help="Excel sheet name (default: first sheet)")
    p.add_argument("--wbd", help=f"path to a WBD .gdb or .gpkg (default: {DEFAULT_WBD})")
    p.add_argument("--download", action="store_true",
                   help="download the national WBD (2.8 GB) into data/wbd/ next to this script")
    p.add_argument("--fix-lon-sign", action="store_true",
                   help="retry points with positive longitude that fall outside the WBD "
                        "using the negated longitude (input values are not changed)")
    p.add_argument("--summarize-by", metavar="COLUMN",
                   help="also write <output>_summary with one row per COLUMN value x HUC "
                        "and a record count (e.g. --summarize-by species)")
    args = p.parse_args(argv)
    try:
        run(args)
    except HucError as e:
        log(f"Error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
