"""
highlight_export.py  (place next to app.py)

Highlights ONLY the matched amount (yellow) inside the ORIGINAL uploaded ledger:
    PDF   -> same PDF, yellow box over the matched amount
    Excel -> same workbook, yellow fill on the matched amount cell only
    (.xls / .csv are converted to .xlsx because they cannot hold cell colours)

Requires:  pip install pypdf reportlab pdfplumber openpyxl
"""
import io
import re
from pathlib import Path

import pandas as pd

TOL = 0.005
PDF_MIME = "application/pdf"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

_NUM_RE = re.compile(r"^\(?-?[\d,]+(?:\.\d+)?\)?(?:\s*(?:DR|CR))?$", re.I)
_DATE_RE = re.compile(r"^\d{1,2}[-/][\dA-Za-z]{1,3}[-/]\d{2,4}$")


# ---------------------------------------------------------------- helpers
def _token_amount(text):
    t = str(text or "").replace("₹", "").strip()
    if not t or _DATE_RE.match(t) or not _NUM_RE.match(t):
        return None
    m = re.search(r"[\d,]+(?:\.\d+)?", t)
    try:
        return abs(float(m.group(0).replace(",", "")))
    except Exception:
        return None


def _cell_amount(v):
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return None if pd.isna(v) else abs(float(v))
    if isinstance(v, str):
        return _token_amount(v)
    return None


def _same(a, b):
    return a is not None and abs(a - b) <= TOL


def _norm(v):
    return re.sub(r"\s+", " ", str(v or "")).strip().lower()


def _group_words(words, tolerance=2.5):
    """Identical to the app's _group_pdf_words_by_line (row index must agree)."""
    lines = []
    for word in sorted(words, key=lambda w: (w.get("top", 0), w.get("x0", 0))):
        for line in lines:
            if abs(float(line[0].get("top", 0)) - float(word.get("top", 0))) <= tolerance:
                line.append(word)
                break
        else:
            lines.append([word])
    for line in lines:
        line.sort(key=lambda w: w.get("x0", 0))
    return lines


# ---------------------------------------------------------------- PDF
def _party_pdf_boxes(pdf, rows):
    boxes, missed, cache = [], 0, {}
    for r in rows.to_dict("records"):
        try:
            page_no, idx, amt = int(r["Source Page"]), int(r["Source Row"]), float(r["Amount"])
        except Exception:
            missed += 1
            continue
        if page_no not in cache:
            try:
                words = pdf.pages[page_no - 1].extract_words(use_text_flow=True)
            except Exception:
                words = []
            cache[page_no] = _group_words(words)
        lines = cache[page_no]
        hits = []
        if 0 <= idx < len(lines):
            hits = [w for w in lines[idx] if _same(_token_amount(w["text"]), amt)]
        if hits:
            boxes.append((page_no, min(hits, key=lambda w: w["x0"])))  # Dr/Cr columns sit left of Balance
        else:
            missed += 1
    return boxes, missed


def _our_pdf_boxes(pdf, rows):
    boxes, missed, cache, used = [], 0, {}, set()
    recs = sorted(
        rows.to_dict("records"),
        key=lambda r: (int(r.get("__source_page", 0) or 0), int(r.get("__source_row", 0) or 0)),
    )
    for r in recs:
        try:
            page_no = int(r["__source_page"])
            amt = abs(float(r.get("SAP Net Amount", r.get("__amount"))))
            date_txt = pd.Timestamp(r["__date"]).strftime("%d-%b-%y").lower()
        except Exception:
            missed += 1
            continue
        if page_no not in cache:
            try:
                cache[page_no] = pdf.pages[page_no - 1].extract_words()
            except Exception:
                cache[page_no] = []
        words = cache[page_no]
        date_words = [w for w in words if w["text"].strip().lower() == date_txt]
        cands = [
            w for w in words
            if _same(_token_amount(w["text"]), amt)
            and (page_no, round(w["x0"], 1), round(w["top"], 1)) not in used
        ]

        def dy(w):
            return min((abs(w["top"] - d["top"]) for d in date_words), default=999)

        cands = [w for w in cands if dy(w) <= 40]
        if not cands:
            missed += 1
            continue
        # same line as the date first, then leftmost (Debit/Credit before TDS/Balance)
        w = min(cands, key=lambda w: (0 if dy(w) <= 6 else dy(w), w["x0"]))
        used.add((page_no, round(w["x0"], 1), round(w["top"], 1)))
        boxes.append((page_no, w))
    return boxes, missed


