# tools

The scripts that make the product films. None is part of the package; they
exist so the film can be remade when the interface changes, rather than being
a one-off nobody can reproduce.

## Making the film

Record on the sample mailbox, so every name in the film is invented:

```sh
bmail --home /tmp/film init
bmail --home /tmp/film samples --out /tmp/film/samples
bmail --home /tmp/film ingest --path /tmp/film/samples/sample.mbox --account demo
bmail --home /tmp/film triage
bmail --home /tmp/film serve --port 8800 --json      # one shell; note the token

python tools/record_film.py <token> --cut full  --out film/full    # another
python tools/record_film.py <token> --cut short --out film/short
```

Each capture starts with the browser opening. Trim to the first frame of the
black title card (about 4.3 s in), write a score exactly as long as what is
left, and mux; the ffmpeg line is in the header of `compose_score.py`:

```sh
python tools/compose_score.py 110.0 --out bed.wav
```

The films in `media/` are `bmail-film.mp4` (the full tour, 1:49),
`bmail-film-short.mp4` (the 37-second walkthrough) and `bmail-launch.mp4` (a
minute, the one the README opens with; see below).

For the README itself the film has to become an **animated image**, because
GitHub's Markdown sanitiser deletes a `<video>` element and everything inside
it — the player and any fallback alike. Only `<img>` survives, so motion on a
README means an animated WebP:

```sh
ffmpeg -ss 14.587 -t 41.143 -i media/bmail-launch.mp4 \
  -vf "fps=10,scale=1000:-2:flags=lanczos" \
  -c:v libwebp_anim -lossless 0 -q:v 86 -compression_level 4 -loop 0 -an \
  media/film.webp
```

That is the middle of the launch film and nothing else: bars 9 to 32, from
the window arriving to the window leaving. Someone landing on the README is
already reading about Bmail; the six words and the mark that open the film
are for someone who is not, and here they would be fourteen seconds before
anything of the app is on screen. Cut this way the loop also closes on
itself: the window leaves, and arrives.

About 5.3 MB. The quality is that high for the film's dark room and white
window: at 72 both come out in blotches, because each frame is stored as its
difference from the last and the errors pile up. The width, 1000 rather than
1120, pays for it; a README column is no wider. The 37-second walkthrough as a
160-colour GIF was 13 MB and mangled 13px type as well.

ffmpeg can encode animated WebP but cannot decode it, so to check the output,
parse the RIFF chunks: count the `ANMF` frames and add up their durations, the
three bytes at offset 12 of each. Held frames are merged, so the count is under
ten a second; the durations still add up to the length of the cut.

Needs Playwright's Chromium (`python -m playwright install chromium`) and, for
the mux, any ffmpeg with libx264 and aac — `pip install imageio-ffmpeg` brings
one if the system has none.

## The launch cut

`media/bmail-launch.mp4` is a third film, a minute long, for a landing page
rather than a README: the app as a window in a dark room, the mark arriving,
type big enough to carry a line on its own. Same app, same sample mailbox, and
still no mockups; the window is the page itself, moved.

It is cut to a piece of music. The shot list is written in bars, so the tempo
and where the first bar starts are all it needs to know about the track:

```sh
python tools/record_film.py <token> --cut launch --raster 2 --gpu --slow 6 \
  --bpm 140 --downbeat 0.873 --out film/launch

cd film/launch
ffmpeg -f concat -safe 0 -i frames.txt -i track.mp3 \
  -filter_complex "[0:v]fps=60,scale=in_range=pc:out_range=tv:in_color_matrix=bt601:out_color_matrix=bt709,gradfun=1.4:24,format=yuv420p[v];[1:a]atrim=duration=66,adelay=100|100,loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000,afade=t=in:d=0.3,afade=t=out:st=63.4:d=2.6[a]" \
  -map "[v]" -map "[a]" -t 66.017 \
  -c:v libx264 -preset slow -crf 16 -x264-params aq-mode=3 \
  -color_range tv -colorspace bt709 -color_primaries bt709 -color_trc bt709 \
  -c:a aac -b:a 192k -movflags +faststart bmail-launch.mp4
```

The track is by lnplusmusic, from Pixabay, downloaded as
`lnplusmusic-tech-technology-presentation-music-470651.mp3`. It is not in the
repository: Pixabay's licence lets a film use it and does not let
anyone hand the file on by itself. It is 140 beats a minute and built the way
the film needs: eight quiet bars, the beat comes in, and twenty-four bars later
it drops away again. The words take the quiet bars, the window arrives with the
beat, each of the six things it shows has four bars, and the last line gets the
quiet. For another track, measure its tempo and first downbeat and pass those;
a track with a different shape wants `film_launch()` re-cut to it.

Without music, `python tools/compose_score.py --cues film/launch/cues.json
--peak -7 --out score.wav` still writes a bed, with a thump under each moment
in the cue sheet the recording leaves behind.

It is recorded differently from the other two:

- **Frames, not a video.** Playwright's recorder encodes at a megabit a
  second, which a white page survives and a dark gradient does not. This cut
  keeps the JPEG frames Chromium hands over, with the time each arrived, and
  ffmpeg encodes them once. `gradfun` and `aq-mode=3` are there for the same
  dark gradient; the `scale` turns the JPEGs' full-range colour into what
  players expect. A take is around two gigabytes of frames.
- **Acted out slowly.** A headless browser draws five to ten of these frames a
  second. `--slow 6` runs the whole film six times slower and divides every
  timestamp by six on the way out, which is sixty frames a second from a
  browser managing ten. `--gpu` roughly doubles what it manages.
- **The page keeps the time.** A page busy drawing a large window can take
  four tenths of a second of film to hear from the recorder: nearly a beat.
  So the recorder never says "now". It tells the stage ahead of time what to
  do at which second, and the stage does it on that frame, by the same slowed
  clock its animations run on. The recording ends by printing how many of
  those it was told too late; it should say none.
- **The music comes in a tenth of a second late.** That `adelay=100`. What the
  page does at a given moment reaches a captured frame about a tenth of a
  second of film later while the window is on screen, and hardly later at all
  when it is not. Delaying the music by that much puts the cuts inside the
  window scenes on the beat and leaves the type-only scenes slightly ahead of
  it, which the eye forgives; behind it, it does not.

The stage is `launch_stage.js`, its controls and the frame-keeping are in
`launch_cut.py`, and the shot list is `film_launch()` in `record_film.py`.

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
- **The drawer moves with the camera.** It is fixed to the viewport on
  `<body>`, outside `#shell`, so the camera gives it the same transform with
  its origin set to the viewport's corner; otherwise a push would zoom the
  board behind it and leave the drawer where it was.
- **Everything is styled through the CSSOM.** The page is served under
  `style-src 'self'`; a style attribute would be refused by the browser.
- **Slowing the page slows one clock, not both.** `Animation.setPlaybackRate`
  slows CSS, Web Animations and the timestamp each animation frame is handed.
  `performance.now()` carries on at full speed. A move that starts its stopwatch
  with one and reads it with the other runs away, so the launch cut's camera
  keeps to `document.timeline` throughout.

Cursor moves follow a slight arc and ease in and out. A straight line at
constant speed is the one thing that reads as a machine immediately.

## Re-cutting

The shot lists are the bodies of `film_full()` and `film_short()`: flat
sequences of moves, captions and holds. Changing the film means editing that list, not recording it again by
hand.
