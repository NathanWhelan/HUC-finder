# huc_lookup.py

Adds USGS watershed codes (HUC8 by default) to a table of specimen records
that have latitude and longitude.

## Setup (once)

You need [conda](https://docs.conda.io/en/latest/miniconda.html) (Miniconda is fine).
On Windows, type these in the Anaconda Prompt.

```bash
conda env create -f environment.yml
conda activate huc
python huc_lookup.py --download
```

`--download` fetches the USGS watershed boundaries (2.8 GB) into `data/wbd/`
next to the script. It needs about 6.5 GB of free disk while it unzips; 3.6 GB
stays afterwards.

## Run

```bash
conda activate huc
python huc_lookup.py my_records.txt
```

This writes `my_records_huc.txt`: your table, unchanged, with three columns added.

| Column | Example |
|---|---|
| `HUC8` | `06040002` |
| `HUC8_name` | `Upper Duck` |
| `huc_note` | Blank if all is well; otherwise why there is no HUC (e.g. `missing coordinates`) |

It reads tab-delimited `.txt`, `.csv` and `.xlsx` files, with any other columns.
The latitude and longitude columns are found from common headings
(`Latitude`, `lat`, `decimalLatitude`, `Lat_DD`, `Longitude`, `long`, …).

## Options

| Option | What it does |
|---|---|
| `--levels 2,4,6,8,10,12` | Report other HUC levels (default `8`) |
| `--summarize-by species` | Also write a table of each species × HUC with record counts (records placed by `--fix-lon-sign` or on a boundary are counted in separate columns) |
| `-o out.xlsx` | Choose the output file name and format |
| `--lat-col NAME --lon-col NAME` | Name the coordinate columns if they aren't found |
| `--fix-lon-sign` | Retry points with a positive longitude (probably a missing `-`) |

## Good to know

- **Leading zeros:** opening a `.txt`/`.csv` output in Excel by double-clicking
  drops the leading zero from HUC codes (`06040002` becomes `6040002`). Use
  `-o out.xlsx` to get an Excel file instead.
- **Coordinates:** decimal degrees (WGS84/NAD83). Degrees-minutes-seconds such as
  `35°56'12"N` also work.
- **Coverage:** the U.S. and its territories, plus the Canadian and Mexican parts
  of shared watersheds.

All the details are in [REFERENCE.md](REFERENCE.md).

## Data source

U.S. Geological Survey, Watershed Boundary Dataset (WBD).
`data/wbd/WBD_SOURCE.txt` records the dataset date and download date for
citing in a methods section.

To cite this script, see [CITATION.cff](CITATION.cff) (GitHub's "Cite this
repository" button).

## License

MIT — see [LICENSE](LICENSE). The WBD data are a U.S. Government work in the
public domain and are not included in this repository.
