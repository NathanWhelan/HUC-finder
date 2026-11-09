#!/usr/bin/env python
"""End-to-end checks for huc_lookup.py.

    conda activate huc
    python tests/run_checks.py            # all checks (USGS web check needs internet)
    python tests/run_checks.py --offline  # skip the USGS web-service comparison

Builds variant input files in tests/fixtures/ from sample_data.txt (other
formats, other column headings, extra columns), runs huc_lookup.py on each, and
checks that the HUC results are identical, that rows are preserved 1:1, and
that leading zeros survive. Then compares a spread of points against the USGS
WBD web service, which is independent of the local copy.
"""

import json
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import geopandas as gpd
import openpyxl
import pandas as pd
import pyproj
from shapely.geometry import box

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCRIPT = ROOT / "huc_lookup.py"
SAMPLE = ROOT / "sample_data.txt"
FIX = HERE / "fixtures"
OUT = HERE / "output"
LEVELS = "2,4,6,8,10,12"
HUC_COLS = [c for lv in (2, 4, 6, 8, 10, 12) for c in (f"HUC{lv}", f"HUC{lv}_name")] + ["huc_note"]
REST = "https://hydro.nationalmap.gov/arcgis/rest/services/wbd/MapServer/{layer}/query"

sys.path.insert(0, str(ROOT))
from huc_lookup import HucError, locate, parse_coord  # noqa: E402

failures = []


def check(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        failures.append(msg)


def run(args, expect_ok=True):
    p = subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                       capture_output=True, text=True)
    if expect_ok and p.returncode != 0:
        print(p.stderr)
    return p


def read_out(path):
    if path.suffix == ".xlsx":
        return pd.read_excel(path, dtype=str, keep_default_na=False)
    sep = ";" if "semi" in path.name else ("," if path.suffix == ".csv" else "\t")
    return pd.read_csv(path, sep=sep, dtype=str, keep_default_na=False,
                       encoding="cp1252" if "cp1252" in path.name else "utf-8")


def make_fixtures(base):
    """Variants of the sample: (file name, lat heading, lon heading, writer)."""
    FIX.mkdir(exist_ok=True)
    n = len(base)
    extra = pd.DataFrame({
        "catalogNumber": [f"{i:06d}" for i in range(n)],      # leading zeros
        "locality": ["Duck River, at the ford" if i % 2 else "NA" for i in range(n)],
        "notes": ["" for _ in range(n)],
    })
    variants = [
        ("v_latitude.csv", "Latitude", "Longitude", "csv"),
        ("v_dwc.xlsx", "decimalLatitude", "decimalLongitude", "xlsx"),
        ("v_latlong.txt", "lat", "long", "tsv"),
        ("v_dd_semi.csv", "LAT_DD", "LONG_DD", "semicolon"),
        ("v_cp1252.txt", "Latitude (DD)", "Longitude (DD)", "tsv-cp1252"),
    ]
    files = []
    for name, la, lo, kind in variants:
        df = pd.concat([extra[["catalogNumber"]], base.rename(
            columns={"decimalLat": la, "decimalLon": lo}), extra[["locality", "notes"]]], axis=1)
        path = FIX / name
        if kind == "csv":
            df.to_csv(path, index=False)
        elif kind == "semicolon":
            df.to_csv(path, index=False, sep=";")
        elif kind == "tsv":
            df.to_csv(path, index=False, sep="\t")
        elif kind == "tsv-cp1252":
            df["locality"] = df["locality"].str.replace("Duck River", "Río Duck")
            df.to_csv(path, index=False, sep="\t", encoding="cp1252")
        elif kind == "xlsx":
            xdf = df.copy()  # numeric coordinate cells, as in a real spreadsheet
            for c in (la, lo):
                xdf[c] = pd.to_numeric(xdf[c], errors="coerce")
            xdf.to_excel(path, index=False)
        files.append((path, df))
    return files


