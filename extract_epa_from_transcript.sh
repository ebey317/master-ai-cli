#!/bin/bash
# Extract EPA 608 certification page from transcript PDF

TRANSCRIPT_PATH='/home/user/Desktop/resume/Transcript.pdf'
OUTPUT_PATH="$HOME/portfolio/master/epa608_type2.pdf"
PAGE_NUMBER=1  # Page 1 contains EPA certification

if command -v pdftk >/dev/null 2>&1; then
    pdftk "$TRANSCRIPT_PATH" cat $PAGE_NUMBER output "$OUTPUT_PATH"
    if [ -f "$OUTPUT_PATH" ]; then
        echo "EPA 608 extracted to: $OUTPUT_PATH"
    else
        echo "Failed to extract page $PAGE_NUMBER"
    fi
else
    echo "Need pdftk to extract PDF pages"
    echo "Install with: sudo apt install pdftk"
fi