"""
Line-for-line port of the Streamlit UI section of app.py (display_matching_engine,
display_summary, READ LEDGERS and DISPLAY blocks).

Every calculation calls the original app.py functions through api/_engine.py.
The differences from app.py are only what a stateless web request requires:
  * `st.<call>` becomes `ui.<call>` (recorded for the React frontend).
  * Widget values arrive as request parameters instead of `st.number_input` etc.
  * st.session_state lives in a per-request dict. Ledgers read by READ LEDGERS
    are re-read from the same files on later requests (with the checkbox value
    from READ time), which gives the same objects Streamlit kept in memory.
"""
import copy
import hashlib
import io
import threading
from collections import OrderedDict
from urllib.parse import urlparse
from urllib.request import urlopen

import pandas as pd

from _engine import engine, use_session_state
from _ui import UI

format_amount = engine.format_amount
balance_text = engine.balance_text
get_fy = engine.get_fy
extract_our_pdf_transactions = engine.extract_our_pdf_transactions
prepare_our_ledger = engine.prepare_our_ledger
extract_our_closing_metadata = engine.extract_our_closing_metadata
calculate_our_summary = engine.calculate_our_summary
extract_party_pdf_transactions = engine.extract_party_pdf_transactions
extract_party_excel_transactions = engine.extract_party_excel_transactions
extract_party_metadata = engine.extract_party_metadata
calculate_party_summary = engine.calculate_party_summary
build_our_matching_df = engine.build_our_matching_df
match_transactions = engine.match_transactions
_calculate_fy_withholding_tax = engine._calculate_fy_withholding_tax
generate_reconciliation_excel = engine.generate_reconciliation_excel
find_column = engine.find_column
_matched_rows_for_export = engine._matched_rows_for_export
build_highlighted_ledger = engine.build_highlighted_ledger

BLOB_HOST_SUFFIX = ".blob.vercel-storage.com"


# =========================================================
# UPLOADED FILES
# =========================================================
class UploadedFile(io.BytesIO):
    """Same surface as Streamlit's UploadedFile: a BytesIO with name and size."""

    def __init__(self, data, name):
        super().__init__(data)
        self.name = name
        self.size = len(data)


def _download_blob(url):
    parsed = urlparse(url)
    if parsed.scheme != "https" or not (parsed.hostname or "").endswith(BLOB_HOST_SUFFIX):
        raise ValueError("File URL is not a Vercel Blob URL.")
    with urlopen(url, timeout=120) as response:
        return response.read()


class FileSource:
    """A ledger file sent inline with the request or stored in Vercel Blob."""

    def __init__(self, ref, inline_files):
        self.name = ref["name"]
        self._url = ref.get("url")
        self._data = inline_files.get(ref["part"]) if ref.get("part") else None
        if self._data is None and not self._url:
            raise ValueError(f"File {self.name} was not sent with the request.")

    @property
    def cache_key(self):
        if self._url:
            return f"{self.name}|{self._url}"
        return f"{self.name}|{hashlib.sha256(self._data).hexdigest()}"

    def open(self):
        if self._data is None:
            self._data = _download_blob(self._url)
        return UploadedFile(self._data, self.name)


def _file_source(ref, inline_files):
    return FileSource(ref, inline_files) if ref else None


# =========================================================
# READ LEDGERS
# =========================================================
_READ_CACHE = OrderedDict()
_READ_CACHE_SIZE = 4
_READ_CACHE_LOCK = threading.Lock()


