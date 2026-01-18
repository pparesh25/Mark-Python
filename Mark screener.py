import os
import pandas as pd
import talib as ta
import time
from multiprocess import Pool, freeze_support, cpu_count, set_start_method
import matplotlib
matplotlib.use('Agg')  # Use non-interactive backend to prevent plots from opening
import matplotlib.pyplot as plt
import mplfinance as mpf
from datetime import date, datetime
from pathlib import Path

# Start measuring execution time
start_time = time.time()

# Path to the data directory (Universal paths using pathlib)
data_dir = Path('/Users/paresh/Desktop/eod2-main/src/eod2_data/daily')
output_chart_dir = Path('/Users/paresh/Desktop/Mark Python/python_output_charts')
fundamental_file = Path('/Users/paresh/Desktop/Mark Python/stock_project_file/all-company.csv')
amibroker_watchlist_dir = Path('/Users/paresh/.wine/drive_c/Program Files (x86)/AmiBroker/Data/WatchLists')

# Current date for file naming (YYYYMMDD format)
current_date = datetime.now().strftime('%Y%m%d')

# Create output folders if not exists
output_chart_dir.mkdir(parents=True, exist_ok=True)
amibroker_watchlist_dir.mkdir(parents=True, exist_ok=True)

# List all the files in the data directory
files = [f.name for f in data_dir.glob('*.csv')]

# Function to process each file
def process_file(file):
    try:
        # Check if the file is a CSV file
        if file.endswith('.csv'):
            # Extract symbol from filename (remove .csv extension)
            symbol = file.replace('.csv', '').upper()
            
            # Read the file into a DataFrame with header row
            filepath = data_dir / file
            df = pd.read_csv(filepath, header=0)
            
            # Add symbol column
            df['symbol'] = symbol
            
            # Rename columns to lowercase for consistency
            df.columns = df.columns.str.lower()
            
            # Convert 'date' column to datetime
            df['date'] = pd.to_datetime(df['date'])

            # Calculate the moving averages
            df['EMA_50'] = ta.EMA(df['close'], timeperiod=50).round(2)
            df['EMA_150'] = ta.EMA(df['close'], timeperiod=150).round(2)
            df['EMA_200'] = ta.EMA(df['close'], timeperiod=200).round(2)

            # Obtain the 200-day EMA before 30 days
            df['EMA_200_30'] = df['EMA_200'].shift(30)

            # Calculate the 52-week high and low based on close values
            df['52 week high'] = ta.MAX(df['high'], timeperiod=252)
            df['52 week low'] = ta.MIN(df['low'], timeperiod=252)

            # This calc is from original script, but we will filter using CSV data later too.
            df['Down from 52w high'] = (((df['52 week high'] - df['close']) / df['52 week high']) * 100).round(2)

            # Filter the DataFrame to get the last row
            # last_row = df.iloc[[-1]] # Unused in original logic actually?

            # Reverse the DataFrame to iterate from the last date
            reversed_df = df.iloc[::-1]

            # Find the date when the condition is first met starting from the last date and moving backwards
            first_match_date = pd.NaT
            for index, row in reversed_df.iterrows():
                # Check for NaN because comparisons with NaN are False
                if pd.notna(row['EMA_200_30']):
                    if row['close'] > row['EMA_50'] > row['EMA_150'] > row['EMA_200'] > row['EMA_200_30'] :
                        first_match_date = row['date']
                    else:
                        break
            
            if pd.isna(first_match_date):
                 return None

            # Assign the first match date to the 'Mark' column
            last_row = df.iloc[[-1]].copy()
            last_row['Mark'] = first_match_date

            # Return the last row
            return last_row[['symbol', 'date', 'close', 'EMA_50', 'EMA_150', 'EMA_200', 'EMA_200_30', '52 week high', '52 week low', 'Mark', 'Down from 52w high']]
    except Exception as e:
        print(f"Error processing {file}: {e}")
        return None

