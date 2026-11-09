# huc_lookup.py — full reference

(For a quick start, see [README.md](README.md).)

Takes a table of records with latitude/longitude and adds the USGS Hydrologic
Unit Code (HUC8 by default; any of HUC2/4/6/8/10/12 on request) and its name
for each record, using the USGS Watershed Boundary Dataset (WBD).

## Setup (once)

```bash
conda env create -f environment.yml        # creates the "huc" environment
conda activate huc
python huc_lookup.py --download            # fetches the national WBD (2.8 GB) into data/wbd/
```

`data/wbd/WBD_SOURCE.txt` records the source URL, the dataset's date and the
download date. To use a WBD copy stored elsewhere (e.g. one shared by a lab),
pass `--wbd /path/to/WBD_National_GDB.gdb`.

Without activating the environment (e.g. in a SLURM script), call its Python directly,
e.g. `~/miniconda3/envs/huc/bin/python huc_lookup.py ...`

## Usage

```bash
python huc_lookup.py sample_data.txt                         # HUC8 → sample_data_huc.txt
python huc_lookup.py records.xlsx --levels 4,8,12            # several levels
python huc_lookup.py records.csv --summarize-by species      # + species × HUC table
python huc_lookup.py records.csv --lat-col Y_dd --lon-col X_dd -o out.xlsx
```

| Option | Meaning |
|---|---|
| `--levels 8` | HUC level(s): any of 2,4,6,8,10,12, comma-separated (default 8) |
| `-o FILE` | Output file (default `<input>_huc.<same extension>`; `.xls` input → `.xlsx`). Format follows the extension (.txt/.tsv tab, .csv comma, .xlsx Excel). |
| `--lat-col`, `--lon-col` | Name the coordinate columns if auto-detection can't |
| `--sheet NAME` | Excel sheet to read (default: first sheet) |
| `--summarize-by COLUMN` | Also write `<output>_summary.<ext>`: one row per COLUMN value × HUC with `n_records`, plus `n_sign_flipped` (how many of those records were placed only by `--fix-lon-sign`) and `n_on_boundary` (how many lie on a unit boundary, lowest code used) |
| `--fix-lon-sign` | Retry points that have a positive longitude and fall outside the WBD with the longitude negated (the input value is not changed; the row is noted) |
| `--wbd PATH` | Use a different WBD copy (national or regional .gdb/.gpkg). The `hucN` fields must be text; a copy with numeric codes (leading zeros lost) is refused. |

### Input

* `.txt`, `.tsv`, `.csv` (delimiter detected: tab, comma, semicolon or pipe), `.xlsx`, `.xls`
  (`.xls` reading is supported via xlrd but has not been tested here).
* Any number of other columns; they are passed through untouched, and every
  input row appears in the output in the same order — including blank lines in
  the middle of the file (they come out as `missing coordinates` rows), so the
  HUC columns can be pasted back next to the original spreadsheet. Only empty
  rows at the very end of the file are dropped.
* Every row of a text file must have as many fields as the header; otherwise
  the script stops and names the offending lines, rather than letting values
  slide into the wrong column or row. Extra *empty* fields (a trailing tab or
  comma on each row) are fine and are dropped.
* Tab- and pipe-delimited files are read without quote processing, so a stray
  `"` in a locality (e.g. `5" below dam`) cannot merge rows; text is written back
  verbatim. The exception is a file in which Excel has exported a cell
  containing a line break (as `"two⏎lines"`): if quote processing makes every
  row fit the header, the file is read that way and the script says so, and each
  output row then corresponds to one spreadsheet record, not one line of the
  file. Comma/semicolon CSV uses standard quoting; if the number of rows read
  differs from the number of lines (a quoted field spanning lines, or stray
  quotes), the script prints a warning, and a quote that is never closed stops
  it with an error.