def _read_ledgers(our_file, party_file, ui):
    """Port of the READ LEDGERS block. Returns the session values it sets."""
    state = {}

    for key in [
        "our_df", "party_transactions", "party_pages",
        "party_page_count", "our_sheet"
    ]:
        state[key] = None if key == "our_df" else (
            [] if key in ["party_transactions", "party_pages"] else
            0 if key == "party_page_count" else None
        )

    # ---------------- Our Ledger ----------------
    try:
        if our_file.name.lower().endswith(".pdf"):
            our_df = extract_our_pdf_transactions(our_file)
            our_sheet = "PDF Ledger"
            ui.info("📘 Our Ledger PDF detected: standard Debit / Credit / TDS format")
        else:
            our_file.seek(0)
            excel = pd.ExcelFile(our_file)
            sheets = excel.sheet_names
            ui.info("📘 Our Ledger sheets found: " + ", ".join(sheets))

            our_sheet = "SAPUI5 Export" if "SAPUI5 Export" in sheets else sheets[0]
            our_file.seek(0)
            our_df = pd.read_excel(our_file, sheet_name=our_sheet)
    except Exception as e:
        ui.error(f"❌ Could not read Our Ledger: {e}")
        our_df = None
        our_sheet = None

    # ---------------- Party Ledger ----------------
    if our_df is not None:
        try:
            if party_file.name.lower().endswith(".pdf"):
                (
                    party_transactions,
                    party_pages,
                    party_page_count,
                    party_metadata
                ) = extract_party_pdf_transactions(party_file)
            else:
                party_transactions, party_metadata = extract_party_excel_transactions(party_file)
                party_pages = []
                party_page_count = 0
        except Exception as e:
            ui.error(f"❌ Could not read Party Ledger: {e}")
            party_transactions = []
            party_pages = []
            party_page_count = 0
            party_metadata = {"manual_review": True, "error": str(e)}

        (
            party_name,
            party_closing_date,
            party_closing_balance,
            party_last_entry_date,
        ) = extract_party_metadata(
            party_file,
            party_pages=party_pages,
            party_transactions=party_transactions,
            party_metadata=party_metadata,
        )

        state["our_df"] = our_df
        state["party_transactions"] = party_transactions
        state["party_pages"] = party_pages
        state["party_page_count"] = party_page_count
        state["our_sheet"] = our_sheet
        state["loaded_our_name"] = our_file.name
        state["loaded_party_name"] = party_file.name
        state["our_file_bytes"] = our_file.getvalue()
        state["our_file_name"] = our_file.name
        state["party_file_bytes"] = party_file.getvalue()
        state["party_file_name"] = party_file.name
        state["party_name"] = party_name
        state["party_closing_date"] = party_closing_date
        state["party_closing_balance"] = party_closing_balance
        state["party_last_entry_date"] = party_last_entry_date
        state["party_opening_balance"] = party_metadata.get("opening_balance")
        state["party_manual_review"] = bool(party_metadata.get("manual_review", False))
        state["party_parser_diagnostics"] = party_metadata
        state["ledgers_loaded"] = True

        if state["party_manual_review"]:
            ui.warning(
                "⚠️ Party Ledger was uploaded, but some or all rows could not be "
                "automatically normalized. Review the Party Ledger manually; the original file was not changed."
            )
        else:
            ui.success("✅ Universal Party Ledger normalized successfully.")
    else:
        state["ledgers_loaded"] = False

    return state


def _read_ledgers_cached(our_source, party_source, from_our_books, session):
    """READ LEDGERS result for these files, plus the messages it showed."""
    key = (our_source.cache_key, party_source.cache_key, bool(from_our_books))

    with _READ_CACHE_LOCK:
        cached = _READ_CACHE.get(key)
        if cached is not None:
            _READ_CACHE.move_to_end(key)

    if cached is None:
        # The Party Excel sign flip is read while parsing, so parse with the
        # checkbox value that was active when READ LEDGERS was clicked.
        current_flag = session.get("party_excel_from_our_books", False)
        session["party_excel_from_our_books"] = bool(from_our_books)
        try:
            read_ui = UI()
            state = _read_ledgers(our_source.open(), party_source.open(), read_ui)
            cached = (read_ui.elements, state)
        finally:
            session["party_excel_from_our_books"] = current_flag

        with _READ_CACHE_LOCK:
            _READ_CACHE[key] = cached
            while len(_READ_CACHE) > _READ_CACHE_SIZE:
                _READ_CACHE.popitem(last=False)

    elements, state = cached
    return copy.deepcopy(elements), copy.deepcopy(state)