# Function to plot and save a single stock chart
def plot_save_chart(row):
    try:
        stock = row['symbol']
        # Use Index if available from iterrows, or construct logic. Here row is a Series.
        # Ensure we have an Index or just use symbol
        idx_val = str(row.name) if 'Index' not in row else str(row['Index'])

        stock_file = data_dir / f"{stock.lower()}.csv"
        
        if not stock_file.exists():
            return

        # Load the stock data from the CSV file
        stock_data = pd.read_csv(stock_file, header=0)
        stock_data['Date'] = pd.to_datetime(stock_data['Date'])
        stock_data.set_index('Date', inplace=True)

        # Calculate EMAs
        stock_data['EMA_50'] = ta.EMA(stock_data['Close'], timeperiod=50)
        stock_data['EMA_150'] = ta.EMA(stock_data['Close'], timeperiod=150)
        stock_data['EMA_200'] = ta.EMA(stock_data['Close'], timeperiod=200)

        # Get the start and end dates from the filtered dataframe
        stock_start_date = row['Plot Start']
        stock_end_date = row['Plot End']

        # Filter the stock data
        stock_data = stock_data.loc[stock_start_date:stock_end_date]
        
        if stock_data.empty:
            return

        # Create the plots
        ema_50 = mpf.make_addplot(stock_data['EMA_50'], color='blue')
        ema_150 = mpf.make_addplot(stock_data['EMA_150'], color='orange')
        ema_200 = mpf.make_addplot(stock_data['EMA_200'], color='red')

        # Plot the OHLC chart with optimized layout (16:9 aspect ratio)
        fig, axes = mpf.plot(stock_data, type='candle', volume=True, style='charles', figsize=(16, 9),
                             ylabel_lower='Volume', panel_ratios=(3, 1), title=stock, ylabel='Price',
                             addplot=[ema_50, ema_150, ema_200], returnfig=True, tight_layout=True)

        plt.grid(True, linestyle=':', linewidth=0.3, color='gray')

        # Convert dates to corresponding index values
        # Need to handle if Mark date is not exactly in index (e.g. non-trading day? though it comes from file)
        try:
            start_index = stock_data.index.get_loc(row['Mark'])
            axes[0].axvline(x=start_index, color='blue', linestyle=':')
        except KeyError:
            pass # Mark date might be outside plot range or not exact match

        # Overlay fundamental data
        stock_fundamentals = row

        # Prepare the text to display
        text_str = f"""
        Compny Name: {stock_fundamentals['Name']}
        Industry: {stock_fundamentals['Industry']}
        Current Market Price: {stock_fundamentals['Current Price']}
        Market Capitalization: {stock_fundamentals['Market Capitalization']}
        FII Holding: {stock_fundamentals['FII holding']}
        DII Holding: {stock_fundamentals['DII holding']}
        Sales growth 5Years: {stock_fundamentals['Sales growth 5Years']}
        Profit growth 5Years: {stock_fundamentals['Profit growth 5Years']}
        Down from 52w high: {stock_fundamentals.get('Down from 52w high_y', 0)}%
        Up from 52w low: {stock_fundamentals.get('Up from 52w low', 0)}%
        """
        # Note: Merge might create _x and _y for 'Down from 52w high' if both have it. 
        # Query results has 'Down from 52w high'. EOD scan has it too. 
        # We will check column names after merge.

        # Define font properties (smaller for better proportions)
        font_properties = {
            'family': 'monospace',
            'weight': 'bold',
            'size': 12
        }

        # Add the text with adjusted position (reduced left margin)
        plt.figtext(0.12, 0.62, text_str, fontdict=font_properties, color='blue', ha='left', bbox=dict(facecolor='pink', alpha=0.2))

        # Save the chart as an image in the corresponding folder
        image_name = f"{idx_val}_{stock}.png"
        image_path = output_chart_dir / image_name
        plt.savefig(image_path, bbox_inches='tight', pad_inches=0.1)

        plt.close()
    except Exception as e:
        print(f"Error plotting {row.get('symbol', 'unknown')}: {e}")


