#!/usr/bin/env python3
"""slideshow.py — full-screen image slideshow with fade transitions.

Usage:
  slideshow.py                         # default folder, 5s per image
  slideshow.py --interval 10           # 10 seconds per image
  slideshow.py --folder /path/to/pics  # custom folder

Controls during the slideshow:
  ESC or Q          quit
  SPACE             pause / resume
  RIGHT or N        next image
  LEFT or P         previous image
  + / -             adjust interval (clamped to 3-30 seconds)

Auto-creates the picture folder if missing and prints where to drop pictures.
Handles JPG, PNG, GIF, BMP, WEBP. Fade crossfade between images (~0.5s).
"""

import argparse
from pathlib import Path

DEFAULT_FOLDER = Path.home() / "Pictures" / "slideshow"
EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
FADE_FRAMES = 30
FPS = 60


def get_image_files(folder):
    return sorted(
        p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in EXTENSIONS
    )


def load_and_scale(path, screen_size, pygame):
    img = pygame.image.load(str(path)).convert()
    sw, sh = screen_size
    iw, ih = img.get_size()
    # Preserve aspect ratio, center on black
    scale = min(sw / iw, sh / ih)
    nw, nh = max(1, int(iw * scale)), max(1, int(ih * scale))
    img = pygame.transform.smoothscale(img, (nw, nh))
    surface = pygame.Surface((sw, sh))
    surface.fill((0, 0, 0))
    surface.blit(img, ((sw - nw) // 2, (sh - nh) // 2))
    return surface


def main():
    parser = argparse.ArgumentParser(
        description="Full-screen image slideshow with fade."
    )
    parser.add_argument(
        "--interval",
        "-i",
        type=int,
        default=5,
        help="Seconds per image (3-30, default 5)",
    )
    parser.add_argument(
        "--folder",
        "-f",
        type=Path,
        default=DEFAULT_FOLDER,
        help=f"Picture folder (default: {DEFAULT_FOLDER})",
    )
    args = parser.parse_args()

    interval = max(3, min(30, args.interval))
    folder = args.folder.expanduser()

    # Auto-create folder if missing — friendly message instead of crash
    if not folder.exists():
        folder.mkdir(parents=True, exist_ok=True)
        print(f"Created folder: {folder}")
        print("Drop pictures (jpg / png / gif / bmp / webp) in there, then re-run.")
        return

    files = get_image_files(folder)
    if not files:
        print(f"No pictures found in {folder}")
        print("Drop pictures (jpg / png / gif / bmp / webp) in there, then re-run.")
        return

    print(f"Found {len(files)} pictures in {folder}")
    print(f"Interval: {interval}s. ESC/Q quits, SPACE pauses, +/- adjusts.")

    import pygame

    pygame.init()
    info = pygame.display.Info()
    screen = pygame.display.set_mode(
        (info.current_w, info.current_h), pygame.FULLSCREEN
    )
    pygame.display.set_caption("Slideshow")
    pygame.mouse.set_visible(False)
    clock = pygame.time.Clock()
    screen_size = screen.get_size()

    current_idx = 0
    paused = False
    current_surface = load_and_scale(files[current_idx], screen_size, pygame)

    def show_message(msg, dur_ms=1200):
        font = pygame.font.SysFont(None, 48)
        text = font.render(msg, True, (255, 255, 255))
        rect = text.get_rect(center=(screen_size[0] // 2, screen_size[1] - 60))
        screen.blit(current_surface, (0, 0))
        pygame.draw.rect(screen, (0, 0, 0), rect.inflate(40, 20))
        screen.blit(text, rect)
        pygame.display.flip()
        pygame.time.wait(dur_ms)

    def crossfade(from_surf, to_surf):
        for i in range(FADE_FRAMES):
            alpha = int(255 * (i / FADE_FRAMES))
            screen.blit(from_surf, (0, 0))
            tmp = to_surf.copy()
            tmp.set_alpha(alpha)
            screen.blit(tmp, (0, 0))
            pygame.display.flip()
            clock.tick(FPS)
        screen.blit(to_surf, (0, 0))
        pygame.display.flip()

    def advance_to(new_idx):
        nonlocal current_surface, current_idx
        current_idx = new_idx % len(files)
        next_surface = load_and_scale(files[current_idx], screen_size, pygame)
        crossfade(current_surface, next_surface)
        current_surface = next_surface

    last_advance = pygame.time.get_ticks()
    screen.blit(current_surface, (0, 0))
    pygame.display.flip()

    running = True
    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                if event.key in (pygame.K_ESCAPE, pygame.K_q):
                    running = False
                elif event.key == pygame.K_SPACE:
                    paused = not paused
                    show_message("PAUSED" if paused else "RESUMED")
                    last_advance = pygame.time.get_ticks()
                elif event.key in (pygame.K_RIGHT, pygame.K_n):
                    advance_to(current_idx + 1)
                    last_advance = pygame.time.get_ticks()
                elif event.key in (pygame.K_LEFT, pygame.K_p):
                    advance_to(current_idx - 1)
                    last_advance = pygame.time.get_ticks()
                elif event.key in (pygame.K_PLUS, pygame.K_EQUALS):
                    interval = min(30, interval + 1)
                    show_message(f"{interval}s")
                    last_advance = pygame.time.get_ticks()
                elif event.key == pygame.K_MINUS:
                    interval = max(3, interval - 1)
                    show_message(f"{interval}s")
                    last_advance = pygame.time.get_ticks()

        if not paused and pygame.time.get_ticks() - last_advance >= interval * 1000:
            advance_to(current_idx + 1)
            last_advance = pygame.time.get_ticks()

        clock.tick(FPS)

    pygame.quit()


if __name__ == "__main__":
    main()