# =========================================================
# HIGHLIGHTED LEDGERS
# =========================================================
def render_highlighted_exports(ui, session, our_match, party_match, matched, selected_fy, amount_tolerance):
    """Button + download buttons: highlighted ORIGINAL ledgers (PDF -> PDF, Excel -> Excel)."""
    ui.subheader("🖍️ Highlighted Ledgers (same format as uploaded)")
    ui.caption("PDF → highlighted PDF. Excel → highlighted Excel (matched amount cells only).")

    sig = (selected_fy, float(amount_tolerance), len(matched),
           session.get("loaded_our_name"), session.get("loaded_party_name"))

    if ui.button("🖍️ Generate Highlighted Ledgers", key="gen_highlight"):
        our_rows, party_rows = _matched_rows_for_export(
            our_match, party_match, matched, amount_tolerance)
        our_df_raw = session.get("our_df")
        header = (find_column(our_df_raw, ["Amount (CoCode Crcy)", "Amount",
                                           "Amount (Company Code Currency)"])
                  if our_df_raw is not None else None)
        res = {"sig": sig}
        for kind, rows in (("our", our_rows), ("party", party_rows)):
            try:
                res[kind] = build_highlighted_ledger(
                    kind,
                    session[f"{kind}_file_bytes"],
                    session[f"{kind}_file_name"],
                    rows,
                    sheet=session.get("our_sheet") if kind == "our" else None,
                    amount_header=header if kind == "our" else None,
                )
            except Exception as e:
                res[kind] = {"error": str(e)}
        session["hl_results"] = res

    res = session.get("hl_results")
    if res and res.get("sig") == sig:
        fy = str(selected_fy).replace(" ", "_")
        for kind, label in (("our", "Our_Ledger"), ("party", "Party_Ledger")):
            out = res.get(kind, {})
            if "error" in out:
                ui.warning(f"⚠️ {label}: {out['error']}")
                continue
            ui.download_button(
                f"📥 {label.replace('_', ' ')} — highlighted ({out['ext'].upper()[1:]})",
                data=out["data"],
                file_name=f"{label}_Highlighted_{fy}{out['ext']}",
                mime=out["mime"],
                key=f"dl_hl_{kind}",
            )
            msg = f"{label.replace('_', ' ')}: {out['highlighted']} amounts highlighted"
            if out["missed"]:
                ui.warning(f"{msg}; {out['missed']} could not be located in the file.")
            else:
                ui.caption(msg)


