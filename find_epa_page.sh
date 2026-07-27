#!/bin/bash
# Find the page number containing EPA 608 certification in transcript PDF

TRANSCRIPT_PATH='/home/user/Desktop/resume/Transcript.pdf'
TEMP_DIR='/tmp/transcript_pages'

# Check if pdftotext is installed
if ! command -v pdftotext &> /dev/null; then
    echo "Error: pdftotext is not installed. Install it with: sudo apt install poppler-utils"
    exit 1
fi

# Create temporary directory
mkdir -p "$TEMP_DIR"
rm -f "$TEMP_DIR"/*.txt

# Get total number of pages
TOTAL_PAGES=$(pdfinfo "$TRANSCRIPT_PATH" | grep Pages | awk '{print $2}')
if [ -z "$TOTAL_PAGES" ]; then
    echo "Error: Could not get page count from PDF."
    exit 1
fi

echo "Total pages in transcript: $TOTAL_PAGES"

# Extract text from each page and search for EPA keyword
for (( page=1; page<=TOTAL_PAGES; page++ )); do
    txt_file="$TEMP_DIR/page_$page.txt"
    pdftotext -f $page -l $page "$TRANSCRIPT_PATH" "$txt_file"
    if grep -q -i "epa\|608\|refrigerant" "$txt_file"; then
        echo "EPA certification found on page: $page"
        # Show a preview of the text
        echo "Preview:"
        grep -i "epa\|608\|refrigerant" "$txt_file" | head -3
        break
    fi
done

# Clean up
rm -rf "$TEMP_DIR"