def main():
    offline = "--offline" in sys.argv
    OUT.mkdir(exist_ok=True)
    base = pd.read_csv(SAMPLE, sep="\t", dtype=str, keep_default_na=False)

    print("1. Sample file, all levels")
    ref_path = OUT / "sample_all.txt"
    p = run([SAMPLE, "--levels", LEVELS, "-o", ref_path])
    check(p.returncode == 0, "huc_lookup.py ran on sample_data.txt")
    ref = read_out(ref_path)
    check(len(ref) == len(base), f"row count preserved ({len(ref)} == {len(base)})")
    check(ref[list(base.columns)].equals(base), "original columns unchanged and in order")
    # Special rows in the sample, found by content rather than position
    blank_sp = base.index[(base["species"] == "") & (base["decimalLat"] != "")]
    blank = base.index[(base == "").all(axis=1)]
    pos_idx = base.index[pd.to_numeric(base["decimalLon"], errors="coerce") > 0]
    check(len(blank_sp) and all(ref.loc[blank_sp, "HUC8"].str.len() == 8),
          f"{len(blank_sp)} rows with blank species but valid coords got HUC8s")
    check(len(blank) and all(ref.loc[blank, "huc_note"] == "missing coordinates"),
          f"{len(blank)} blank rows noted 'missing coordinates'")
    check(ref["HUC8"].str.startswith("05").any()
          and (ref.loc[ref["HUC8"] != "", "HUC8"].str.len() == 8).all(),
          "HUC8 codes are 8 characters with leading zeros (region 05 present)")
    for lv in (2, 4, 6, 8, 10):
        ok = (ref["HUC12"] == "") | (ref["HUC12"].str[:lv] == ref[f"HUC{lv}"])
        check(ok.all(), f"HUC{lv} == first {lv} digits of HUC12")
    check(not (ref.loc[ref["HUC8"] != "", "HUC8_name"] == "").any(), "every HUC8 has a name")
    pos = ref.loc[pos_idx]
    check(len(pos) and all(pos["HUC8"] == "") and all(pos["huc_note"].str.contains("positive longitude")),
          f"{len(pos)} positive-longitude rows flagged, not assigned")

    print("2. --fix-lon-sign")
    fix_path = OUT / "sample_fixsign.txt"
    run([SAMPLE, "--fix-lon-sign", "-o", fix_path])
    fx = read_out(fix_path)
    check(all(fx.loc[pos_idx, "HUC8"].str.startswith("06")),
          f"positive-longitude rows land in region 06 with sign flipped: {list(fx.loc[pos_idx, 'HUC8'])}")
    check(all(fx.loc[pos_idx, "decimalLon"] == base.loc[pos_idx, "decimalLon"]),
          "original longitude values left unchanged")

    print("3. Other formats / headings / extra columns")
    for path, src in make_fixtures(base):
        out = OUT / (path.stem + "_huc" + path.suffix)
        p = run([path, "--levels", LEVELS, "-o", out])
        if p.returncode != 0:
            check(False, f"{path.name}: script failed")
            continue
        got = read_out(out)
        check(len(got) == len(src), f"{path.name}: row count {len(got)}")
        check(got[HUC_COLS].equals(ref[HUC_COLS]), f"{path.name}: HUC columns identical to .txt run")
        check(got["catalogNumber"].equals(src["catalogNumber"]),
              f"{path.name}: catalogNumber leading zeros kept")
        check(got["locality"].equals(src["locality"]), f"{path.name}: locality text ('NA', commas, accents) kept")

    xl = OUT / "sample_all.xlsx"
    run([SAMPLE, "--levels", "8", "-o", xl])
    ws = openpyxl.load_workbook(xl).active
    header = [c.value for c in ws[1]]
    col = header.index("HUC8") + 1
    vals = [ws.cell(r, col).value for r in range(2, ws.max_row + 1)]
    check(any(isinstance(v, str) and v.startswith("05") and len(v) == 8 for v in vals)
          and ws.cell(2, col).number_format == "@",
          "xlsx output stores HUC8 as text (leading zero kept)")

    print("3b. Row alignment with blank lines and stray quotes (counted without pandas)")
    messy = FIX / "messy.txt"
    lines = ["species\tdecimalLat\tdecimalLon\tlocality",
             "A\t36.12472\t-87.09806\t\"Big Creek",            # unclosed quote
             "B\t35.5709\t-86.8716\t5\" below dam",           # stray inch mark
             "",                                              # truly empty line
             "C\t\"34.7323\"\t\"-87.8112\"\t\"quoted, ok\"",  # Excel-style quoting
             "\t\t\t",
             "D\t35.10265\t85.2359\tat the ford",
             "", ""]                                          # trailing blank lines
    messy.write_text("\n".join(lines) + "\n")
    raw_rows = len([ln for ln in "\n".join(lines).rstrip("\n\t").split("\n")]) - 1
    out = OUT / "messy_huc.txt"
    p = run([messy, "-o", out])
    out_lines = out.read_text().rstrip("\n").split("\n")
    check(p.returncode == 0 and len(out_lines) - 1 == raw_rows,
          f"messy.txt: {len(out_lines) - 1} output rows == {raw_rows} raw data lines")
    got = [ln.split("\t") for ln in out_lines[1:]]
    check([g[0] for g in got] == ["A", "B", "", "C", "", "D"], "row order/identity preserved")
    check(got[0][4] == "05130204" and got[1][4] == "06040002" and got[3][4] == "06030005",
          f"rows after the stray quotes keep their own HUC8s: {[g[4] for g in got]}")
    check(got[2][-1] == "missing coordinates" and got[4][-1] == "missing coordinates",
          "mid-file blank lines -> 'missing coordinates' rows")
    check(out_lines[1].split("\t")[3] == '"Big Creek' and out_lines[4].split("\t")[3] == '"quoted, ok"',
          "locality text written back verbatim")
    messy_csv = FIX / "messy.csv"
    messy_csv.write_text('species,lat,lon,locality\nA,36.12472,-87.09806,"Big Creek\n'
                         'B,35.5709,-86.8716,below dam\n')
    p = run([messy_csv, "-o", OUT / "messy_huc.csv"], expect_ok=False)
    check(p.returncode == 1 and "never closed" in p.stderr,
          "CSV with a quote never closed -> clear error, no output")
    messy_csv2 = FIX / "messy2.csv"
    messy_csv2.write_text('species,lat,lon,locality\nA,36.12472,-87.09806,"Big Creek\n'
                          'B,35.5709,-86.8716,5" below dam\nC,34.7323,-87.8112,ford\n')
    p = run([messy_csv2, "-o", OUT / "messy2_huc.csv"], expect_ok=False)
    check(p.returncode == 0 and "Warning" in p.stderr and "stray" in p.stderr,
          "CSV whose stray quotes merge lines -> warning about row count")

    print("3c. Field counts, column-name clashes, summary flags")
    # A = 05130204, B = 06040002 (as in 3b)
    multi = FIX / "multiline.txt"   # Excel tab export of a cell with Alt+Enter
    multi.write_text('species\tlocality\tdecimalLat\tdecimalLon\tyear\n'
                     'A\t"Duck R.\nat ford"\t36.12472\t-87.09806\t1998\n'
                     'B\tford\t35.5709\t-86.8716\t2001\n')
    out = OUT / "multiline_huc.txt"
    p = run([multi, "-o", out])
    got = pd.read_csv(out, sep="\t", dtype=str, keep_default_na=False) if p.returncode == 0 else None
    check(got is not None and list(got["species"]) == ["A", "B"]
          and list(got["HUC8"]) == ["05130204", "06040002"]
          and got["locality"][0] == "Duck R.\nat ford",
          "multi-line cell in a tab file stays one record; later rows keep their own HUC8")

    trail = FIX / "trailing_tab.txt"  # trailing tab on data rows only
    trail.write_text("species\tdecimalLat\tdecimalLon\nA\t36.12472\t-87.09806\t\n"
                     "B\t35.5709\t-86.8716\t\n")
    out = OUT / "trailing_tab_huc.txt"
    p = run([trail, "-o", out])
    got = read_out(out) if p.returncode == 0 else None
    check(got is not None and list(got["species"]) == ["A", "B"]
          and list(got["HUC8"]) == ["05130204", "06040002"],
          "trailing tab on data rows: first column kept, values not shifted")

    for name, body, what in [
            ("extra_field.txt", "species\tdecimalLat\tdecimalLon\nA\t36.12472\t-87.09806\tX\n",
             "an extra non-empty field"),
            ("short_row.txt", "species\tdecimalLat\tdecimalLon\nA\t36.12472\nB\t35.5709\t-86.8716\n",
             "a missing field")]:
        path = FIX / name
        path.write_text(body)
        p = run([path, "-o", OUT / (path.stem + "_huc.txt")], expect_ok=False)
        check(p.returncode == 1 and "fields" in p.stderr and "line 2" in p.stderr,
              f"row with {what} -> clear error naming the line, no output")

    clash = FIX / "clash.csv"  # input columns named like internal ones
    pd.DataFrame({"species": ["A", "B"], "lat": ["36.12472", "35.5709"],
                  "lon": ["-87.09806", "-86.8716"], "note": ["x", ""], "code": ["1", "2"],
                  "_row_id": ["9", "8"], "_raw_lat": ["", ""], "_note": ["n", "m"]}
                 ).to_csv(clash, index=False)
    out = OUT / "clash_huc.csv"
    p = run([clash, "-o", out], expect_ok=False)
    got = read_out(out) if p.returncode == 0 else None
    check(got is not None and list(got["HUC8"]) == ["05130204", "06040002"]
          and list(got["note"]) == ["x", ""] and list(got["_row_id"]) == ["9", "8"],
          "input columns named note/code/_row_id/_raw_lat/_note: run works, columns untouched")

    out = OUT / "sample_fixsign_sum.txt"
    p = run([SAMPLE, "--fix-lon-sign", "--summarize-by", "species", "-o", out])
    summ = read_out(OUT / "sample_fixsign_sum_summary.txt") if p.returncode == 0 else None
    check(summ is not None and {"n_records", "n_sign_flipped", "n_on_boundary"} <= set(summ.columns)
          and pd.to_numeric(summ["n_sign_flipped"]).sum() == len(pos_idx),
          f"summary counts the {len(pos_idx)} sign-flipped records in n_sign_flipped")

    class StubWBD:  # a WBD whose HU8 codes are stored as numbers or text
        path = "stub.gpkg"

        def __init__(self, code):
            self.code = code

        def layer(self, level):
            return f"WBDHU{level}"

        def fields(self, level):
            return "huc8", "name", pyproj.CRS("EPSG:4269")

        def polygons(self, level, bbox):
            return gpd.GeoDataFrame({"code": [self.code]}, geometry=[box(-88, 35, -86, 37)],
                                    crs="EPSG:4269")
    try:
        locate(StubWBD(6040002), [36.0], [-87.0], 8)
        check(False, "numeric HUC codes in --wbd -> error")
    except HucError as e:
        check("text" in str(e), f"numeric HUC codes in --wbd -> error ({e})")
    check(locate(StubWBD("06040002"), [36.0], [-87.0], 8)["code"][0] == "06040002",
          "text HUC codes in --wbd accepted")

    print("4. Column detection")
    amb = FIX / "ambiguous.csv"
    pd.DataFrame({"Lat": ["36.1"], "Latitude": ["36.1"], "Long": ["-87.1"]}).to_csv(amb, index=False)
    p = run([amb], expect_ok=False)
    check(p.returncode == 1 and "--lat-col" in p.stderr, "ambiguous headings -> clear error naming --lat-col")
    p = run([amb, "--lat-col", "Latitude", "-o", OUT / "ambiguous_huc.csv"])
    check(p.returncode == 0 and read_out(OUT / "ambiguous_huc.csv")["HUC8"][0] == "05130204",
          "--lat-col override works")
    odd = FIX / "odd_headers.csv"
    pd.DataFrame({"Y_coord": ["36.12472"], "x_coord": ["-87.09806"]}).to_csv(odd, index=False)
    p = run([odd, "--lat-col", "Y_coord", "--lon-col", "x_coord", "-o", OUT / "odd_huc.csv"])
    check(p.returncode == 0, "--lat-col/--lon-col with arbitrary headings")

    print("5. Coordinate parsing")
    cases = [("35°56'12\"N", "lat", 35.936667), ("86 30 W", "lon", -86.5), ("86.5W", "lon", -86.5),
             ("-86 30.5", "lon", -86.508333), ("N35 30", "lat", 35.5), (" 36.1 ", "lat", 36.1),
             ("abc", "lat", None), ("86.5W", "lat", None), ("NA", "lat", None), ("35 61", "lat", None)]
    for raw, axis, want in cases:
        got, _ = parse_coord(raw, axis)
        ok = (got is None) if want is None else (got is not None and abs(got - want) < 1e-5)
        check(ok, f"parse_coord({raw!r}, {axis}) -> {got}")

    if offline:
        print("6. USGS web service comparison skipped (--offline)")
    else:
        print("6. Compare with the USGS WBD web service (independent of the local copy)")
        assigned = ref[ref["HUC12"] != ""].drop_duplicates("HUC8")
        picks = assigned.groupby("HUC2").head(4)
        for _, r in picks.iterrows():
            lat, lon = r["decimalLat"], r["decimalLon"]
            api = {}
            for layer, lv in ((4, 8), (6, 12)):
                q = urllib.parse.urlencode({
                    "geometry": f"{lon},{lat}", "geometryType": "esriGeometryPoint",
                    "inSR": 4326, "spatialRel": "esriSpatialRelIntersects",
                    "outFields": f"huc{lv}", "returnGeometry": "false", "f": "json"})
                with urllib.request.urlopen(REST.format(layer=layer) + "?" + q, timeout=60) as resp:
                    feats = json.load(resp).get("features", [])
                api[lv] = sorted(f["attributes"][f"huc{lv}"] for f in feats)
            check(r["HUC8"] in api[8] and r["HUC12"] in api[12],
                  f"({lat}, {lon}) script {r['HUC8']}/{r['HUC12']}  USGS {api[8]}/{api[12]}")

    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED")
        sys.exit(1)
    print("All checks passed.")


if __name__ == "__main__":
    main()
