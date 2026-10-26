# Ledger Reconciliation

React + TypeScript frontend with a FastAPI backend, deployed on Vercel.

- `app.py` is the original Streamlit app and the **single source of the reconciliation logic**.
  The backend (`api/_engine.py`) executes `app.py` from the first line up to
  `def display_matching_engine(` unchanged, so every parser, FY summary, matching rule and
  Excel export is the same code. Edit logic in `app.py` only.
- `api/_pipeline.py` is a line-for-line port of the Streamlit UI flow (READ LEDGERS, FY
  selector, summaries, matching engine, downloads); `api/_ui.py` records its output for React.
- `api/blob-upload.ts` lets the browser upload large files to Vercel Blob
  (Vercel functions accept at most 4.5 MB per request).
- `src/` is the React app.

## Run locally

Needs Python 3.12 (tick "Add python.exe to PATH" when installing) and Node.js 20+.

After cloning, install everything once (creates `.venv`, installs `requirements.txt` into it,
then runs `npm install`):

```bash
npm run setup
```

Then start the app (FastAPI on port 8000 + React on port 5173, opens the browser):

```bash
npm start
```

Press Ctrl+C to stop both. Locally, files are sent straight to the FastAPI server
(`.env.development` sets `VITE_UPLOAD_MODE=direct`), so no Blob store is needed.

The original Streamlit app still runs too:

```bash
.venv\Scripts\python -m pip install -r requirements-streamlit.txt
.venv\Scripts\python -m streamlit run app.py
```

## Deploy on Vercel

1. Push this repository to GitHub.
2. In Vercel: **Add New → Project**, import the repository. Settings come from `vercel.json`
   (Vite build to `dist/`, Python function `api/index.py`, Node function `api/blob-upload.ts`).
3. In the project: **Storage → Create → Blob**, and connect it to the project. This adds the
   `BLOB_READ_WRITE_TOKEN` environment variable that `api/blob-upload.ts` needs.
4. Redeploy. The app is served at your `*.vercel.app` URL.

### Vercel limits to know

- Each request (READ LEDGERS, changing FY or tolerance) can run up to 300 seconds (`maxDuration`).
- Uploaded ledgers are stored in Vercel Blob under `ledgers/` with an unguessable URL.
  Delete old files from the Blob store in the Vercel dashboard when no longer needed.
- Responses (tables + Excel downloads) are gzip-compressed and must stay under 4.5 MB
  compressed; very large ledgers with tens of thousands of rows can hit this.
