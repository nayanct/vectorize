# vectorize

Turn raster images (PNG/JPG/WEBP/BMP/TIFF) into clean SVG vector art using
LAB color-space quantization + contour tracing. Two ways to use it:

- **`desktop/`** — the original standalone Tkinter app. Drop a file, tweak
  sliders, get an SVG next to the script.
- **`backend/` + `frontend/`** — the same vectorization engine behind a
  small HTTP API, with a static web UI on top. This is what you'd deploy
  (e.g. frontend on GitHub Pages, backend on a small server/host) so other
  people can use it in a browser.

```
vectorize/
  README.md
  desktop/
    vectorize.py          # standalone Tkinter GUI app
  backend/
    app.py                 # FastAPI server (jobs, progress, cancel, download)
    vectorizer.py           # the vectorization pipeline (no GUI deps)
    eta.py                  # smoothed ETA estimator shared by the job runner
    requirements.txt
  frontend/
    index.html
    styles.css
    app.js
```

## Desktop app

```bash
python -m pip install pillow numpy opencv-python tkinterdnd2
python desktop/vectorize.py
```

Outputs are written next to the script as `<name>_vectorized.svg`.

Changes from the earlier version:
- **Reliable ETA.** The estimate used to be a single `elapsed / progress`
  ratio, which jumped around every time the job moved between stages of
  very different cost (color clustering vs. tracing vs. cleanup). It's now
  a smoothed rolling-rate estimate (`ETATracker` in `vectorize.py`), so the
  number moves gradually instead of jumping.
- **Cancel button.** Sits next to Vectorize once a job is running. It sets
  a `threading.Event` that the pipeline checks between chunks of work
  (color clustering batches, per-label cleanup, per-label tracing), so a
  cancel takes effect within a fraction of a second rather than only at
  the end of the current file.
- Progress text is a little more specific about which file/stage is
  active in multi-file batches.

The visual style (black background, canvas-drawn buttons/sliders, red
accent) is unchanged.

## Web app

### Run the backend locally

```bash
cd backend
python -m venv .venv && source .venv/bin/activate   # optional but recommended
pip install -r requirements.txt
uvicorn app:app --reload --port 8000
```

This starts the API at `http://localhost:8000`. Check it's alive:

```bash
curl http://localhost:8000/api/health
```

### Run the frontend locally

The frontend is fully static — no build step. Easiest way to serve it
locally (opening `index.html` directly via `file://` will hit CORS/fetch
issues in most browsers):

```bash
cd frontend
python -m http.server 5500
```

Then open `http://localhost:5500`. By default it talks to
`http://localhost:8000` (see `API_BASE_URL` at the top of `app.js`).

### How it works

1. User drops/selects an image and adjusts detail / color accuracy /
   anti-gap overlap / preserve-transparency in the browser.
2. On **Vectorize**, the frontend uploads the file + settings to
   `POST /api/jobs`, which returns a `job_id` and starts the pipeline on a
   background thread.
3. The frontend polls `GET /api/jobs/{job_id}` every 500ms for
   `state`, `progress`, `stage`, and `eta_seconds`.
4. **Cancel** calls `POST /api/jobs/{job_id}/cancel`, which sets the job's
   cancel flag; the pipeline checks it between chunks of work and stops
   promptly.
5. When `state` is `"done"`, the frontend fetches
   `GET /api/jobs/{job_id}/download` and offers it as a file download.

Jobs live in memory only (no disk/database) and are swept after 30 minutes
if nobody collects the result — fine for a small single-instance
deployment, not meant to survive a server restart.

### Deploying

**Frontend → GitHub Pages.** Push the `frontend/` folder's contents (or
point Pages at that folder) — it's plain static HTML/CSS/JS.

Before deploying, point it at your real backend URL. Either edit
`API_BASE_URL` in `frontend/app.js`, or set it from `index.html` before
`app.js` loads:

```html
<script>window.VECTORIZE_API_BASE_URL = "https://your-backend.example.com";</script>
<script src="app.js"></script>
```

**Backend → any Python host that isn't GitHub Pages** (Pages only serves
static files). Render, Railway, Fly.io, a small VPS, etc. all work — it's
a standard FastAPI app:

```bash
uvicorn app:app --host 0.0.0.0 --port $PORT
```

Make sure the host you pick serves HTTPS if your GitHub Pages site is
HTTPS (it will be); browsers block HTTPS pages from calling an HTTP API.

`CORSMiddleware` in `app.py` currently allows all origins (`*`) so the
GitHub Pages frontend can call it regardless of domain. Tighten
`allow_origins` to your Pages URL once you know it, if you want to lock
that down.

## Vectorization settings

| Setting | What it does |
|---|---|
| Detail (1–10) | Controls working resolution, smoothing strength, and path simplification. Higher keeps finer edges and more small shapes, and takes longer. |
| Color accuracy (8–192) | Target color count sampled via k-means in LAB space, with region colors taken from the median of original pixels (not the cluster center) for truer color. |
| Anti-gap overlap | Slightly dilates each color region and draws a matching-color stroke, so adjacent regions overlap a hair instead of leaving hairline gaps between paths. |
| Preserve transparency | Keeps transparent source pixels out of the traced regions instead of compositing them onto a background color. |
