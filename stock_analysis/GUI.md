# Interactive stock charts

The batch image renderer is unchanged.  The interactive workspace lives behind
the optional `gui` feature, so the binary published for each release keeps
building without the UI stack.  Start it against the existing DuckDB EOD store
with:

```bash
cargo run --release --features gui -- gui /path/to/eod.duckdb NSE
```

Use `BSE` as the third argument for BSE data.  The app reads its symbol list
from `eod_data`, so it never needs a second watchlist file, and it opens the
database read-only.

Features included:

- Hideable, resizable watchlist sidebar with a filter box, and a symbol
  dropdown in the toolbar.
- Candlestick price panel and a separate coloured volume panel.
- Add/remove RSI (14), MACD (12, 26, 9), and Stochastic (14, 3) panes at
  runtime.
- Linked time axes and crosshairs across price, volume, and every oscillator.

Image export is opt-in and does not appear in ordinary GUI builds:

```bash
cargo run --release --features image-export -- gui /path/to/eod.duckdb NSE
```

The `Export PNG` button then uses the existing headless Plotters renderer and
writes `exports/<SYMBOL>_chart.png` relative to the process working directory.