if __name__ == '__main__':
    try:
        set_start_method('spawn')
    except RuntimeError:
        pass
    freeze_support()

    # Create a pool of worker processes
    pool = Pool()  # Defaults to cpu_count()

    print("Scanning files...")
    # Process each file using parallel execution
    results_list = pool.map(process_file, files)
    
    pool.close()
    pool.join()

    # Filter out None results
    results_list = [r for r in results_list if r is not None]
    
    if not results_list:
        print("No stocks matched the technical conditions.")
        exit()

    # Concatenate the results
    results = pd.concat(results_list, ignore_index=True)
    
    # Read the CSV file into another DataFrame
    query_df = pd.read_csv(fundamental_file)

    # Merge the two DataFrames
    # output_df 'symbol' and query_df 'NSE Code'
    merged_df = pd.merge(results, query_df, left_on='symbol', right_on='NSE Code', how='left')

    # Drop rows where 'NSE Code' is NaN (no fundamental match)
    merged_df = merged_df.dropna(subset=['NSE Code'])

    # --- Filtering Logic ---
    print(f"Total matched technicals: {len(merged_df)}")
    
    # Start with all merged data (Market Cap filter removed)
    filtered_df = merged_df
    
    # 1. Down from 52w high < 20
    # Note: query-results.csv has 'Down from 52w high'. Our calculated one is 'Down from 52w high_x'.
    # We should use the one from CSV as requested? Actually query-results.csv usually has better data if updated.
    # Let's check columns. 
    # 'Down from 52w high' from CSV will be used.
    # If name collision occurs, pandas uses suffix.
    
    down_col = 'Down from 52w high_y' if 'Down from 52w high_y' in filtered_df.columns else 'Down from 52w high'
    if down_col not in filtered_df.columns and 'Down from 52w high' in query_df.columns:
         down_col = 'Down from 52w high' # Should exist from merge

    up_col = 'Up from 52w low'
    
    # Check if columns exist to avoid error
    if down_col in filtered_df.columns:
         filtered_df = filtered_df[filtered_df[down_col] < 20]
    
    if up_col in filtered_df.columns:
         filtered_df = filtered_df[filtered_df[up_col] > 50]

    # Filter for rows where 'Mark' is NaN (should be handled by initial scan but safe to keep)
    filtered_df = filtered_df[~filtered_df['Mark'].isna()]
    
    # Sort the data by 'Mark' column in descending order
    df_sorted = filtered_df.sort_values(by='Mark', ascending=False)
    
    # 'Plot Start' = 'Mark' - 500 days
    df_sorted['Plot Start'] = pd.to_datetime(df_sorted['Mark']) - pd.to_timedelta(500, unit='D')
    df_sorted['Plot End'] = pd.to_datetime(date.today())

    # Calculate Days_In_Trend: Mark Date to Last Candle Date (date column)
    df_sorted['Days_In_Trend'] = (pd.to_datetime(df_sorted['date']) - pd.to_datetime(df_sorted['Mark'])).dt.days
    
    # Calculate Trend_Strength: EMA Separation Score
    # Formula: ((EMA_50 - EMA_200) / EMA_200) * 100
    # Higher value = stronger trend (EMAs more separated)
    df_sorted['Trend_Strength'] = ((df_sorted['EMA_50'] - df_sorted['EMA_200']) / df_sorted['EMA_200'] * 100).round(2)

    # Reset the index after sorting
    df_sorted = df_sorted.reset_index(drop=True)
    df_sorted['Index'] = df_sorted.index # explicit index column

    print(f"Filtered down to {len(df_sorted)} stocks.")

    # Export CSV with required columns
    # Columns: Index, Symbol, Days_In_Trend, Trend_Strength, [All Fundamental Columns]
    csv_columns = ['Index', 'symbol', 'Days_In_Trend', 'Trend_Strength']
    
    # Add all fundamental columns (from query_df merge)
    fundamental_cols = [col for col in df_sorted.columns if col not in [
        'Index', 'symbol', 'date', 'close', 'EMA_50', 'EMA_150', 'EMA_200', 'EMA_200_30',
        '52 week high', '52 week low', 'Mark', 'Down from 52w high_x', 
        'Plot Start', 'Plot End', 'Days_In_Trend', 'Trend_Strength'
    ]]
    
    csv_columns.extend(fundamental_cols)
    
    # Create CSV with selected columns and date-based filename
    csv_export = df_sorted[csv_columns].copy()
    csv_filename = f"{current_date}_qualified_stocks.csv"
    csv_filepath = Path(csv_filename)
    csv_export.to_csv(csv_filepath, index=False)
    print(f"CSV exported: {csv_filename} ({len(csv_export)} stocks)")
    
    # Create AmiBroker watchlist file with stock symbols only
    amibroker_filename = f"{current_date} Mark_qualified.tls"
    amibroker_filepath = amibroker_watchlist_dir / amibroker_filename
    
    # Write symbols to AmiBroker watchlist file (one symbol per line)
    with open(amibroker_filepath, 'w') as f:
        for symbol in df_sorted['symbol']:
            f.write(f"{symbol}\n")
    
    print(f"AmiBroker watchlist created: {amibroker_filepath} ({len(df_sorted)} symbols)")

    print("Generating charts...")
    
    # Use multiprocessing pool to process charts
    # We need a new pool because previous one was closed
    with Pool(cpu_count()) as pool:
        pool.map(plot_save_chart, [row for _, row in df_sorted.iterrows()])

    print(f"Images exported to {output_chart_dir}")

    # Stop measuring execution time
    end_time = time.time()
    execution_time = end_time - start_time
    print("Execution time:", execution_time, "seconds")
