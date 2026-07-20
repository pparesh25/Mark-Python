import os
import csv
import config


def clean_csv_files():
    source_dir = str(config.DATA_DIR)
    dest_dir = str(config.BASE_DIR / 'NSE_EOD_Cleaned')

    # Create destination directory if it doesn't exist
    os.makedirs(dest_dir, exist_ok=True)

    cols_to_keep = ['Date', 'Open', 'High', 'Low', 'Close', 'Volume']

    # Get list of all CSV files in the source directory
    files = [f for f in os.listdir(source_dir) if f.lower().endswith('.csv')]
    total_files = len(files)
    print(f"Found {total_files} CSV files to process.")

    processed_count = 0
    error_count = 0
    skipped_count = 0

    for index, filename in enumerate(files, 1):
        source_file = os.path.join(source_dir, filename)
        dest_file = os.path.join(dest_dir, filename)

        try:
            with open(source_file, mode='r', newline='', encoding='utf-8') as infile:
                reader = csv.reader(infile)

                try:
                    header = next(reader)
                except StopIteration:
                    # Empty file
                    print(f"Skipping empty file: {filename}")
                    skipped_count += 1
                    continue
                
                # Find indices of the required columns
                try:
                    col_indices = [header.index(col) for col in cols_to_keep]
                except ValueError:
                    # Try case-insensitive match
                    header_lower = [h.lower() for h in header]
                    try:
                        col_indices = [header_lower.index(col.lower()) for col in cols_to_keep]
                    except ValueError:
                        print(f"Skipping {filename}: Headers do not match expected format. Found: {header}")
                        error_count += 1
                        continue
                
                # Write to the destination file
                with open(dest_file, mode='w', newline='', encoding='utf-8') as outfile:
                    writer = csv.writer(outfile)
                    writer.writerow(cols_to_keep)
                    
                    for row in reader:
                        # Write only the selected columns, handle cases where row is shorter
                        writer.writerow([row[idx] if idx < len(row) else '' for idx in col_indices])
            
            processed_count += 1
            if index % 500 == 0 or index == total_files:
                print(f"Processed {index}/{total_files} files...")
                
        except Exception as e:
            print(f"Error processing {filename}: {e}")
            error_count += 1

    print(f"\nProcessing finished.")
    print(f"Successfully processed: {processed_count} files.")
    if skipped_count > 0:
        print(f"Empty files skipped: {skipped_count} files.")
    if error_count > 0:
        print(f"Failed/Skipped: {error_count} files.")
    print(f"Cleaned files saved in: {dest_dir}")

if __name__ == '__main__':
    clean_csv_files()