def _highlight_pdf(data, kind, rows):
    """Yellow boxes drawn over the matched amounts (pypdf + reportlab, no DLLs needed)."""
    import pdfplumber
    from pypdf import PdfReader, PdfWriter
    from reportlab.pdfgen import canvas

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        boxes, missed = (_party_pdf_boxes if kind == "party" else _our_pdf_boxes)(pdf, rows)
        heights = {pn: float(pdf.pages[pn - 1].height) for pn, _ in boxes}

    by_page = {}
    for pn, w in boxes:
        by_page.setdefault(pn, []).append(w)

    reader = PdfReader(io.BytesIO(data))
    writer = PdfWriter()
    for i, page in enumerate(reader.pages, start=1):
        if i in by_page:
            mb = page.mediabox
            buf = io.BytesIO()
            c = canvas.Canvas(buf, pagesize=(float(mb.right), float(mb.top)))
            c.setFillColorRGB(1, 1, 0, alpha=0.55)
            h = heights[i]
            for w in by_page[i]:
                x0, x1 = w["x0"] - 1, w["x1"] + 1
                y = h - w["bottom"] - 1
                c.rect(x0, y, x1 - x0, (w["bottom"] - w["top"]) + 2, stroke=0, fill=1)
            c.save()
            buf.seek(0)
            overlay = PdfReader(buf).pages[0]
            page.merge_page(overlay)
        writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue(), len(boxes), missed


# ---------------------------------------------------------------- Excel
def _open_workbook(data, name):
    from openpyxl import Workbook, load_workbook

    ext = Path(name).suffix.lower()
    if ext in (".xlsx", ".xlsm"):
        return (load_workbook(io.BytesIO(data)),
                load_workbook(io.BytesIO(data), data_only=True))

    # .xls / .csv -> rebuild as xlsx (cell positions are kept 1:1)
    if ext == ".csv":
        sheets = [("CSV", pd.read_csv(io.BytesIO(data), header=None, dtype=object,
                                      keep_default_na=False, sep=None, engine="python"))]
    else:
        xl = pd.ExcelFile(io.BytesIO(data))
        sheets = [(s, xl.parse(s, header=None, dtype=object)) for s in xl.sheet_names]
    wb = Workbook()
    wb.remove(wb.active)
    for sheet_name, df in sheets:
        ws = wb.create_sheet(title=str(sheet_name)[:31])
        for row in df.itertuples(index=False):
            ws.append([
                None if (v is None or v == "" or (not isinstance(v, str) and pd.isna(v))) else v
                for v in row
            ])
    return wb, wb


def _find_cell(ws, wsv, row, col, amt):
    def numeric_ok(c):
        return _same(_cell_amount(wsv.cell(c.row, c.column).value), amt)

    if col:
        c = ws.cell(row, col)
        if numeric_ok(c):
            return c
        f = c.value
        if wsv.cell(row, col).value is None and isinstance(f, str) and f.startswith("="):
            return c  # formula without cached value: trust the position
    for c in ws[row]:  # fallback: leftmost cell in that row holding the amount
        if numeric_ok(c):
            return c
    return None


def _our_excel_targets(wb, sheet, header, rows):
    ws = wb[sheet] if sheet in wb.sheetnames else wb.worksheets[0]
    hrow, hcol, want = 1, None, _norm(header)
    if want:
        for r in range(1, min(ws.max_row, 60) + 1):
            for c in ws[r]:
                if c.value is not None and _norm(c.value) == want:
                    hrow, hcol = r, c.column
                    break
            if hcol:
                break
    out = []
    for r in rows.to_dict("records"):
        amt = abs(float(r.get("SAP Net Amount", r.get("__amount"))))
        out.append((ws.title, hrow + 1 + int(r["Our Row"]), hcol, amt))
    return out


def _party_excel_targets(rows):
    out = []
    for r in rows.to_dict("records"):
        col = r.get("Excel Col")
        col = int(col) if col is not None and not pd.isna(col) else None
        out.append((str(r["Source Page"]), int(r["Source Row"]), col, float(r["Amount"])))
    return out


def _highlight_excel(data, name, kind, rows, sheet, header):
    from openpyxl.styles import PatternFill

    wb, wbv = _open_workbook(data, name)
    targets = (_our_excel_targets(wb, sheet, header, rows) if kind == "our"
               else _party_excel_targets(rows))
    fill = PatternFill("solid", start_color="FFFF00", end_color="FFFF00")
    done = missed = 0
    for sheet_name, row, col, amt in targets:
        if sheet_name not in wb.sheetnames:
            missed += 1
            continue
        cell = _find_cell(wb[sheet_name], wbv[sheet_name], row, col, amt)
        if cell is None:
            missed += 1
            continue
        cell.fill = fill
        done += 1
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue(), done, missed


# ---------------------------------------------------------------- public API
def build_highlighted_ledger(kind, file_bytes, file_name, matched_rows,
                             sheet=None, amount_header=None):
    """
    kind          'our' or 'party'
    file_bytes    original uploaded file (bytes)
    matched_rows  DataFrame of the MATCHED rows of that ledger
    Returns dict(data, ext, mime, highlighted, missed)
    """
    rows = matched_rows if matched_rows is not None else pd.DataFrame()
    if str(file_name).lower().endswith(".pdf"):
        data, done, missed = _highlight_pdf(file_bytes, kind, rows)
        return {"data": data, "ext": ".pdf", "mime": PDF_MIME,
                "highlighted": done, "missed": missed}
    data, done, missed = _highlight_excel(file_bytes, file_name, kind, rows, sheet, amount_header)
    return {"data": data, "ext": ".xlsx", "mime": XLSX_MIME,
            "highlighted": done, "missed": missed}
