#!/bin/bash

# Matrix Rain Animation
# Features: proper cleanup, hidden cursor, color variation, timed execution

# Terminal dimensions
ROWS=$(tput lines)
COLS=$(tput cols)

# Color codes
GREEN='\033[0;32m'
BRIGHT_GREEN='\033[1;32m'
DARK_GREEN='\033[0;90m'
RESET='\033[0m'

# Hide cursor
echo -ne "\033[?25l"

# Cleanup function to restore cursor and clear screen
cleanup() {
    echo -ne "\033[?25h"  # Show cursor
    clear
    exit 0
}

# Set trap for cleanup on exit
trap cleanup EXIT INT TERM

# Matrix characters (Katakana, numbers, symbols)
CHARS="ｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉﾊﾋﾌﾍﾎﾏﾐﾑﾒﾓﾔﾕﾖﾗﾘﾙﾚﾛﾜﾝ0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# Initialize rain columns
declare -A rain
for ((i=1; i<=COLS; i++)); do
    rain[$i]=0
done

# Main animation loop
start_time=$SECONDS
end_time=$((start_time + 15))  # Run for 15 seconds

while (( SECONDS < end_time )); do
    # Clear screen
    echo -ne "\033[H"

    # Draw rain columns
    for ((col=1; col<=COLS; col++)); do
        # Randomly start new rain columns
        if (( rain[$col] == 0 && RANDOM % 100 < 3 )); then
            rain[$col]=1
        fi

        # Draw active rain columns
        if (( rain[$col] > 0 )); then
            # Head of the rain (bright)
            if (( rain[$col] <= ROWS )); then
                char=${CHARS:$((RANDOM % ${#CHARS})):1}
                echo -ne "\033[${rain[$col]};${col}H${BRIGHT_GREEN}${char}${RESET}"
            fi

            # Trail of the rain (varying intensity)
            for ((i=1; i<5; i++)); do
                if (( rain[$col] - i > 0 && rain[$col] - i <= ROWS )); then
                    char=${CHARS:$((RANDOM % ${#CHARS})):1}
                    if (( i == 1 )); then
                        echo -ne "\033[$((rain[$col] - i));${col}H${GREEN}${char}${RESET}"
                    else
                        echo -ne "\033[$((rain[$col] - i));${col}H${DARK_GREEN}${char}${RESET}"
                    fi
                fi
            done

            # Clear old tail
            if (( rain[$col] - 5 > 0 && rain[$col] - 5 <= ROWS )); then
                echo -ne "\033[$((rain[$col] - 5));${col}H "
            fi

            # Move rain down
            ((rain[$col]++))

            # Reset if rain goes off screen
            if (( rain[$col] - 5 > ROWS )); then
                rain[$col]=0
            fi
        fi
    done

    # Control animation speed
    sleep 0.08
done
