#!/usr/bin/env bash
# Countdown display with green numbers centered on the terminal

# Clear the screen
printf '\033[2J'

# Get terminal size
rows=$(tput lines)
cols=$(tput cols)

# Calculate middle position
mid_row=$((rows / 2))
mid_col=$((cols / 2 - 1))

# Countdown from 5 to 1
for n in 5 4 3 2 1; do
   # Clear the screen
   printf '\033[2J'
   # Print the number in bright green at the calculated position
   printf "\033[${mid_row};${mid_col}H\033[1;92m  %d  \033[0m" "$n"
   sleep 1
done

# Optional: reset cursor position after finishing
printf '\033[${rows};0H'