# =========================================================
# MATCHING ENGINE
# =========================================================
def display_matching_engine(
    ui,
    session,
    prepared_our,
    party_df_all,
    selected_fy,
    date_tolerance,
    amount_tolerance,
    our_summary=None,
    party_summary=None,
    party_name="Party Ledger",
    party_closing_date=None,
    party_closing_balance=None,
    party_last_entry_date=None,
    our_closing_date=None,
    our_closing_balance=None,
    our_last_entry_date=None,
    our_last_entry_fy=None
):
    ui.divider()
    ui.header(f"🔎 Transaction Matching Engine — {selected_fy}")

    our_match = build_our_matching_df(prepared_our, selected_fy, amount_tolerance)
    party_match = party_df_all[
        party_df_all["FY"] == selected_fy
    ].copy() if not party_df_all.empty else pd.DataFrame()

    if our_match.empty:
        ui.warning(
            f"⚠️ Matching engine found 0 Our Ledger transactions for {selected_fy}. "
            "Check the Journal Entry Date and Amount columns in the SAP export."
        )
        ui.caption(
            f"Prepared Our Ledger rows: {0 if prepared_our is None else len(prepared_our):,} | "
            f"Selected FY rows: {0 if prepared_our is None else int((prepared_our.get('__fy') == selected_fy).sum()):,}"
        )
        return

    if party_match.empty:
        ui.warning(
            f"⚠️ Matching engine found 0 Party Ledger transactions for {selected_fy}. "
            "Check that the Party Ledger dates are parsed correctly and belong to this FY."
        )
        ui.caption(
            f"Party rows loaded: {len(party_df_all):,} | "
            f"Party rows for selected FY: 0"
        )
        return

    matched, unmatched_our, unmatched_party = match_transactions(
        our_match,
        party_match,
        date_tolerance,
        amount_tolerance
    )
    doc_mismatch = matched.attrs.get("doc_mismatch", pd.DataFrame())

    total_our = len(our_match)
    total_party = len(party_match)
    total_matched = len(matched)
    rate = (total_matched / total_our * 100) if total_our else 0

    c1, c2, c3, c4 = ui.columns(4)
    c1.metric("Our Transactions", f"{total_our:,}")
    c2.metric("Party Transactions", f"{total_party:,}")
    c3.metric("Matched", f"{total_matched:,}")
    c4.metric("Match Rate", f"{rate:.1f}%")

    ui.caption(
        "Matching uses opposite accounting sides: "
        "Our Debit ↔ Party Credit and Our Credit ↔ Party Debit. "
        "For SAP credit rows, Withholding Tax/TDS is added back to the "
        "negative SAP amount before matching the gross Party Ledger amount. "
        f"Amount matching allows a variance of ±₹{float(amount_tolerance):.2f}."
    )

    if not matched.empty:
        ui.subheader(f"✅ Matched Transactions ({len(matched):,})")
        ui.dataframe(
            matched,
            use_container_width=True,
            hide_index=True
        )
    else:
        ui.warning("No transactions matched with the current tolerances.")

    ui.subheader(f"⚠️ Invoice No. Mismatch ({len(doc_mismatch):,})")
    if doc_mismatch.empty:
        ui.success("Every matched invoice has the same invoice number on both sides.")
    else:
        ui.warning(
            "Amount and date match, but the invoice number differs, so these entries "
            "are kept unmatched and listed in the RECO statement. Check for an extra "
            "party invoice or a wrong invoice number in our books."
        )
        ui.dataframe(doc_mismatch, use_container_width=True, hide_index=True)

    c1, c2 = ui.columns(2)

    with c1:
        ui.subheader(f"❌ Unmatched Our Ledger ({len(unmatched_our):,})")
        if unmatched_our.empty:
            ui.success("All Our Ledger transactions are matched.")
        else:
            cols = [
                "Doc No. Check", "__date", "Match Side", "Match Amount",
                "Our Description", "Our Row"
            ]
            ui.dataframe(
                unmatched_our[[c for c in cols if c in unmatched_our.columns]],
                use_container_width=True,
                hide_index=True
            )

    with c2:
        ui.subheader(f"❌ Unmatched Party Ledger ({len(unmatched_party):,})")
        if unmatched_party.empty:
            ui.success("All Party Ledger transactions are matched.")
        else:
            cols = [
                "Doc No. Check", "Date", "Type", "Amount",
                "Particulars", "Vch Type", "Vch No."
            ]
            ui.dataframe(
                unmatched_party[[c for c in cols if c in unmatched_party.columns]],
                use_container_width=True,
                hide_index=True
            )

    ui.success(
        f"✅ Matching engine completed: {total_matched:,} matched, "
        f"{len(unmatched_our):,} unmatched Our Ledger, "
        f"{len(unmatched_party):,} unmatched Party Ledger."
    )

    render_highlighted_exports(ui, session, our_match, party_match, matched, selected_fy, amount_tolerance)

    # ---------------- Reconciliation format export ----------------
    session["reco_unmatched_our"] = unmatched_our.copy()
    session["reco_unmatched_party"] = unmatched_party.copy()
    session["reco_selected_fy"] = selected_fy

    ui.divider()
    ui.subheader("📄 Reconciliation Statement")
    ui.caption(
        "Stores all unmatched entries in your RECO FORMAT.xlsx layout: "
        "A-1 Party Debit, B-2 Party Credit, C-3 Our Debit, D-4 Our Credit."
    )

    try:
        reco_bytes = generate_reconciliation_excel(
            selected_fy,
            unmatched_our,
            unmatched_party,
            our_summary=our_summary,
            party_summary=party_summary,
            amount_tolerance=amount_tolerance,
            party_name=party_name,
            party_closing_date=party_closing_date,
            party_closing_balance=party_closing_balance,
            party_last_entry_date=party_last_entry_date,
            our_closing_date=our_closing_date,
            our_closing_balance=our_closing_balance,
            our_last_entry_date=our_last_entry_date,
            our_last_entry_fy=our_last_entry_fy,
            withholding_tax_total=_calculate_fy_withholding_tax(
                prepared_our, selected_fy, amount_tolerance
            ),
        )
        ui.download_button(
            "📥 Download Reconciliation Statement (RECO Format)",
            data=reco_bytes,
            file_name=f"RECO_{selected_fy.replace(' ', '_')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
            help="Downloads the unmatched transactions in the supplied RECO FORMAT.xlsx layout.",
        )
    except Exception as e:
        ui.warning(f"⚠️ RECO export is not available: {e}")


