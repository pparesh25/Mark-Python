"""Chart rendering for qualified stocks."""

import logging
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import mplfinance as mpf
import pandas as pd
import talib as ta

import config


def plot_save_chart(args):
    row, target_dir, date_col, data_dir = args
    try:
        stock = row["symbol"]
        idx_val = str(row["Index"]) if "Index" in row else "0"
        mark_date = row[date_col]
        exchange = row.get("Exchange", row.get("exchange", "NSE"))
        import duckdb
        conn = duckdb.connect(str(config.DUCKDB_FILE), read_only=True)
        try:
            stock_data = conn.execute("""
                SELECT Date, Open, High, Low, Close, Volume, Series, Total_Trades as TOTAL_TRADES, Qty_Per_Trade as QTY_PER_TRADE, Dlv_Qty as DLV_QTY
                FROM eod_data 
                WHERE Symbol = ? AND Exchange = ?
                ORDER BY Date ASC
            """, (stock, exchange)).fetchdf()
        finally:
            conn.close()

        if stock_data.empty:
            return

        stock_data = stock_data.tail(500)
        stock_data["Date"] = pd.to_datetime(stock_data["Date"])
        stock_data.set_index("Date", inplace=True)
        stock_data["EMA_50"] = ta.EMA(stock_data["Close"], timeperiod=50)
        stock_data["EMA_150"] = ta.EMA(stock_data["Close"], timeperiod=150)
        stock_data["EMA_200"] = ta.EMA(stock_data["Close"], timeperiod=200)
        stock_data = stock_data.loc[row["Plot Start"]:row["Plot End"]]
        if stock_data.empty:
            return

        from matplotlib.gridspec import GridSpec

        fig = plt.figure(figsize=(19.2, 10))
        fig.suptitle(stock, fontsize=18, fontweight="bold", y=0.995)
        grid = GridSpec(3, 1, figure=fig, height_ratios=[0.5, 2.5, 0.8], hspace=0.02, top=0.96)
        ax_info = fig.add_subplot(grid[0])
        ax_info.axis("off")
        info_line1 = (
            f"{row['Name']}  |  Industry: {row['Industry']}  |  "
            f"Price: ₹{row['Current Price']}  |  "
            f"MCap: ₹{row['Market Capitalization']} Cr  |  "
            f"PE: {row.get('Price to Earning', 'N/A')}"
        )
        info_line2 = (
            f"ROE: {row.get('Return on equity', 'N/A')}%  |  "
            f"ROCE: {row.get('Return on capital employed', 'N/A')}%  |  "
            f"OPM: {row.get('OPM', 'N/A')}%  |  "
            f"P/B: {row.get('Price to book value', 'N/A')}  |  "
            f"Debt/Eq: {row.get('Debt to equity', 'N/A')}"
        )
        info_line3 = (
            f"Sales 5Y: {row['Sales growth 5Years']}%  |  "
            f"Profit 5Y: {row['Profit growth 5Years']}%  |  "
            f"Promoter: {row.get('Promoter holding', 'N/A')}%  |  "
            f"FII: {row.get('FII holding', 'N/A')}%  |  "
            f"DII: {row.get('DII holding', 'N/A')}%  |  "
            f"52w: -{row.get('Down from 52w high_y', 0)}% / +{row.get('Up from 52w low', 0)}%"
        )
        ax_info.text(
            0.5, 0.5, f"{info_line1}\n{info_line2}\n{info_line3}",
            transform=ax_info.transAxes, fontsize=10, verticalalignment="center",
            horizontalalignment="center", bbox=dict(boxstyle="round", facecolor="lightblue", alpha=0.3),
            family="monospace", linespacing=1.8,
        )
        ax_price = fig.add_subplot(grid[1])
        ax_volume = fig.add_subplot(grid[2], sharex=ax_price)
        addplots = [
            mpf.make_addplot(stock_data["EMA_50"], ax=ax_price, color="blue", width=1.0),
            mpf.make_addplot(stock_data["EMA_150"], ax=ax_price, color="orange", width=1.0),
            mpf.make_addplot(stock_data["EMA_200"], ax=ax_price, color="red", width=1.0),
        ]
        style = mpf.make_mpf_style(base_mpf_style="charles", mavcolors=["blue", "orange", "red"])
        mpf.plot(
            stock_data, type="candle", style=style, ax=ax_price, volume=ax_volume,
            addplot=addplots, ylabel="Price", ylabel_lower="Volume",
            update_width_config=dict(volume_linewidth=0, volume_width=0.75),
        )
        ax_volume.yaxis.tick_right()
        ax_volume.yaxis.set_label_position("right")
        ax_price.grid(True, linestyle=":", linewidth=0.3, color="gray", alpha=0.5)
        ax_volume.grid(True, linestyle=":", linewidth=0.3, color="gray", alpha=0.5)
        try:
            ax_price.axvline(x=stock_data.index.get_loc(mark_date), color="blue", linestyle=":", linewidth=0.8, alpha=0.7)
        except (KeyError, ValueError):
            pass
        plt.setp(ax_volume.xaxis.get_majorticklabels(), rotation=0, ha="center")
        plt.savefig(target_dir / f"{idx_val}_{stock}.png", bbox_inches="tight", pad_inches=0.1, dpi=150)
        plt.close()
    except Exception as error:
        logging.error(f"Error plotting {row.get('symbol', 'unknown')}: {error}")

