#!/bin/bash

# Mark Screener Runner
# This script runs the Mark screener with the opentrader-pro conda environment

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  Mark Stock Screener${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

# Get the directory where this script is located
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# Run the screener using the conda environment
echo "Downloading latest data..."
/opt/anaconda3/envs/opentrader-pro/bin/python "$SCRIPT_DIR/screener_downloader.py"
echo ""

echo "Running screener..."
/opt/anaconda3/envs/opentrader-pro/bin/python "$SCRIPT_DIR/Mark screener.py"

echo ""
echo -e "${GREEN}✓ Screening complete!${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