# =========================================================
# SUMMARY DISPLAY
# =========================================================
def display_summary(ui, title, summary):
    ui.subheader(title)
    ui.write(f"**Financial Year:** {summary['financial_year']}")

    c1, c2, c3 = ui.columns(3)
    c1.metric("Transactions", f"{summary['transactions']:,}")
    c2.metric("Total Debit", format_amount(summary["total_debit"]))
    c3.metric("Total Credit", format_amount(summary["total_credit"]))

    ui.divider()
    c1, c2 = ui.columns(2)

    with c1:
        ui.info(
            "🔵 Opening Balance\n\n"
            f"### {balance_text(summary['opening_balance'])}"
        )

    with c2:
        closing = summary["closing_balance"]
        if closing > 0:
            ui.success(
                "🟢 Closing Balance\n\n"
                f"### {balance_text(closing)}"
            )
        elif closing < 0:
            ui.warning(
                "🟠 Closing Balance\n\n"
                f"### {balance_text(closing)}"
            )
        else:
            ui.success(
                "🟢 Closing Balance\n\n"
                "### ₹0.00 Nil"
            )


# =========================================================
# DISPLAY
# =========================================================
def _display(ui, session, date_tolerance, amount_tolerance):
    our_df = session["our_df"]
    party_transactions = session["party_transactions"]
    party_pages = session["party_pages"]

    # ---------------- Prepare Our Ledger ----------------
    prepared_our = None
    our_error = None

    if our_df is not None:
        try:
            prepared_our, our_error = prepare_our_ledger(our_df)
            if prepared_our is not None:
                prepared_our = prepared_our.dropna(subset=["__date"]).copy()
                prepared_our["__fy"] = prepared_our["__date"].apply(get_fy)
        except Exception as e:
            prepared_our = None
            our_error = str(e)

    # ---------------- Prepare Party Ledger ----------------
    if party_transactions:
        party_df_all = pd.DataFrame(party_transactions)
        party_df_all["Date"] = pd.to_datetime(
            party_df_all["Date"], errors="coerce"
        )
        party_df_all["FY"] = party_df_all["Date"].apply(get_fy)
    else:
        party_df_all = pd.DataFrame(columns=[
            "Date", "Type", "Particulars", "Vch Type",
            "Vch No.", "Debit", "Credit", "Amount", "FY"
        ])

    # ---------------- Financial Years ----------------
    our_fys = (
        sorted(prepared_our["__fy"].dropna().unique().tolist())
        if prepared_our is not None and not prepared_our.empty
        else []
    )
    party_fys = (
        sorted(party_df_all["FY"].dropna().unique().tolist())
        if not party_df_all.empty
        else []
    )

    common_fys = sorted(set(our_fys) & set(party_fys))
    all_fys = sorted(set(our_fys) | set(party_fys))

    default_fy = (
        party_fys[-1] if party_fys
        else common_fys[-1] if common_fys
        else all_fys[-1] if all_fys
        else None
    )

    if session.get("selected_fy") not in all_fys:
        session["selected_fy"] = default_fy

    # ---------------- FY selector ----------------
    if all_fys:
        ui.divider()
        ui.header("📅 Financial Year")

        summary_fys = {"FY 2024-25", "FY 2025-26", "FY 2026-27"}
        selectable_fys = sorted(set(all_fys) | summary_fys)

        selected_fy = ui.selectbox(
            "Select Financial Year",
            selectable_fys,
            value=session["selected_fy"],
            key="selected_fy"
        )

        if selected_fy in common_fys:
            ui.success(
                f"✅ {selected_fy} is available in BOTH Our Ledger and Party Ledger."
            )
        elif selected_fy in summary_fys:
            ui.warning(
                f"ℹ️ {selected_fy} uses the new Ledger Summary carry-forward logic. "
                "The Our Ledger summary is calculated from the 31.12.2024 CL base."
            )
        else:
            ui.warning(
                f"⚠️ {selected_fy} is available in only one ledger."
            )
    else:
        selected_fy = None
        ui.error("❌ No Financial Year could be detected.")

    # ---------------- Our Entries ----------------
    if selected_fy is not None:
        ui.divider()
        ui.header(f"📘 Our Ledger Entries — {selected_fy}")

        if prepared_our is not None:
            our_fy_df = prepared_our[
                prepared_our["__fy"] == selected_fy
            ].copy()

            if not our_fy_df.empty:
                ui.success(
                    f"✅ {len(our_fy_df):,} Our Ledger transactions found for {selected_fy}."
                )
                display_our_df = our_fy_df.drop(
                    columns=["__date", "__amount", "__entry_type", "__fy"],
                    errors="ignore"
                )
                ui.dataframe(
                    display_our_df,
                    use_container_width=True,
                    hide_index=True
                )
            else:
                ui.info(f"ℹ️ No Our Ledger transactions were found for {selected_fy}.")

    our_summary = None
    party_summary = None

    # ---------------- Our Summary ----------------
    if selected_fy is not None and our_df is not None:
        ui.divider()
        our_summary, our_error = calculate_our_summary(
            our_df, selected_fy
        )
        if our_summary is not None:
            display_summary(ui, "📊 Our Ledger Summary", our_summary)
        else:
            ui.warning(
                f"⚠️ Our Ledger Summary could not be calculated for {selected_fy}: "
                f"{our_error}"
            )

    # ---------------- RECO MY BOOKS metadata ----------------
    if selected_fy is not None and prepared_our is not None:
        _our_reco_date, _our_reco_balance = extract_our_closing_metadata(
            prepared_our, selected_fy
        )
        if _our_reco_balance is not None:
            _our_reco_date_text = (
                _our_reco_date.strftime("%d/%m/%Y")
                if not pd.isna(_our_reco_date) else "last available date"
            )
            ui.caption(
                f"📌 RECO My Books = Our SAP Ledger calculated FY closing: "
                f"{format_amount(_our_reco_balance)} "
                f"as at {_our_reco_date_text}"
            )

    # ---------------- Party Entries ----------------
    if selected_fy is not None:
        ui.divider()
        ui.header(f"📕 Party Ledger Entries — {selected_fy}")

        if session.get("party_name"):
            ui.caption(f"Party Ledger: **{session.get('party_name')}**")
        parser_diag = session.get("party_parser_diagnostics", {})
        if parser_diag:
            loaded_count = len(party_transactions)
            if session.get("party_manual_review"):
                ui.warning(
                    f"⚠️ Universal parser status: Manual review recommended. "
                    f"Normalized transactions: {loaded_count:,}. "
                    f"The source file was not modified."
                )
            else:
                ui.success(
                    f"🔄 Universal parser: {loaded_count:,} transactions normalized "
                    "from the uploaded Party Ledger."
                )
        if session.get("party_closing_balance") is not None:
            close_date = pd.to_datetime(
                session.get("party_closing_date"), errors="coerce"
            )
            close_date_text = (
                close_date.strftime("%d/%m/%Y")
                if not pd.isna(close_date) else "last available date"
            )
            ui.info(
                f"📌 Party Ledger last available balance: "
                f"{format_amount(session.get('party_closing_balance'))} "
                f"as at {close_date_text}"
            )

        party_fy_df = party_df_all[
            party_df_all["FY"] == selected_fy
        ].copy()

        if not party_fy_df.empty:
            ui.success(
                f"✅ {len(party_fy_df):,} Party Ledger transactions found for {selected_fy}."
            )

            ui.dataframe(
                party_fy_df.drop(columns=["FY"], errors="ignore"),
                use_container_width=True,
                hide_index=True
            )
        else:
            ui.info(f"ℹ️ No Party Ledger transactions were found for {selected_fy}.")

    # ---------------- Party Summary ----------------
    if selected_fy is not None:
        ui.divider()
        party_fy_df = party_df_all[
            party_df_all["FY"] == selected_fy
        ].copy()

        if not party_fy_df.empty:
            party_summary = calculate_party_summary(
                party_fy_df.drop(columns=["FY"], errors="ignore").to_dict("records"),
                opening_balance=float(session.get("party_opening_balance") or 0.0),
                selected_fy=selected_fy,
                party_metadata=session.get("party_parser_diagnostics") or {}
            )
        else:
            party_summary = {
                "financial_year": selected_fy,
                "transactions": 0,
                "opening_balance": float(session.get("party_opening_balance") or 0.0),
                "total_debit": 0.0,
                "total_credit": 0.0,
                "closing_balance": 0.0
            }

        display_summary(ui, "📊 Party Ledger Summary", party_summary)

        if party_fy_df.empty:
            ui.info(
                f"ℹ️ No Party Ledger data was found for {selected_fy}. "
                "Party Ledger values are shown as ₹0.00."
            )

    # ---------------- Matching Engine ----------------
    if selected_fy is not None:
        our_closing_date, our_closing_balance = extract_our_closing_metadata(
            prepared_our, selected_fy
        )

        our_selected_dates = pd.to_datetime(
            prepared_our.loc[prepared_our["__fy"] == selected_fy, "__date"],
            errors="coerce"
        ).dropna() if prepared_our is not None and not prepared_our.empty else pd.Series(dtype="datetime64[ns]")
        our_last_entry_date_for_reco = (
            our_selected_dates.max() if not our_selected_dates.empty else pd.NaT
        )
        our_last_entry_fy_for_reco = (
            get_fy(our_last_entry_date_for_reco)
            if not pd.isna(our_last_entry_date_for_reco) else selected_fy
        )

        display_matching_engine(
            ui,
            session,
            prepared_our,
            party_df_all,
            selected_fy,
            date_tolerance,
            amount_tolerance,
            our_summary=our_summary,
            party_summary=party_summary,
            party_name=session.get("party_name", "Party Ledger"),
            party_closing_date=session.get("party_closing_date", pd.NaT),
            party_closing_balance=session.get("party_closing_balance"),
            party_last_entry_date=session.get("party_last_entry_date", pd.NaT),
            our_closing_date=our_closing_date,
            our_closing_balance=our_closing_balance,
            our_last_entry_date=our_last_entry_date_for_reco,
            our_last_entry_fy=our_last_entry_fy_for_reco,
        )

    # ---------------- Raw PDF ----------------
    if party_pages:
        with ui.expander("🔎 Show Raw Party PDF Text"):
            for page in party_pages:
                ui.markdown(f"**Page {page['Page']}**")
                ui.text(page["Text"])

    # ---------------- Settings / Status ----------------
    ui.divider()
    ui.subheader("⚙️ Reconciliation Settings")
    c1, c2 = ui.columns(2)
    c1.info(f"📅 Date tolerance: {date_tolerance} days")
    c2.info(f"💰 Amount tolerance: {amount_tolerance:.2f}")

    ui.divider()
    ui.success(
        f"✅ Currently displaying {selected_fy}. "
        "Changing the Financial Year changes both ledger entries, "
        "both summaries, and transaction matching."
    )


