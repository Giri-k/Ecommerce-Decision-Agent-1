# M5 input files

Obtain the official files from the [M5 Forecasting Accuracy data page](https://www.kaggle.com/competitions/m5-forecasting-accuracy/data). Sign in and follow Kaggle's access process before downloading.

Place these three unmodified files here:

- `sales_train_evaluation.csv`
- `calendar.csv`
- `sell_prices.csv`

Use the evaluation sales file, which contains days `d_1` through `d_1941`. The shorter validation sales file is not a replacement.

From the project root, run:

```sh
make data
```

Or point to an existing input directory:

```sh
make data M5_SOURCE_DIR='/path/to/m5'
```

The importer selects ten products from store CA_3 using training-period information only. It joins sales, calendar and weekly prices, checks data quality, and records source/output checksums under `var/data/m5/`. That prepared panel is regenerated locally.

`SHA256SUMS` records the three input files used for the current development data. To compare a fresh download with those exact inputs, run this from `data/raw/`:

```sh
shasum -a256 -c SHA256SUMS
```

On systems with `sha256sum` instead, use `sha256sum -c SHA256SUMS`.

Raw files and generated data remain ignored by Git. The repository's synthetic fixtures and tests run without downloading M5. Do not add API credentials to this directory. Review the source's applicable terms before redistributing M5 files or derived sales data.
