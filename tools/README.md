# tools

Two scripts that make the product film. Neither is part of the package; they
exist so the film can be remade when the interface changes, rather than being
a one-off nobody can reproduce.

## Making the film

```sh
bmail ui --no-open --port 8800        # one shell — note the token it prints
python tools/record_film.py <token>   # another
python tools/compose_score.py 89.3 --out bed.wav
```

then mux (the ffmpeg line is in the header of `compose_score.py`).

For the README itself the film has to become an **animated image**, because
GitHub's Markdown sanitiser deletes a `<video>` element and everything inside
it — the player and any fallback alike. Only `<img>` survives, so motion on a
README means an animated WebP:

```sh
ffmpeg -i media/bmail-film-30s.mp4 \
  -vf "fps=10,scale=1120:-2:flags=lanczos" \
  -c:v libwebp_anim -lossless 0 -q:v 72 -compression_level 3 -loop 0 -an \
  media/film.webp
```

3.9 MB, against 13 MB for the same thing as a 160-colour GIF that also mangles
13px type. Note ffmpeg can encode animated WebP but cannot decode it, so to
check the output, parse the RIFF chunks and count `ANMF` frames rather than
trying to read it back.

Needs Playwright's Chromium (`python -m playwright install chromium`) and, for
the mux, any ffmpeg with libx264 and aac — `pip install imageio-ffmpeg` brings
one if the system has none.

## Why these are stranger than they look

Playwright records a flat viewport. It has no camera, no pointer and no
narration, so all three are built:

- **The camera** is an eased transform on `#shell`. Two coordinate spaces are
  in play: `getBoundingClientRect()` says where a thing is on *screen*, which is
  what a click needs, while the camera needs where it sits in the *layout* to
  keep it centred as the scale changes. `push()` converts between them, which is
  why clicks still land at 150%.
- **Raster scale matters more than it looks.** Chromium rasterises a
  transformed layer once and then scales the texture, so a camera push enlarges
  a bitmap rather than re-rendering. `--raster` has to exceed the deepest push
  or every zoom is soft. Default 3.
- **The pointer is drawn**, because Chromium's screencast composites the page
  without a cursor — there is none in the frames at all. It is the real arrow at
  real size with the hotspot at the tip, switching to the hand over anything
  whose computed `cursor` is `pointer`, and dipping under a click.
- **Everything is styled through the CSSOM.** The page is served under
  `style-src 'self'`; a style attribute would be refused by the browser.

Cursor moves follow a slight arc and ease in and out. A straight line at
constant speed is the one thing that reads as a machine immediately.

## Re-cutting

The shot list is the body of `film()` — a flat sequence of moves, captions and
holds. Changing the film means editing that list, not recording it again by
hand.
