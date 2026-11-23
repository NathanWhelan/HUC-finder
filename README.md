# Tools for downloading records and classifying by HUC

Two scripts for specimen and occurrence records:

- **`gbif_pull.py`** downloads [GBIF](https://www.gbif.org) records that have
  coordinates for a list of taxa. You can keep only records from chosen U.S. states.
- **`huc_lookup.py`** adds USGS watershed codes (HUC8 by default) to any table
  with latitude and longitude, including the output of `gbif_pull.py`.

## Setup (once)

Install [Miniconda](https://docs.conda.io/en/latest/miniconda.html). On Windows,
type the commands below in the Anaconda Prompt.

```bash
conda env create -f environment.yml
conda activate huc
python huc_lookup.py --download
```

`--download` gets the USGS watershed maps (2.8 GB) into `data/wbd/`. It needs
6.5 GB of free disk while it unzips. `gbif_pull.py` does not need this step.

Run `conda activate huc` each time you open a new terminal.

## gbif_pull.py: get GBIF records

Make a text file with one taxon per line (e.g. `Physa acuta`), then:

```bash
python gbif_pull.py taxa.txt --states OH IN --class Gastropoda
```

This writes two files to `gbif_output/`:

- `occurrences.csv`: the records.
- `taxon_summary.csv`: how each name matched GBIF. **Check this file.** Misspelled,
  unknown or ambiguous names are skipped, and the reason is given here. Synonyms
  are converted to the accepted name, so records filed under old names are included.

| Option | What it does |
|---|---|
| `--states OH IN` | Postal code abbreviations. Keep records whose coordinates fall in these states, including state waters (e.g. Lake Erie) |
| `--class Gastropoda` | Helps match names that are also used for plants, insects, etc. |
| `--download` | Use for taxa with more than 100,000 records. Gives a DOI to cite. Needs a free gbif.org account |
| `-o folder` | Choose the output folder |

`python gbif_pull.py --help` lists all options. The top of the script explains each one.

## huc_lookup.py: add watersheds

```bash
python huc_lookup.py gbif_output/occurrences.csv
```

This writes `gbif_output/occurrences_huc.csv`: your table, unchanged, with three columns added.

| Column | Example |
|---|---|
| `HUC8` | `06040002` |
| `HUC8_name` | `Upper Duck` |
| `huc_note` | Blank if all is well. Otherwise, why there is no HUC (e.g. `missing coordinates`) |

Input can be `.txt` (tab-delimited), `.csv` or `.xlsx`. Coordinate columns are found
by their usual names (`Latitude`, `lat`, `decimalLatitude`, …).

| Option | What it does |
|---|---|
| `--levels 2,4,6,8,10,12` | Report other HUC levels (default `8`) |
| `--summarize-by species` | Also write a table of record counts for each species in each HUC |
| `-o out.xlsx` | Choose the output file name and format |
| `--lat-col NAME --lon-col NAME` | Name the coordinate columns if they aren't found |
| `--fix-lon-sign` | Retry points with a positive longitude (probably a missing `-`) |

Good to know:

- Excel drops leading zeros from HUC codes in `.txt`/`.csv` files (`06040002`
  becomes `6040002`). Use `-o out.xlsx` to avoid this.
- Coordinates: decimal degrees (WGS84/NAD83) or degrees-minutes-seconds (`35°56'12"N`).
- Covers the U.S. and its territories, plus the Canadian and Mexican parts of
  shared watersheds.

All the details are in [REFERENCE.md](REFERENCE.md).

## Citing

- **GBIF:** cite the DOI from `--download`. Records pulled without it have no DOI.
- **Watersheds:** U.S. Geological Survey, Watershed Boundary Dataset (WBD).
  `data/wbd/WBD_SOURCE.txt` records the dataset date for your methods section.
- **These scripts:** see [CITATION.cff](CITATION.cff) (GitHub's "Cite this repository" button).

## License

MIT; see [LICENSE](LICENSE). The WBD data are public domain and are not included here.