# =========================================================
# ONE RERUN OF THE PAGE
# =========================================================
DEFAULTS = {
    "ledgers_loaded": False,
    "our_df": None,
    "party_transactions": [],
    "party_pages": [],
    "party_page_count": 0,
    "our_sheet": None,
    "loaded_our_name": None,
    "loaded_party_name": None,
    "party_name": "Party Ledger",
    "party_closing_date": pd.NaT,
    "party_closing_balance": None,
    "party_last_entry_date": pd.NaT,
    "party_opening_balance": None,
    "party_manual_review": False,
    "party_parser_diagnostics": {},
    "selected_fy": None,
}


def run(params, inline_files):
    """
    One Streamlit-style rerun.

    params:
      read                          READ LEDGERS was clicked on this run
      our, party                    files currently in the uploaders ({name, url} or {name, part})
      loaded                        files of the last successful READ LEDGERS, with
                                    party_excel_from_our_books as it was at that click
      date_tolerance, amount_tolerance, party_excel_from_our_books, selected_fy
      clicked                       key of the button clicked on this run (st.button)
    """
    date_tolerance = int(params["date_tolerance"])
    amount_tolerance = float(params["amount_tolerance"])

    session = dict(DEFAULTS)
    session["party_excel_from_our_books"] = bool(params.get("party_excel_from_our_books", False))
    session["selected_fy"] = params.get("selected_fy")
    session["amount_tolerance"] = float(amount_tolerance)
    use_session_state(session)

    ui = UI(clicked=params.get("clicked"))
    loaded = params.get("loaded")
    read = bool(params.get("read"))
    our_source = _file_source(params.get("our"), inline_files) if read else None
    party_source = _file_source(params.get("party"), inline_files) if read else None
    read_succeeded = None

    # Session values kept from the last READ LEDGERS click (a fresh read replaces them).
    if loaded and not (read and our_source and party_source):
        _, state = _read_ledgers_cached(
            _file_source(loaded["our"], inline_files),
            _file_source(loaded["party"], inline_files),
            loaded.get("party_excel_from_our_books", False),
            session,
        )
        session.update(state)

    # ---------------- READ LEDGERS ----------------
    if read:
        if our_source is None:
            ui.warning("⚠️ Please upload the Our Ledger Excel / PDF file.")

        elif party_source is None:
            ui.warning("⚠️ Please upload the Party Ledger file.")

        else:
            session["selected_fy"] = None
            elements, state = _read_ledgers_cached(
                our_source,
                party_source,
                session["party_excel_from_our_books"],
                session,
            )
            ui.elements.extend(elements)
            session.update(state)
            read_succeeded = bool(state["ledgers_loaded"])

    # ---------------- DISPLAY ----------------
    if session["ledgers_loaded"]:
        _display(ui, session, date_tolerance, amount_tolerance)
    else:
        ui.info("👆 Upload both ledgers and click **READ LEDGERS** to start.")

    return {
        "elements": ui.elements,
        "ledgers_loaded": bool(session["ledgers_loaded"]),
        "read_succeeded": read_succeeded,
        "selected_fy": session.get("selected_fy"),
    }