* Coordinate columns are recognized by heading (case, spaces and punctuation
  ignored), in this order of preference:
  1. `decimalLatitude` / `decimalLongitude` (Darwin Core; `dwc:` prefix OK)
  2. `Latitude`, `Lat`, `decimalLat`, `DecLat`, `Lat_DD`, `LatDecimal`, `Latitude (DD)`, `GPS_Lat`, `Lat_WGS84`, … /
     `Longitude`, `Long`, `Lon`, `Lng`, `decimalLon`, `DecLong`, `Long_DD`, `LongDecimal`, `GPS_Long`, …
  3. `verbatimLatitude` / `verbatimLongitude`
  4. `Y` / `X`

  If two columns tie (e.g. both `Lat` and `Latitude`), the script stops and asks
  for `--lat-col`/`--lon-col`.
* Values: decimal degrees, or simple degrees-minutes-seconds such as
  `35°56'12"N`, `35 56 12 N`, `86 30.5 W`, `86.5W`.

### Output columns

`HUC8`, `HUC8_name` (one pair per requested level, coarse → fine) and `huc_note`.
If the input already has a column called e.g. `HUC8`, the new columns get a `_wbd` suffix.

`huc_note` is empty when all is well, otherwise one of:

| Note | Meaning |
|---|---|
| `missing coordinates` / `missing latitude` / `missing longitude` | blank cell(s) |
| `unparseable latitude` / `unparseable longitude` | not a number or recognizable DMS |
| `coordinates out of range (possible lat/lon swap)` | \|lat\| > 90 or \|lon\| > 180 |
| `coordinates are 0,0 (likely a placeholder)` | |
| `outside WBD coverage` | point not inside any hydrologic unit (outside the U.S., offshore, …) |
| `… (positive longitude: possible missing '-' sign; see --fix-lon-sign)` | as above, with lon > 0 |
| `longitude sign flipped for lookup (used -85.2359)` | `--fix-lon-sign` found it with the sign flipped |
| `on HUC boundary; also in 06040003` | point lies exactly on a boundary; the first (lowest) code is reported |

A summary of counts is printed when the script finishes.

## Notes and caveats

* **Leading zeros.** HUCs are text codes, and those in regions 01–09 start with
  0 (e.g. Kentucky's Green River basin is `05110001`). The script keeps them as
  text, and in `.xlsx` output the HUC cells are stored as text. **Opening a
  .txt/.csv output in Excel by double-clicking strips the leading zero** — use
  Excel's import (Data → From Text/CSV, set the HUC columns to Text), or ask
  for `.xlsx` output (`-o result.xlsx`).
* **How levels are derived.** The point is matched against the finest requested
  level's polygons; coarser codes are the leading digits of that code (WBD
  units are strictly nested, e.g. HUC12 `060400030101` lies in HUC8 `06040003`),
  and names come from the corresponding WBD layer.
* **Coverage.** The WBD covers the 50 states, Puerto Rico, the U.S. Virgin
  Islands and Pacific territories, plus the Canadian and Mexican parts of
  shared watersheds — so a point just across the border can still get a HUC.
  Points outside any unit are reported as `outside WBD coverage`.
* **Datum.** Coordinates are treated as WGS84; the WBD is NAD83. The difference
  (~1–2 m) is far below the precision of specimen coordinates.
* **Excel.** Only one sheet is read (first, or `--sheet`); the output workbook
  contains that sheet only.
* **Text encoding.** UTF-8 (with or without BOM), Windows-1252 and Latin-1 input
  are accepted; output is written in the same encoding.

## Checking the script

`python tests/run_checks.py` (in the `huc` environment) re-runs the end-to-end
checks: other formats and column headings give identical results, rows stay
aligned, leading zeros survive, and a spread of points matches the USGS WBD web
service (`--offline` skips that last part).

## Data source / citation

U.S. Geological Survey, Watershed Boundary Dataset (WBD), national file
geodatabase, <https://www.usgs.gov/national-hydrography/watershed-boundary-dataset>.
Downloaded from
`https://prd-tnm.s3.amazonaws.com/StagedProducts/Hydrography/WBD/National/GDB/WBD_National_GDB.zip`
(see `data/wbd/WBD_SOURCE.txt` for the exact dataset date and download date).
