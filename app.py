import streamlit as st
import pandas as pd
import pdfplumber
import io
import re
from difflib import SequenceMatcher
from copy import copy
from pathlib import Path
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from openpyxl.styles import PatternFill, Font, Alignment
from highlight_export import build_highlighted_ledger

# =========================================================
# UNIVERSAL PARTY LEDGER READER v3
# Side detection never trusts DR/CR text inside Particulars/Narration.
#
# PAGE CONFIG
# =========================================================
st.set_page_config(
    page_title="Ledger Reconciliation",
    page_icon="📊",
    layout="wide"
)

# =========================================================
# PARTY LEDGER SIDE FIX
# =========================================================
# Party Ledger Dr/Cr must be kept EXACTLY as shown in the source ledger.
# The matching engine compares opposite sides:
#   Our Debit <-> Party Credit
#   Our Credit <-> Party Debit
PARTY_SIDE_INVERTED = False


# =========================================================
# HELPERS
# =========================================================
def format_amount(value):
    try:
        return f"₹{float(value):,.2f}"
    except Exception:
        return "₹0.00"


def balance_text(amount):
    amount = float(amount)
    if amount > 0:
        return f"{format_amount(amount)} Debit"
    if amount < 0:
        return f"{format_amount(abs(amount))} Credit"
    return "₹0.00 Nil"


def calculate_closing(opening, debit, credit):
    return float(opening) + float(debit) - float(credit)


def get_fy(date_value):
    if pd.isna(date_value):
        return None
    y, m = date_value.year, date_value.month
    return f"FY {y}-{str(y + 1)[-2:]}" if m >= 4 else f"FY {y - 1}-{str(y)[-2:]}"


def parse_amount(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return 0.0
    text = text.replace("₹", "").replace(",", "").replace("INR", "").strip()
    try:
        return float(text)
    except Exception:
        return 0.0


def find_column(df, possible_names):
    normalized = {str(c).strip().lower(): c for c in df.columns}
    for name in possible_names:
        if name.lower() in normalized:
            return normalized[name.lower()]
    for col in df.columns:
        col_text = str(col).strip().lower()
        for name in possible_names:
            if name.lower() in col_text:
                return col
    return None


def clean_text(value):
    if value is None or pd.isna(value):
        return ""
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9]+", " ", str(value).upper())).strip()


def text_similarity(a, b):
    a, b = clean_text(a), clean_text(b)
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def normalize_voucher(value):
    return re.sub(r"\s+", "", clean_text(value))


def normalize_party_side(source_side):
    side = str(source_side).strip().upper()
    if side not in ("DR", "CR"):
        return side
    if PARTY_SIDE_INVERTED:
        return "CR" if side == "DR" else "DR"
    return side


def party_side_from_source(source_side):
    value = str(source_side).strip().upper()
    if value not in ("DR", "CR"):
        return value
    if PARTY_SIDE_INVERTED:
        return "CR" if value == "DR" else "DR"
    return value


def side_amounts(side, amount):
    amount = float(amount)
    if side == "DR":
        return amount, 0.0
    if side == "CR":
        return 0.0, amount
    return 0.0, 0.0


def _clean_party_name(value):
    """Clean a party/supplier name for the RECO To:- side."""
    if value is None or pd.isna(value):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


# =========================================================
# OUR LEDGER
# =========================================================
def extract_our_pdf_transactions(file_obj):
    """Parse the standard Our Ledger PDF (Debit/Credit/TDS/Running Balance)."""
    file_obj.seek(0)
    raw_pages = []
    opening_balance = None
    opening_date = pd.NaT
    closing_balance = None
    closing_date = pd.NaT
    rows = []
    row_counter = 0
    date_re = re.compile(r"^(\d{2}-[A-Za-z]{3}-\d{2})\s+(.*)$")
    num_re = re.compile(r"(?:₹\s*)?-?\(?\d[\d,]*(?:\.\d+)?\)?")

    with pdfplumber.open(file_obj) as pdf:
        for page_no, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            raw_pages.append(text)
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            current = None
            pending_before_date = []
            for idx, line in enumerate(lines):
                low_line = line.casefold()

                if low_line.startswith("closing balance"):
                    closing_nums = num_re.findall(line)
                    if closing_nums:
                        parsed_closing = parse_amount(closing_nums[-1])
                        if abs(float(parsed_closing)) > 1e-12:
                            closing_balance = float(parsed_closing)
                            closing_dm = re.search(
                                r"(\d{1,2}-[A-Za-z]{3}-\d{2,4})", line
                            )
                            if closing_dm:
                                closing_date = pd.to_datetime(
                                    closing_dm.group(1), dayfirst=True, errors="coerce"
                                )
                            elif current is not None:
                                closing_date = pd.to_datetime(
                                    current[0], format="%d-%b-%y", errors="coerce"
                                )

                is_footer = (
                    "total amount" in low_line
                    or low_line.startswith("closing balance")
                    or low_line.startswith("accepted by")
                    or low_line.startswith("for, ")
                )
                if is_footer and current is not None:
                    rows.append((page_no, row_counter, current[0], " ".join(current[1:])))
                    row_counter += 1
                    current = None
                    pending_before_date = []
                    continue

                m = date_re.match(line)
                if m:
                    if current is not None:
                        rows.append((page_no, row_counter, current[0], " ".join(current[1:])))
                        row_counter += 1
                    current = [m.group(1)]
                    if pending_before_date and "opening balance" not in m.group(2).casefold():
                        current.extend(pending_before_date[-2:])
                    current.append(m.group(2))
                    pending_before_date = []
                elif current is not None:
                    next_is_date = idx + 1 < len(lines) and date_re.match(lines[idx + 1])
                    if next_is_date and re.search(r"\b[A-Za-z]{1,8}\d[A-Za-z0-9-]*\b", line):
                        pending_before_date.append(line)
                    else:
                        current.append(line)
                else:
                    if re.search(r"\b[A-Za-z]{1,8}\d[A-Za-z0-9-]*\b", line):
                        pending_before_date.append(line)
            if current is not None:
                rows.append((page_no, row_counter, current[0], " ".join(current[1:])))
                row_counter += 1

    out = []
    for page_no, source_row, date_text, rest in rows:
        dt = pd.to_datetime(date_text, format="%d-%b-%y", errors="coerce")
        if pd.isna(dt):
            continue
        rest_for_numbers = re.sub(r"\s+\d{1,3}\s*$", "", rest).strip()

        decimal_money_re = re.compile(
            r"(?:₹\s*)?-?\(?\d[\d,]*\.\d+\)?"
        )
        decimal_nums = decimal_money_re.findall(rest_for_numbers)

        if len(decimal_nums) >= 4:
            nums_clean = [parse_amount(x) for x in decimal_nums]
        else:
            nums = num_re.findall(rest_for_numbers)
            nums_clean = [parse_amount(x) for x in nums]

        low = rest.casefold()

        if "opening balance" in low:
            if nums_clean:
                opening_balance = float(nums_clean[-1])
                opening_date = dt
            continue
        if "total amount" in low or low.startswith("total ") or "closing balance" in low:
            continue

        if len(nums_clean) < 4:
            continue
        debit, credit, tds, running_balance = nums_clean[-4:]
        if abs(debit) < 1e-12 and abs(credit) < 1e-12:
            continue

        ref_match = re.search(r"\b[A-Za-z]{1,6}\d[A-Za-z0-9-]*\b", rest)
        reference = ref_match.group(0) if ref_match else ""
        amount = float(debit) if abs(debit) > 1e-12 else -abs(float(credit))

        out.append({
            "Journal Entry Date": dt,
            "Reference": reference,
            "Particulars": rest,
            "Debit": float(abs(debit)),
            "Credit": float(abs(credit)),
            "TDS": float(tds),
            "Running Balance": float(running_balance),
            "Amount": amount,
            "__source_page": page_no,
            "__source_row": source_row,
            "__opening_balance": float(opening_balance or 0.0),
            "__opening_date": opening_date,
            "__closing_balance": (float(closing_balance) if closing_balance is not None else float("nan")),
            "__closing_date": closing_date,
            "__ledger_source": "PDF",
        })

    if not out:
        raise ValueError("No standard Our Ledger transactions were found in the PDF.")

    result = pd.DataFrame(out)
    result["Withholding Tax"] = pd.to_numeric(result["TDS"], errors="coerce").fillna(0.0)
    return result


def prepare_our_ledger(df):
    working = df.copy()

    if "__ledger_source" in working.columns and working["__ledger_source"].astype(str).eq("PDF").any():
        working["__date"] = pd.to_datetime(working["Journal Entry Date"], errors="coerce", dayfirst=True)
        working["__amount"] = pd.to_numeric(working["Amount"], errors="coerce").fillna(0.0)
        if "Withholding Tax" not in working.columns:
            working["Withholding Tax"] = pd.to_numeric(working.get("TDS", 0), errors="coerce").fillna(0.0)
        working["__entry_type"] = "PDF"
        return working, None

    date_col = find_column(
        working,
        ["Journal Entry Date", "Date", "Posting Date", "Document Date"]
    )
    amount_col = find_column(
        working,
        ["Amount (CoCode Crcy)", "Amount", "Amount (Company Code Currency)", "__amount"]
    )
    entry_type_col = find_column(
        working,
        ["Journal Entry Type", "Entry Type", "Type"]
    )

    if date_col is None:
        return None, "Date column could not be found."
    if amount_col is None:
        return None, "Amount column could not be found."

    working["__date"] = pd.to_datetime(
        working[date_col], errors="coerce", dayfirst=True
    )
    working["__amount"] = working[amount_col].apply(parse_amount)
    working["__entry_type"] = (
        working[entry_type_col].astype(str).str.strip().str.upper()
        if entry_type_col is not None else ""
    )
    return working, None


def find_our_cl_entries(df):
    indexes = []
    for index, row in df.iterrows():
        row_text = " ".join(str(v) for v in row.values).upper()
        if re.search(r"\bCL\b", row_text) or "CLOSING BALANCE" in row_text:
            indexes.append(index)
    return df.loc[indexes].copy()


def _fy_start_year(selected_fy):
    """Return the starting calendar year from labels such as FY 2025-26."""
    try:
        return int(str(selected_fy).replace("FY ", "").split("-")[0])
    except Exception:
        return None


def _find_explicit_cl_before(df, cutoff_date):
    """Find the latest explicit CL entry on or before cutoff_date."""
    if df is None or df.empty:
        return None

    rows = find_our_cl_entries(df).copy()
    if rows.empty:
        return None

    rows = rows.dropna(subset=["__date"])
    rows = rows[rows["__date"] <= cutoff_date].sort_values("__date")
    if rows.empty:
        return None

    return rows.iloc[-1]


def _calculate_our_fy_summary_from_cl(df, selected_fy):
    """Calculate Our Ledger FY balance.

    EXCEL: CL row = anchor only (never a transaction). Selected-FY opening =
      CL anchor + genuine rows physically BEFORE the CL row
      + genuine rows physically AFTER it and dated before 1-Apr of the FY.
    PDF: use explicit Opening / Closing Balance printed in the PDF.
    """
    if df is None or df.empty:
        return None, "No Our Ledger data."

    working = df.copy()
    working["__source_order"] = range(len(working))

    working["__date"] = pd.to_datetime(working["__date"], errors="coerce")
    working["__amount"] = pd.to_numeric(
        working["__amount"], errors="coerce"
    ).fillna(0.0)

    working = working.dropna(subset=["__date"]).copy()

    start_year = _fy_start_year(selected_fy)
    if start_year is None:
        return None, f"Invalid Financial Year: {selected_fy}"

    fy_start = pd.Timestamp(start_year, 4, 1)
    fy_end = pd.Timestamp(start_year + 1, 3, 31)

    is_pdf = (
        "__ledger_source" in working.columns
        and working["__ledger_source"].astype(str).eq("PDF").any()
    )

    # ---------------- OUR LEDGER PDF ----------------
    if is_pdf:
        pdf_working = working.sort_values(
            ["__date", "__source_order"]
        ).copy()

        opening_series = (
            pd.to_numeric(
                pdf_working["__opening_balance"], errors="coerce"
            ).dropna()
            if "__opening_balance" in pdf_working.columns
            else pd.Series(dtype=float)
        )
        opening = (
            float(opening_series.iloc[0])
            if not opening_series.empty
            else 0.0
        )

        fy_mask = (
            pdf_working["__date"]
            .apply(get_fy)
            .astype(str)
            .eq(str(selected_fy))
        )
        tx = pdf_working[fy_mask].copy()

        debit = float(tx["__amount"].clip(lower=0).sum())
        credit = float((-tx["__amount"].clip(upper=0)).sum())
        calculated_closing = calculate_closing(
            opening, debit, credit
        )

        closing_series = (
            pd.to_numeric(
                pdf_working["__closing_balance"], errors="coerce"
            ).dropna()
            if "__closing_balance" in pdf_working.columns
            else pd.Series(dtype=float)
        )
        explicit_closing = (
            float(closing_series.iloc[-1])
            if not closing_series.empty
            else None
        )

        closing = (
            explicit_closing
            if explicit_closing is not None
            else float(calculated_closing)
        )

        closing_date_series = (
            pd.to_datetime(
                pdf_working["__closing_date"], errors="coerce"
            ).dropna()
            if "__closing_date" in pdf_working.columns
            else pd.Series(dtype="datetime64[ns]")
        )

        if not closing_date_series.empty:
            end_date = closing_date_series.iloc[-1]
        elif not tx.empty:
            end_date = tx["__date"].max()
        else:
            end_date = fy_end

        return {
            "financial_year": selected_fy,
            "transactions": len(tx),
            "opening_balance": float(opening),
            "total_debit": debit,
            "total_credit": credit,
            "closing_balance": float(closing),
            "opening_source": "PDF Opening Balance",
            "closing_source": (
                "PDF Closing Balance"
                if explicit_closing is not None
                else "Calculated from PDF transactions"
            ),
            "period_start": fy_start,
            "period_end": pd.Timestamp(end_date),
            "base_cl_date": pd.NaT,
        }, None

    # ---------------- OUR LEDGER EXCEL ----------------
    cl_rows = find_our_cl_entries(working).copy()
    cl_indexes = set(cl_rows.index)
    real_transactions = working[
        ~working.index.isin(cl_indexes)
    ].copy()

    anchor_amount = 0.0
    anchor_date = pd.NaT
    anchor_order = None

    if not cl_rows.empty:
        cl_rows["__date"] = pd.to_datetime(
            cl_rows["__date"], errors="coerce"
        )
        cl_rows["__amount"] = pd.to_numeric(
            cl_rows["__amount"], errors="coerce"
        ).fillna(0.0)

        eligible_cl = cl_rows[
            cl_rows["__date"].notna()
            & (cl_rows["__date"] < fy_start)
        ].sort_values(["__date", "__source_order"])

        if not eligible_cl.empty:
            anchor = eligible_cl.iloc[-1]
            anchor_date = pd.Timestamp(anchor["__date"])
            anchor_amount = float(anchor["__amount"])
            anchor_order = int(anchor["__source_order"])

    # 1) genuine transactions physically BEFORE the CL row
    if anchor_order is not None:
        pre_cl_leftovers = real_transactions[
            real_transactions["__source_order"] < anchor_order
        ].copy()
    else:
        pre_cl_leftovers = real_transactions.iloc[0:0].copy()

    pre_cl_leftovers = pre_cl_leftovers[
        pre_cl_leftovers["__date"] < fy_start
    ].copy()

    pre_cl_debit = float(
        pre_cl_leftovers["__amount"].clip(lower=0).sum()
    )
    pre_cl_credit = float(
        (-pre_cl_leftovers["__amount"].clip(upper=0)).sum()
    )
    pre_cl_net = calculate_closing(
        0.0, pre_cl_debit, pre_cl_credit
    )

    # 2) genuine transactions physically AFTER the CL row, dated before FY start
    if anchor_order is not None:
        post_cl_prior = real_transactions[
            (real_transactions["__source_order"] > anchor_order)
            & (real_transactions["__date"] < fy_start)
        ].copy()
    else:
        post_cl_prior = real_transactions[
            real_transactions["__date"] < fy_start
        ].copy()

    post_cl_debit = float(
        post_cl_prior["__amount"].clip(lower=0).sum()
    )
    post_cl_credit = float(
        (-post_cl_prior["__amount"].clip(upper=0)).sum()
    )
    post_cl_net = calculate_closing(
        0.0, post_cl_debit, post_cl_credit
    )

    opening = (
        float(anchor_amount)
        + float(pre_cl_net)
        + float(post_cl_net)
    )

    tx = real_transactions[
        (real_transactions["__date"] >= fy_start)
        & (real_transactions["__date"] <= fy_end)
    ].copy()

    debit = float(tx["__amount"].clip(lower=0).sum())
    credit = float((-tx["__amount"].clip(upper=0)).sum())

    closing = calculate_closing(
        opening, debit, credit
    )

    actual_end_date = (
        tx["__date"].max()
        if not tx.empty
        else fy_end
    )

    if anchor_order is not None:
        opening_source = (
            f"CL anchor {anchor_date.strftime('%d-%m-%Y')} "
            f"({balance_text(anchor_amount)}) "
            f"+ pre-CL leftover transactions "
            f"+ post-CL transactions before "
            f"{fy_start.strftime('%d-%m-%Y')}"
        )
    else:
        opening_source = (
            "No historical CL anchor found; "
            "calculated from all prior Excel transactions"
        )

    return {
        "financial_year": selected_fy,
        "transactions": len(tx),

        "opening_balance": float(opening),

        "opening_anchor_balance": float(anchor_amount),
        "opening_pre_cl_debit": pre_cl_debit,
        "opening_pre_cl_credit": pre_cl_credit,
        "opening_pre_cl_net": float(pre_cl_net),
        "opening_post_cl_debit": post_cl_debit,
        "opening_post_cl_credit": post_cl_credit,
        "opening_post_cl_net": float(post_cl_net),

        "opening_debit_total": (
            pre_cl_debit + post_cl_debit
        ),
        "opening_credit_total": (
            pre_cl_credit + post_cl_credit
        ),

        "total_debit": debit,
        "total_credit": credit,
        "closing_balance": float(closing),

        "opening_source": opening_source,
        "closing_source": (
            "Opening + selected-FY Debit - selected-FY Credit"
        ),

        "period_start": fy_start,
        "period_end": pd.Timestamp(actual_end_date),
        "base_cl_date": anchor_date,
    }, None


def extract_our_closing_metadata(prepared_our, selected_fy=None):
    """Return the closing date/balance used by RECO (calculated FY closing)."""
    if prepared_our is None or prepared_our.empty or selected_fy is None:
        return pd.NaT, None

    summary, error = _calculate_our_fy_summary_from_cl(
        prepared_our, selected_fy
    )

    if summary is None:
        return pd.NaT, None

    return pd.to_datetime(summary["period_end"], errors="coerce"), float(
        summary["closing_balance"]
    )


def calculate_our_summary(df, selected_fy=None):
    """Our Ledger Summary entry point."""
    working, error = prepare_our_ledger(df)
    if error:
        return None, error

    working = working.dropna(subset=["__date"]).sort_values("__date").copy()
    if working.empty:
        return None, "No valid dates found in Our Ledger."

    if selected_fy is None:
        working["__fy"] = working["__date"].apply(get_fy)
        available = working["__fy"].dropna().unique().tolist()
        if not available:
            return None, "Could not determine financial year."
        selected_fy = sorted(available)[-1]

    return _calculate_our_fy_summary_from_cl(working, selected_fy)


# =========================================================
# UNIVERSAL PARTY LEDGER NORMALIZATION
# =========================================================
# Internal transaction schema:
# Date | Type | Particulars | Vch Type | Vch No. | Debit | Credit |
# Amount | Net Amount | FY | Plant/Section | Source Page | Source Row
# Type is the ACCOUNTING side: DR = Party Debit, CR = Party Credit.

PARTY_DATE_ALIASES = [
    "Date", "Posting Date", "Document Date", "Journal Entry Date",
    "Transaction Date", "Entry Date", "Voucher Date", "Doc Date"
]
PARTY_DEBIT_ALIASES = [
    "Debit", "Dr", "Debit Amount", "Dr Amount", "Debit Amt", "Dr Amt",
    "Withdrawal", "Payments", "Payment"
]
PARTY_CREDIT_ALIASES = [
    "Credit", "Cr", "Credit Amount", "Cr Amount", "Credit Amt", "Cr Amt",
    "Deposit", "Receipts", "Receipt"
]
# Balance is deliberately NOT in this list.
PARTY_AMOUNT_ALIASES = [
    "Amount", "Transaction Amount", "Txn Amount", "Value", "Net Amount",
    "Amount (INR)", "Amount (₹)", "Transaction Value", "Txn Value",
    "Total Amount", "Invoice Amount", "Bill Amount", "Gross Amount"
]
PARTY_SIDE_ALIASES = [
    "Dr/Cr", "DR/CR", "Debit/Credit", "Debit Credit", "Type", "Side",
    "Nature", "Indicator", "DC", "D/C", "Cr/Dr"
]
PARTY_DESCRIPTION_ALIASES = [
    "Particulars", "Description", "Narration", "Details", "Remarks",
    "Reference", "Memo", "Transaction Details", "Description/Narration",
    "Particular", "Narration/Description"
]
PARTY_VOUCHER_ALIASES = [
    "Vch No.", "Vch No", "Voucher No.", "Voucher No", "Voucher Number",
    "Document No.", "Document No", "Doc No", "Reference No", "Ref No",
    "Reference", "Ref", "Transaction ID", "Txn ID"
]
PARTY_VOUCHER_TYPE_ALIASES = [
    "Vch Type", "Voucher Type", "Transaction Type", "Txn Type", "Entry Type", "Document Type"
]

PARTY_WHT_ALIASES = [
    "Withholding Tax", "Withholding Tax Amount", "WHT",
    "TDS", "TDS Amount", "Withholding Tax/TDS", "Tax Withheld"
]

PARTY_SEMANTIC_SIDE_MAP = {
    "SALE": "DR", "SALES": "DR", "INVOICE": "DR", "BILL": "DR",
    "PURCHASE RETURN": "DR", "DEBIT NOTE": "DR",
    "PAYMENT IN": "CR", "PAYMENT-IN": "CR", "RECEIPT": "CR",
    "PAYMENT": "CR", "COLLECTION": "CR", "CREDIT NOTE": "CR"
}


def _semantic_side_from_transaction_type(value):
    text = re.sub(r"[^A-Z0-9]+", " ", str(value or "").upper()).strip()
    if not text:
        return None
    if text in PARTY_SEMANTIC_SIDE_MAP:
        return PARTY_SEMANTIC_SIDE_MAP[text]
    for key, side in PARTY_SEMANTIC_SIDE_MAP.items():
        if key in text:
            return side
    return None


_PARTY_DATE_RE = re.compile(
    r"^(\d{1,2}[-/]\d{1,2}[-/]\d{2,4}|\d{1,2}[-/][A-Za-z]{3}[-/]\d{2,4})$"
)
_PARTY_DATE_PREFIX_RE = re.compile(
    r"^(\d{1,2}[-/]\d{1,2}[-/]\d{2,4}|\d{1,2}[-/][A-Za-z]{3}[-/]\d{2,4})\b\s*(.*)$"
)
_PARTY_MONEY_RE = re.compile(r"(?<![A-Za-z])[-+]?\d[\d,]*(?:\.\d{1,2})?")
_PARTY_VOUCHER_RE = re.compile(r"\b[A-Z]{1,8}[A-Z0-9./_-]*\d[A-Z0-9./_-]*\b")
_PARTY_TALLY_SIDE_RE = re.compile(r"^(DR|CR|TO|BY)\b\s*(.*)$", re.I)
_PARTY_VTYPE_WORDS = (
    "Receipt", "Sales", "Journal", "Payment", "Purchase", "Contra",
    "Credit Note", "Debit Note", "Invoice", "Adjustment"
)


def _norm_header(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip()


def _header_score(values):
    texts = [_norm_header(v) for v in values if str(v).strip()]
    joined = " | ".join(texts)
    score = 0
    groups = [
        PARTY_DATE_ALIASES, PARTY_DEBIT_ALIASES, PARTY_CREDIT_ALIASES,
        PARTY_AMOUNT_ALIASES, PARTY_DESCRIPTION_ALIASES, PARTY_VOUCHER_ALIASES,
        PARTY_SIDE_ALIASES,
    ]
    for group in groups:
        aliases = [_norm_header(x) for x in group]
        if any(a and (a in texts or any(a in t for t in texts)) for a in aliases):
            score += 2
    if "date" in joined:
        score += 1
    if "debit" in joined or "credit" in joined or "amount" in joined:
        score += 2
    return score


def _detect_header_row(raw_df, max_scan=40):
    if raw_df is None or raw_df.empty:
        return None, 0
    best_row, best_score = 0, -1
    for i in range(min(len(raw_df), max_scan)):
        score = _header_score(raw_df.iloc[i].tolist())
        if score > best_score:
            best_row, best_score = i, score
    return (best_row, best_score) if best_score >= 3 else (None, best_score)


def _find_alias_column(df, aliases, exclude=None):
    """FIXED: short aliases (dr / cr / dc / type) match EXACT column names only,
    longer aliases match whole words only. Stops 'Cr' matching inside 'Crcy'."""
    exclude = set(exclude or [])
    cols = [c for c in df.columns if c not in exclude]
    normalized = {_norm_header(c): c for c in cols}
    alias_norm = [_norm_header(a) for a in aliases if _norm_header(a)]
    for a in alias_norm:                      # 1) exact match
        if a in normalized:
            return normalized[a]
    for col in cols:                          # 2) whole-word match only
        c = _norm_header(col)
        for a in alias_norm:
            if len(a) <= 3 or a == "type":
                continue
            if re.search(rf"(?<![a-z0-9]){re.escape(a)}(?![a-z0-9])", c):
                return col
            if len(c) > 3 and re.search(rf"(?<![a-z0-9]){re.escape(c)}(?![a-z0-9])", a):
                return col
    return None


def _parse_signed_amount(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return 0.0, None
    if isinstance(value, (int, float)) and not pd.isna(value):
        return float(value), None
    text = str(value).strip()
    if not text:
        return 0.0, None
    upper = text.upper().replace("₹", "").replace("INR", " ")
    side = None
    if re.search(r"\bDR\b|\bDEBIT\b", upper):
        side = "DR"
    elif re.search(r"\bCR\b|\bCREDIT\b", upper):
        side = "CR"
    negative = bool(re.search(r"^\s*\(.*\)\s*$", upper)) or bool(re.search(r"-\s*\d", upper))
    nums = re.findall(r"[-+]?\d[\d,]*(?:\.\d+)?", upper)
    if not nums:
        return 0.0, side
    try:
        amount = abs(float(nums[0].replace(",", "")))
    except Exception:
        return 0.0, side
    return (-amount if negative else amount), side


def _value_text(value):
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def _detect_side_value(value):
    text = _value_text(value).upper()
    if re.fullmatch(r"(?:DR|DEBIT|D)", text):
        return "DR"
    if re.fullmatch(r"(?:CR|CREDIT|C)", text):
        return "CR"
    return None


def _tally_side(token):
    token = str(token or "").strip().upper()
    if token in ("DR", "TO"):
        return "DR"
    if token in ("CR", "BY"):
        return "CR"
    return None


def _is_summary_or_noise(text):
    """True only for ledger-generated rows, not genuine Dr/Cr transactions."""
    u = re.sub(r"\s+", " ", str(text or "")).strip().upper()
    if not u:
        return True

    summary_patterns = [
        r"^PAGE\s+\d+", r"^CONTINUED", r"^DATE\b", r"^PARTICULARS\b",
        r"\bBROUGHT FORWARD\b", r"\bCARRIED OVER\b", r"\bCARRIED FORWARD\b", r"\bGRAND TOTAL\b",
        r"^SUB[ -]?TOTAL\b", r"^TOTAL\b", r"\bTOTAL\s+\d",
        r"^LEDGER ACCOUNT\b", r"^ACCOUNT\s*:", r"^FROM\s+\d", r"^TO\s+\d",
    ]
    if any(re.search(p, u) for p in summary_patterns):
        return True

    balance_patterns = [
        r"\bOPENING\s+BALANCE\b", r"\bCLOSING\s+BALANCE\b",
        r"\bDEBIT\s+BALANCE\b", r"\bCREDIT\s+BALANCE\b",
        r"^BALANCE\b", r"\bBALANCING(?:\s+ENTRY|\s+FIGURE|\s+AMOUNT)?\b",
        r"\bCLOSING\s+ENTRY\b", r"\bCLOSING\s+FIGURE\b",
    ]
    return any(re.search(p, u) for p in balance_patterns)


def _looks_like_balance_line(text):
    u = re.sub(r"\s+", " ", str(text)).strip().upper()
    return bool(re.search(r"\bOPENING\s+BALANCE\b|\bCLOSING\s+BALANCE\b|\bDEBIT\s+BALANCE\b|\bCREDIT\s+BALANCE\b", u))


def _is_numeric_only_ledger_footer(text):
    """Detect generated total/footer rows containing only money values."""
    raw = re.sub(r"\s+", " ", str(text or "")).strip()
    if not raw:
        return True
    raw = re.sub(r"[₹$€£]", "", raw)
    tokens = raw.split()
    if not tokens:
        return True
    numeric = 0
    for token in tokens:
        token = token.strip("|,:;()[]{}")
        if not token:
            continue
        try:
            _parse_signed_amount(token)
            if re.search(r"\d", token):
                numeric += 1
                continue
        except Exception:
            pass
        return False
    return numeric > 0


def _classify_universal_ledger_row(raw):
    """Return TRANSACTION, OPENING_BALANCE, CLOSING_BALANCE or SUMMARY_TOTAL."""
    u = re.sub(r"\s+", " ", str(raw or "")).strip().upper()
    if re.search(r"\bOPENING\s+BALANCE\b", u):
        return "OPENING_BALANCE"
    if re.search(r"\b(?:CLOSING|DEBIT|CREDIT)\s+BALANCE\b|\bBALANCING(?:\s+ENTRY|\s+FIGURE|\s+AMOUNT)?\b|\bCLOSING\s+(?:ENTRY|FIGURE)\b", u):
        return "CLOSING_BALANCE"
    if _is_numeric_only_ledger_footer(raw):
        return "SUMMARY_TOTAL"
    if _is_summary_or_noise(raw):
        return "SUMMARY_TOTAL"
    return "TRANSACTION"


def _make_party_record(
    date_value, side, amount, description="", voucher_no="", voucher_type="",
    plant_section="", source_page=None, source_row=None, source_type=""
):
    if side not in ("DR", "CR") or amount <= 0:
        return None
    amount = abs(float(amount))
    debit = amount if side == "DR" else 0.0
    credit = amount if side == "CR" else 0.0
    date_value = pd.to_datetime(date_value, errors="coerce")
    return {
        "Date": date_value,
        "Type": side,
        "Particulars": str(description or "").strip(),
        "Vch Type": str(voucher_type or "").strip(),
        "Vch No.": str(voucher_no or "").strip(),
        "Debit": debit,
        "Credit": credit,
        "Amount": amount,
        "Net Amount": debit - credit,
        "FY": get_fy(date_value),
        "Plant/Section": str(plant_section or "").strip(),
        "Source Page": source_page,
        "Source Row": source_row,
        "Source Type": source_type,
    }


def _clean_transaction_text(text):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    text = re.sub(r"\b(?:DR|CR|DEBIT|CREDIT|TO|BY)\b", " ", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip(" -|:")


def _extract_voucher_and_type(text):
    working = re.sub(r"\s+", " ", str(text or "")).strip()
    voucher = ""
    vm = _PARTY_VOUCHER_RE.findall(working)
    if vm:
        candidates = [x for x in vm if any(ch.isdigit() for ch in x)]
        if candidates:
            voucher = max(candidates, key=len)
            working = re.sub(re.escape(voucher), " ", working, count=1)
    vtype = ""
    for candidate in _PARTY_VTYPE_WORDS:
        if re.search(rf"\b{re.escape(candidate)}\b", working, re.I):
            vtype = candidate
            working = re.sub(rf"\b{re.escape(candidate)}\b", " ", working, count=1, flags=re.I)
            break
    return re.sub(r"\s+", " ", working).strip(), voucher, vtype


def _balance_side_from_line(text, numeric_columns=None):
    u = str(text).upper()
    if re.search(r"\bDR\b|\bDEBIT\s+BALANCE\b", u):
        return "DR"
    if re.search(r"\bCR\b|\bCREDIT\s+BALANCE\b", u):
        return "CR"
    if numeric_columns:
        d, c = numeric_columns
        if d > 0 and c <= 0:
            return "DR"
        if c > 0 and d <= 0:
            return "CR"
    return None


def _consolidate_balance_records(records):
    debit_total = sum(abs(float(x.get("amount", 0) or 0)) for x in records if x.get("side") == "DR")
    credit_total = sum(abs(float(x.get("amount", 0) or 0)) for x in records if x.get("side") == "CR")
    net = debit_total - credit_total
    if net > 0:
        return net, "DR", debit_total, credit_total
    if net < 0:
        return abs(net), "CR", debit_total, credit_total
    return 0.0, "", debit_total, credit_total


def _latest_balance_records(records):
    """Keep only the LATEST closing balance of every plant/section.

    A multi-year ledger prints a closing balance at every year end (and the next
    year opens with the same figure). Adding them all double counts
    (e.g. 12,055 + 26,037 = 38,092), so only the last one counts.
    """
    by_section = {}
    for rec in records or []:
        by_section.setdefault(rec.get("plant_section") or "Unidentified Section", []).append(rec)
    out = []
    for recs in by_section.values():
        dated = [(pd.to_datetime(r.get("date"), errors="coerce"), r) for r in recs]
        valid = [d for d, _ in dated if not pd.isna(d)]
        if not valid:
            out.append(recs[-1])  # undated: the last one printed in the ledger
            continue
        latest = max(valid)
        out.extend(r for d, r in dated if not pd.isna(d) and d == latest)
    return out


def _finalize_party_metadata(
    opening_records, closing_records, transactions, diagnostics=None, sections=None
):
    latest_closing_records = _latest_balance_records(closing_records)
    opening_amount, opening_side, opening_dr, opening_cr = _consolidate_balance_records(opening_records)
    closing_amount, closing_side, closing_dr, closing_cr = _consolidate_balance_records(latest_closing_records)

    def section_map(records):
        out = {}
        for rec in records:
            key = rec.get("plant_section") or "Unidentified Section"
            out.setdefault(key, []).append(rec)
        return out

    section_names = []
    for sec in sections or []:
        name = sec.get("plant_section") if isinstance(sec, dict) else str(sec)
        if name and name not in section_names:
            section_names.append(name)
    section_names.extend([k for k in section_map(opening_records) if k not in section_names])
    section_names.extend([k for k in section_map(closing_records) if k not in section_names])

    opening_by_section = {}
    for key in section_names:
        recs = section_map(opening_records).get(key, [])
        amt, side, dr, cr = _consolidate_balance_records(recs)
        opening_by_section[key] = {
            "amount": amt, "side": side or "", "debit_total": dr, "credit_total": cr,
            "records": recs, "displayed": bool(recs),
        }
    closing_by_section = {}
    for key in section_names:
        recs = section_map(latest_closing_records).get(key, [])
        amt, side, dr, cr = _consolidate_balance_records(recs)
        closing_by_section[key] = {
            "amount": amt, "side": side or "", "debit_total": dr, "credit_total": cr,
            "records": recs, "displayed": bool(recs),
        }

    dates = pd.to_datetime([x.get("Date") for x in transactions], errors="coerce").dropna()
    last_date = dates.max() if not dates.empty else pd.NaT
    all_balance_dates = pd.to_datetime(
        [x.get("date") for x in opening_records + closing_records], errors="coerce"
    ).dropna()

    metadata = {
        "opening_balance": opening_amount if opening_side == "DR" else (-opening_amount if opening_side == "CR" else 0.0),
        "opening_side": opening_side,
        "opening_amount": opening_amount,
        "opening_debit_total": opening_dr,
        "opening_credit_total": opening_cr,
        "opening_date": min(all_balance_dates) if not all_balance_dates.empty else pd.NaT,
        "closing_balance": closing_amount if closing_side == "DR" else (-closing_amount if closing_side == "CR" else 0.0),
        "closing_side": closing_side,
        "closing_amount": closing_amount,
        "closing_debit_total": closing_dr,
        "closing_credit_total": closing_cr,
        "closing_date": max(all_balance_dates) if not all_balance_dates.empty else last_date,
        "last_transaction_date": last_date,
        "opening_records": opening_records,
        "closing_records": closing_records,
        "opening_by_section": opening_by_section,
        "closing_by_section": closing_by_section,
        "sections_detail": sections or [],
        "diagnostics": diagnostics or [],
        "unparsed_lines": sum(int(x.get("unparsed", 0) or 0) for x in (diagnostics or [])),
        "candidate_lines": sum(int(x.get("candidate_lines", x.get("rows_seen", 0)) or 0) for x in (diagnostics or [])),
        "sections": len(sections or []),
        "manual_review": False,
    }
    return metadata


def _section_label_from_page(lines):
    """Infer the account/plant section without vendor-specific names."""
    clean = [re.sub(r"\s+", " ", str(x)).strip() for x in lines if str(x).strip()]
    if not clean:
        return "Unidentified Section"
    ledger_idx = next((i for i, x in enumerate(clean) if re.search(r"\bLEDGER\s+ACCOUNT\b|^LEDGER$", x, re.I)), None)
    if ledger_idx is not None:
        before = []
        for i in range(max(0, ledger_idx - 6), ledger_idx):
            x = clean[i]
            if _is_summary_or_noise(x):
                continue
            if re.search(r"\b(?:DATE|PAGE|FROM|TO|EMAIL|E-MAIL|GSTIN|STATE|ROAD|PLOT|SY\.NO|UNIT|WARD)\b|^E[- ]?MAIL\s*:", x, re.I):
                continue
            if len(x) >= 4:
                before.append(x)
        if before:
            return max(before, key=lambda x: ("(" in x, len(x)))
        for i in range(ledger_idx + 1, min(len(clean), ledger_idx + 6)):
            x = clean[i]
            if re.match(r"^ACCOUNT\s*:", x, re.I):
                return x.split(':', 1)[1].strip() or x
            if _is_summary_or_noise(x) or re.search(r"\b(?:DATE|FROM|TO|PAGE|DEBIT|CREDIT|PARTICULARS|VCH|BALANCE)\b", x, re.I):
                continue
            if len(x) >= 4:
                return x
    for x in clean[:12]:
        if _is_summary_or_noise(x):
            continue
        if re.search(r"\b(?:DATE|PARTICULARS|DEBIT|CREDIT|BALANCE|PAGE|FROM|TO)\b", x, re.I):
            continue
        if len(x) >= 4:
            return x
    return "Unidentified Section"


def _detect_pdf_page_structure(lines):
    joined = " ".join(re.sub(r"\s+", " ", x).strip() for x in lines).upper()
    header_text = ""
    for x in lines:
        u = re.sub(r"\s+", " ", x).strip().upper()
        if "DATE" in u and ("PARTICULARS" in u or "DEBIT" in u or "CREDIT" in u or "TYPE" in u):
            header_text = u
            break
    if "BALANCE" in header_text and "DEBIT" in header_text and "CREDIT" in header_text:
        return "TYPE2_STRUCTURED"
    tally_count = sum(1 for x in lines if re.match(r"^\s*(?:\d{1,2}[-/]\w+[-/]\d{2,4}\s+)?(?:DR|CR|TO|BY)\b", x, re.I))
    if tally_count >= 2 and ("VCH TYPE" in joined or "VCH NO" in joined or "PARTICULARS" in joined):
        if sum(1 for x in lines if re.search(r"\b\bTO\b|\bBY\b", x, re.I)) >= 2:
            return "TYPE1_TALLY_TO_BY"
        return "TYPE0_TALLY_DR_CR"
    if "DEBIT" in header_text and "CREDIT" in header_text:
        return "TYPE2_STRUCTURED"
    return "GENERIC"


def _group_pdf_words_by_line(words, tolerance=2.5):
    lines = []
    for word in sorted(words, key=lambda w: (w.get("top", 0), w.get("x0", 0))):
        placed = False
        for line in lines:
            if abs(float(line[0].get("top", 0)) - float(word.get("top", 0))) <= tolerance:
                line.append(word)
                placed = True
                break
        if not placed:
            lines.append([word])
    for line in lines:
        line.sort(key=lambda w: w.get("x0", 0))
    return lines


def _dedupe_party_transactions(transactions):
    """Remove only parser duplicates sharing a source signature."""
    seen = set()
    out = []
    for item in transactions:
        page = item.get("Source Page")
        row = item.get("Source Row")
        section = item.get("Plant/Section", "")
        if page is not None and row is not None:
            key = (str(page), str(row), section, item.get("Source Type", ""))
            if key in seen:
                continue
            seen.add(key)
        out.append(item)
    return out


def _extract_pdf_section_and_balances(lines, page_number):
    section = _section_label_from_page(lines)
    for line in lines:
        x = re.sub(r"\s+", " ", line).strip()
        m = re.search(r"DURGA\s+INFRA[^()]*\([^)]*\)", x, re.I)
        if m:
            # Only "Name (Plant)", not the "Ledger Account : dates Page 2" tail,
            # otherwise every page looks like a different plant.
            section = m.group(0).strip()
            break
    return section


def _infer_pdf_semantics(page, grouped_lines):
    """Infer PDF ledger semantics from geometry and repeated row evidence."""
    p = {
        "header_index": None, "date_x": None, "type_x": None,
        "debit_x": None, "credit_x": None, "balance_x": None,
        "amount_x": None, "particulars_x": None, "vch_type_x": None,
        "vch_no_x": None, "narration_x": None,
        "has_date": False, "has_debit": False, "has_credit": False,
        "has_balance": False, "has_amount": False, "has_type": False,
        "has_side_marker": False, "marker_tokens": set(),
        "marker_map": {}, "confidence": 0.0,
    }

    def cx(w):
        return (float(w.get("x0", 0)) + float(w.get("x1", 0))) / 2.0

    best = None
    for i, lw in enumerate(grouped_lines):
        texts = [str(w.get("text", "")).strip() for w in lw if str(w.get("text", "")).strip()]
        u = " ".join(texts).upper()
        if not texts or "DATE" not in u:
            continue
        semantic = any(k in u for k in ("DEBIT", "CREDIT", "BALANCE", "AMOUNT",
                                         "PARTICULAR", "NARRATION", "VCH", "TYPE",
                                         "DR/CR", "DR CR", "CR DR"))
        if not semantic:
            continue
        score = 2
        score += 2 if "DEBIT" in u and "CREDIT" in u else 0
        score += 1 if "BALANCE" in u else 0
        score += 1 if "AMOUNT" in u else 0
        score += 1 if "PARTICULAR" in u or "NARRATION" in u else 0
        score += 1 if "VCH" in u else 0
        score += 1 if re.search(r"\bTYPE\b", u) else 0
        if best is None or score > best[0]:
            best = (score, i, lw, u)
    if best is None:
        for i, lw in enumerate(grouped_lines):
            u = " ".join(str(w.get("text", "")) for w in lw).upper()
            if "DATE" in u:
                best = (1, i, lw, u)
                break
    if best is None:
        return p

    score, h, header_line, _ = best
    p["header_index"] = h
    for w in header_line:
        t = str(w.get("text", "")).strip().upper()
        x = cx(w)
        if t in ("DATE", "POSTING", "DOCUMENT") or ("DATE" in t and len(t) < 25):
            p["date_x"] = p["date_x"] or x
        elif t == "DEBIT":
            p["debit_x"] = x
        elif t == "CREDIT":
            p["credit_x"] = x
        elif t == "BALANCE":
            p["balance_x"] = x
        elif t in ("AMOUNT", "VALUE", "AMT"):
            p["amount_x"] = x
        elif t == "TYPE":
            p["type_x"] = x
        elif t == "PARTICULARS" or t == "PARTICULAR":
            p["particulars_x"] = x
        elif t == "NARRATION":
            p["narration_x"] = x
        elif t == "VCH":
            p["vch_type_x"] = x
        elif t in ("NO", "NO.", "NUMBER"):
            p["vch_no_x"] = x

    p["has_date"] = p["date_x"] is not None
    p["has_debit"] = p["debit_x"] is not None
    p["has_credit"] = p["credit_x"] is not None
    p["has_balance"] = p["balance_x"] is not None
    p["has_amount"] = p["amount_x"] is not None
    standalone_type = any(str(w.get("text", "")).strip().upper() == "TYPE" for w in header_line)
    vch_type_present = any(str(w.get("text", "")).strip().upper() == "VCH" for w in header_line)
    p["has_type"] = p["type_x"] is not None and standalone_type and not (vch_type_present and p.get("vch_type_x") is not None)
    if not p["has_type"]:
        p["type_x"] = None

    marker_counts = {k: 0 for k in ("DR", "CR", "TO", "BY")}
    for lw in grouped_lines[h + 1:]:
        for w in lw:
            t = str(w.get("text", "")).strip().upper()
            if t in marker_counts:
                marker_counts[t] += 1
    p["marker_tokens"] = {k for k, v in marker_counts.items() if v}
    p["has_side_marker"] = bool(p["marker_tokens"])

    if p["has_type"]:
        p["marker_map"] = {"DR": "DR", "CR": "CR"}
        p["confidence"] += 0.35
    elif p["has_debit"] and p["has_credit"]:
        p["confidence"] += 0.40
    elif p["has_side_marker"]:
        p["confidence"] += 0.20
    if p["has_date"]:
        p["confidence"] += 0.15
    if p["has_balance"]:
        p["confidence"] += 0.15
    if p["has_amount"]:
        p["confidence"] += 0.10
    p["confidence"] = min(1.0, p["confidence"])
    return p


def _numeric_tokens_near_columns(line_words, profile, exclude_indexes=None):
    """Return numeric candidates with geometry-based semantic column labels."""
    exclude = set(exclude_indexes or [])
    out = []

    def cx(w):
        return (float(w.get("x0", 0)) + float(w.get("x1", 0))) / 2.0

    semantic_columns = []
    if profile.get("debit_x") is not None:
        semantic_columns.append((profile["debit_x"], "DR"))
    if profile.get("credit_x") is not None:
        semantic_columns.append((profile["credit_x"], "CR"))
    if profile.get("amount_x") is not None:
        semantic_columns.append((profile["amount_x"], "AMOUNT"))
    if profile.get("balance_x") is not None:
        semantic_columns.append((profile["balance_x"], "BAL"))
    for j, w in enumerate(line_words):
        if j in exclude:
            continue
        t = str(w.get("text", "")).strip()
        if not t or _PARTY_DATE_RE.match(t):
            continue
        val, embedded = _parse_signed_amount(t)
        if abs(val) <= 0:
            continue
        x = cx(w)
        if semantic_columns:
            dist, col = min((abs(x - q), name) for q, name in semantic_columns)
        else:
            dist, col = 9999, None
        out.append({"index": j, "amount": abs(val), "embedded_side": embedded,
                    "x": x, "column": col, "distance": dist})
    return out


def _transaction_amount_column_limit(profile):
    """Maximum horizontal distance allowed for a PDF transaction amount token."""
    xs = [
        float(profile[k]) for k in ("debit_x", "credit_x", "amount_x")
        if profile.get(k) is not None
    ]
    if len(xs) >= 2:
        xs = sorted(xs)
        gaps = [b - a for a, b in zip(xs, xs[1:]) if b > a]
        if gaps:
            return max(24.0, min(45.0, min(gaps) * 0.45))
    return 32.0


def _strict_transaction_candidates(candidates, profile):
    """Keep only numeric tokens genuinely located in transaction amount columns."""
    limit = _transaction_amount_column_limit(profile)
    return [
        c for c in candidates
        if c.get("column") in ("DR", "CR", "AMOUNT")
        and float(c.get("distance", 9999)) <= limit
        and float(c.get("amount", 0) or 0) > 0
    ]


def _marker_column_x(grouped, start_index, token_set=("DR", "CR", "TO", "BY")):
    """Infer a marker column only from repeated standalone side tokens."""
    xs = []
    for lw in grouped[start_index + 1:]:
        for w in lw:
            t = str(w.get("text", "")).strip().upper()
            if t in token_set:
                x = (float(w.get("x0", 0)) + float(w.get("x1", 0))) / 2.0
                xs.append(x)
    if len(xs) < 2:
        return None, 0
    xs_sorted = sorted(xs)
    import statistics
    med = statistics.median(xs_sorted)
    aligned = sum(1 for x in xs if abs(x - med) <= 18)
    return med, aligned


def _side_token_is_in_structural_band(line_words, marker_idx, profile, marker_x=None):
    """Reject DR/CR tokens that are merely part of Particulars/Narration."""
    if marker_idx is None:
        return False
    if profile.get("has_type") and profile.get("type_x") is not None:
        w = line_words[marker_idx]
        x = (float(w.get("x0", 0)) + float(w.get("x1", 0))) / 2.0
        return abs(x - profile["type_x"]) <= 24
    if marker_x is not None:
        w = line_words[marker_idx]
        x = (float(w.get("x0", 0)) + float(w.get("x1", 0))) / 2.0
        return abs(x - marker_x) <= 24
    if profile.get("particulars_x") is not None:
        w = line_words[marker_idx]
        x = (float(w.get("x0", 0)) + float(w.get("x1", 0))) / 2.0
        return x < profile["particulars_x"] - 8
    return False


def _infer_marker_mapping(grouped, profile, header_index):
    """Infer accounting-side semantics without trusting DR/CR in narration."""
    if profile.get("has_debit") and profile.get("has_credit"):
        return {}, 0.98, "physical Debit/Credit transaction columns"

    if profile.get("has_type"):
        return {"DR": "DR", "CR": "CR"}, 0.95, "dedicated transaction Type column"

    marker_x, aligned = _marker_column_x(grouped, header_index)
    tokens = profile.get("marker_tokens", set())
    if marker_x is not None and aligned >= 2:
        if "TO" in tokens or "BY" in tokens:
            return {"TO": "DR", "BY": "CR"}, 0.88, "repeated structural To/By column"
        if "DR" in tokens or "CR" in tokens:
            return {"DR": "DR", "CR": "CR"}, 0.88, "repeated structural Dr/Cr column"

    return {}, 0.0, "no reliable structural transaction-side evidence"


def _infer_row_transaction_semantics(line_words, profile, current_date, marker_mapping, marker_x=None):
    texts = [str(w.get("text", "")).strip() for w in line_words]
    date_idx = None
    date_value = current_date
    for j, t in enumerate(texts):
        if _PARTY_DATE_RE.match(t):
            d = pd.to_datetime(t, dayfirst=True, errors="coerce")
            if not pd.isna(d):
                date_idx = j
                date_value = d
            break
    if pd.isna(date_value):
        return None, date_value

    marker_idx = None
    marker = None
    for j, t in enumerate(texts):
        if date_idx is not None and j <= date_idx:
            continue
        if t.upper() not in ("DR", "CR", "TO", "BY"):
            continue
        if _side_token_is_in_structural_band(line_words, j, profile, marker_x):
            marker_idx = j
            marker = t.upper()
            break

    candidates = _numeric_tokens_near_columns(line_words, profile, {date_idx, marker_idx})
    if not candidates:
        return None, date_value

    strict_candidates = _strict_transaction_candidates(candidates, profile)
    tx = [c for c in strict_candidates if c["column"] in ("DR", "CR")]
    amount_candidates = [c for c in strict_candidates if c["column"] == "AMOUNT"]
    if tx:
        chosen = min(tx, key=lambda c: (c["distance"], -c["amount"]))
        source_col = chosen["column"]
    elif amount_candidates:
        chosen = min(amount_candidates, key=lambda c: c["distance"])
        source_col = "AMOUNT"
    else:
        if marker_idx is None:
            return None, date_value
        after = [c for c in candidates if c["index"] > marker_idx]
        if not after:
            return None, date_value
        chosen = max(after, key=lambda c: (c["index"], c["amount"]))
        source_col = None

    side = None
    if profile.get("has_type") and profile.get("type_x") is not None:
        for j, t in enumerate(texts):
            if t.upper() not in ("DR", "CR"):
                continue
            if _side_token_is_in_structural_band(line_words, j, profile, marker_x):
                side = t.upper()
                break
    if side is None and source_col in ("DR", "CR"):
        side = source_col
    if side is None and marker is not None:
        side = marker_mapping.get(marker)

    if side not in ("DR", "CR") or chosen["amount"] <= 0:
        return None, date_value

    excluded = {date_idx, marker_idx, chosen["index"]}
    body = []
    for j, t in enumerate(texts):
        if j in excluded or not t:
            continue
        if _PARTY_DATE_RE.match(t):
            continue
        if profile.get("has_type") and t.upper() in ("DR", "CR") and _side_token_is_in_structural_band(line_words, j, profile, marker_x):
            continue
        body.append(t)
    raw_body = " ".join(body)
    desc, voucher, vtype = _extract_voucher_and_type(raw_body)
    desc = re.sub(r"\s+", " ", str(desc or "")).strip(" -|:")
    return {
        "date": date_value, "side": side, "amount": chosen["amount"],
        "marker": marker, "description": desc, "voucher": voucher, "vtype": vtype,
        "source_amount_column": source_col, "source_amount_distance": chosen["distance"],
    }, date_value


def _parse_pdf_page_universal(page, lines, plant_section, page_number, source_offset=0):
    """True universal page reader (geometry + repeated-row evidence)."""
    try:
        words = page.extract_words(use_text_flow=True)
    except Exception:
        words = []
    if not words:
        return [], [], 0, 0
    grouped = _group_pdf_words_by_line(words)
    profile = _infer_pdf_semantics(page, grouped)
    h = profile.get("header_index")
    if h is None:
        h = -1
        profile["header_index"] = -1
        profile["has_date"] = True
        profile["marker_tokens"] = {"DR", "CR", "TO", "BY"}
        profile["has_side_marker"] = True
        profile["confidence"] = 0.35
    marker_mapping, map_conf, map_reason = _infer_marker_mapping(grouped, profile, h)
    marker_x, marker_aligned = _marker_column_x(grouped, h)
    profile["marker_x"] = marker_x
    profile["marker_aligned"] = marker_aligned
    profile["marker_mapping_confidence"] = map_conf
    profile["marker_mapping_reason"] = map_reason

    results = []
    balances = []
    current_date = pd.NaT
    candidate = 0
    unparsed = 0

    def clean_raw(lw):
        return re.sub(r"\s+", " ", " ".join(str(w.get("text", "")).strip() for w in lw)).strip()

    for idx in range(h + 1, len(grouped)):
        lw = grouped[idx]
        raw = clean_raw(lw)
        if not raw:
            continue
        u = raw.upper()
        if re.match(r"^PAGE\s+\d+|^CONTINUED", u):
            continue

        if _header_score([w.get("text", "") for w in lw]) >= 5 and "DATE" in u:
            continue

        row_date = current_date
        for w in lw:
            t = str(w.get("text", "")).strip()
            if _PARTY_DATE_RE.match(t):
                d = pd.to_datetime(t, dayfirst=True, errors="coerce")
                if not pd.isna(d):
                    row_date = d
                    current_date = d
                break

        row_kind = _classify_universal_ledger_row(raw)
        if row_kind == "SUMMARY_TOTAL":
            continue

        if row_kind in ("OPENING_BALANCE", "CLOSING_BALANCE"):
            nums = _PARTY_MONEY_RE.findall(raw)
            if nums:
                amount = abs(_parse_signed_amount(nums[-1])[0])
                side = _balance_side_from_line(raw)
                if side in ("DR", "CR") and amount > 0:
                    kind = "opening" if re.search(r"\bOPENING\s+BALANCE\b", raw, re.I) else "closing"
                    if re.search(r"\b(?:DEBIT|CREDIT)\s+BALANCE\b", raw, re.I):
                        kind = "closing"
                    balances.append({"kind": kind, "amount": amount, "side": side,
                                     "date": row_date, "plant_section": plant_section,
                                     "source_page": page_number, "source_row": source_offset + idx,
                                     "raw": raw})
            continue

        info, new_date = _infer_row_transaction_semantics(lw, profile, row_date, marker_mapping, profile.get("marker_x"))
        if info is None:
            if profile.get("has_type") and not pd.isna(row_date):
                texts = [str(w.get("text", "")).strip() for w in lw]
                side = None
                for j, t in enumerate(texts):
                    if t.upper() in ("DR", "CR") and _side_token_is_in_structural_band(lw, j, profile, profile.get("marker_x")):
                        side = t.upper()
                        break
                if side:
                    candidates = _numeric_tokens_near_columns(lw, profile, set())
                    strict_candidates = _strict_transaction_candidates(candidates, profile)
                    tx = [c for c in strict_candidates if c["column"] in ("DR", "CR")]
                    ac = [c for c in strict_candidates if c["column"] == "AMOUNT"]
                    chosen = min(tx, key=lambda c: c["distance"]) if tx else (min(ac, key=lambda c: c["distance"]) if ac else None)
                    if chosen and chosen["amount"] > 0:
                        info = {"date": row_date, "side": side, "amount": chosen["amount"],
                                "marker": side, "description": "", "voucher": "", "vtype": "",
                                "source_amount_column": chosen["column"], "source_amount_distance": chosen["distance"]}
            if info is None and (profile.get("has_debit") or profile.get("has_credit")):
                candidates = _numeric_tokens_near_columns(lw, profile, set())
                tx = [
                    c for c in _strict_transaction_candidates(candidates, profile)
                    if c.get("column") in ("DR", "CR")
                ]
                if tx:
                    chosen = min(tx, key=lambda c: (c["distance"], -c["amount"]))
                    body = []
                    for j, w in enumerate(lw):
                        t = str(w.get("text", "")).strip()
                        if not t or j == chosen["index"] or _PARTY_DATE_RE.match(t):
                            continue
                        body.append(t)
                    desc, voucher, vtype = _extract_voucher_and_type(" ".join(body))
                    info = {"date": row_date, "side": chosen["column"], "amount": chosen["amount"],
                            "marker": "", "description": _clean_transaction_text(desc),
                            "voucher": voucher, "vtype": vtype,
                            "source_amount_column": chosen["column"],
                            "source_amount_distance": chosen["distance"]}
            if info is None:
                has_explicit_date = any(
                    _PARTY_DATE_RE.match(str(w.get("text", "")).strip())
                    for w in lw
                )
                has_structural_marker = any(
                    str(w.get("text", "")).strip().upper() in ("DR", "CR", "TO", "BY")
                    and _side_token_is_in_structural_band(
                        lw, j, profile, profile.get("marker_x")
                    )
                    for j, w in enumerate(lw)
                )
                if (
                    results
                    and not has_explicit_date
                    and not has_structural_marker
                    and not pd.isna(row_date)
                    and pd.to_datetime(results[-1].get("Date"), errors="coerce") == pd.to_datetime(row_date, errors="coerce")
                    and re.search(r"[A-Za-z]", raw)
                ):
                    existing = str(results[-1].get("Particulars", "") or "").strip()
                    continuation = re.sub(r"\s+", " ", raw).strip()
                    if continuation and continuation not in existing:
                        results[-1]["Particulars"] = (
                            f"{existing} | {continuation}" if existing else continuation
                        )
                continue
        candidate += 1
        rec = _make_party_record(info["date"], info["side"], info["amount"],
                                 info.get("description", ""), info.get("voucher", ""), info.get("vtype", ""),
                                 plant_section, page_number, source_offset + idx, "UNIVERSAL_SEMANTIC")
        if rec:
            rec["Row Type"] = "TRANSACTION"
            results.append(rec)
        else:
            unparsed += 1
        current_date = new_date

    return results, balances, candidate, unparsed


def _infer_implicit_closing_rows(page_txns, page_text, page_number, plant_section):
    """Infer an implicit closing-balance row from arithmetic evidence only."""
    if not page_txns:
        return page_txns, []

    text = str(page_text or "")
    lines = [re.sub(r"\s+", " ", x).strip() for x in text.splitlines() if str(x).strip()]
    if not lines:
        return page_txns, []

    summary_amounts = []
    for line in lines:
        u = line.upper()
        if not re.search(r"\b(?:TOTAL|GRAND TOTAL|BALANCE|CLOSING|OPENING)\b", u):
            continue
        vals = []
        for token in _PARTY_MONEY_RE.findall(line):
            try:
                vals.append(abs(float(_parse_signed_amount(token)[0])))
            except Exception:
                pass
        if vals:
            summary_amounts.append((line, vals))

    if not summary_amounts:
        return page_txns, []

    total_values = []
    for line, vals in summary_amounts:
        if re.search(r"\b(?:TOTAL|GRAND TOTAL)\b", line.upper()):
            total_values.extend(vals)
    if len(total_values) < 2:
        return page_txns, []

    candidate = page_txns[-1]
    marker = str(candidate.get("Type", "")).upper()
    if marker not in ("DR", "CR"):
        return page_txns, []

    amount = abs(float(candidate.get("Amount", 0) or 0))
    if amount <= 0:
        return page_txns, []

    matched = False
    for i, a in enumerate(total_values):
        for j, b in enumerate(total_values):
            if i == j:
                continue
            if abs(abs(a - b) - amount) <= max(0.02, amount * 1e-7):
                matched = True
                break
        if matched:
            break

    if not matched:
        return page_txns, []

    candidate_row = candidate.get("Source Row")
    try:
        last_row = max(
            int(x.get("Source Row")) for x in page_txns
            if x.get("Source Row") is not None
        )
        if candidate_row is None or int(candidate_row) != last_row:
            return page_txns, []
    except Exception:
        pass

    last_total_pos = -1
    for pos, line in enumerate(lines):
        if re.search(r"\b(?:TOTAL|GRAND TOTAL|BALANCE|CLOSING)\b", line.upper()):
            last_total_pos = pos
    if last_total_pos < 0:
        return page_txns, []

    out = list(page_txns[:-1])
    closing = {
        "kind": "closing",
        "amount": amount,
        "side": marker,
        "date": pd.to_datetime(candidate.get("Date"), errors="coerce"),
        "plant_section": plant_section,
        "source_page": page_number,
        "source_row": candidate.get("Source Row"),
        "raw": "Implicit closing balance inferred from ledger totals: "
               + str(candidate.get("Particulars", "")),
    }
    return out, [closing]


def extract_party_pdf_transactions(uploaded_file):
    """Universal PDF normalizer."""
    uploaded_file.seek(0)
    pdf_bytes = uploaded_file.read()
    transactions = []
    raw_pages = []
    opening_records = []
    closing_records = []
    diagnostics = []
    sections_seen = {}

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        page_count = len(pdf.pages)
        for page_number, page in enumerate(pdf.pages, 1):
            text = page.extract_text() or ""
            raw_lines = text.splitlines()
            lines = [re.sub(r"\s+", " ", x).strip() for x in raw_lines]
            raw_pages.append({"Page": page_number, "Text": text})
            if not any(x.strip() for x in lines):
                continue

            section = _extract_pdf_section_and_balances(lines, page_number)
            structure = _detect_pdf_page_structure(lines)
            sections_seen.setdefault(section, {"plant_section": section, "pages": []})["pages"].append(page_number)

            page_txns, page_balances, candidate_lines, unparsed = _parse_pdf_page_universal(
                page, lines, section, page_number
            )

            balance_seen = set()
            for b in page_balances:
                key = (b.get("kind"), page_number, b.get("source_row"), section)
                if key in balance_seen:
                    continue
                balance_seen.add(key)
                (opening_records if b["kind"] == "opening" else closing_records).append(b)

            page_txns, implicit_balances = _infer_implicit_closing_rows(
                page_txns, text, page_number, section
            )
            for b in implicit_balances:
                closing_records.append(b)

            transactions.extend(page_txns)
            diagnostics.append({
                "page": page_number,
                "plant_section": section,
                "structure": "UNIVERSAL_SEMANTIC_READER",
                "detected_layout": structure,
                "candidate_lines": candidate_lines,
                "rows_loaded": len(page_txns),
                "unparsed": unparsed,
            })

    transactions = _dedupe_party_transactions(transactions)
    dates = pd.to_datetime([x.get("Date") for x in transactions], errors="coerce").dropna()
    last_date = dates.max() if not dates.empty else pd.NaT

    sections_detail = []
    for section, info in sections_seen.items():
        sections_detail.append({
            "plant_section": section,
            "pages": info["pages"],
            "opening": [x for x in opening_records if x.get("plant_section") == section],
            "closing": [x for x in closing_records if x.get("plant_section") == section],
            "transaction_count": sum(1 for x in transactions if x.get("Plant/Section") == section),
        })

    metadata = _finalize_party_metadata(
        opening_records, closing_records, transactions, diagnostics, sections_detail
    )
    metadata["last_transaction_date"] = last_date
    metadata["manual_review"] = len(transactions) == 0 or any(
        int(d.get("candidate_lines", 0) or 0) > 0 and int(d.get("rows_loaded", 0) or 0) == 0
        for d in diagnostics
    )
    return transactions, raw_pages, page_count, metadata


def _detect_excel_section(row_values, current_section, sheet_name):
    text = " ".join(_value_text(v) for v in row_values if _value_text(v)).strip()
    if not text:
        return current_section
    u = text.upper()
    if re.search(r"DURGA\s+INFRA.*\([^)]*\)", text, re.I):
        return re.sub(r"\s+", " ", text).strip()
    if re.search(r"^ACCOUNT\s*:", u):
        return re.sub(r"^ACCOUNT\s*:\s*", "", text, flags=re.I).strip()
    if "LEDGER ACCOUNT" in u:
        return current_section or sheet_name
    return current_section


def _normalize_party_dataframe(raw_df, source_name="Party Ledger"):
    """Normalize XLS/XLSX/CSV without a fixed template."""
    if raw_df is None or raw_df.empty:
        return [], {"header_found": False, "rows_seen": 0, "rows_loaded": 0, "unparsed": 0}

    header_row, header_score = _detect_header_row(raw_df)
    if header_row is None:
        return [], {"header_found": False, "rows_seen": len(raw_df), "rows_loaded": 0, "unparsed": len(raw_df)}

    header_values = []
    seen = {}
    for idx, value in enumerate(raw_df.iloc[header_row].tolist()):
        name = _value_text(value) or f"Column_{idx + 1}"
        count = seen.get(name, 0) + 1
        seen[name] = count
        header_values.append(name if count == 1 else f"{name}_{count}")

    df = raw_df.iloc[header_row + 1:].copy()
    df.columns = header_values
    df = df.dropna(how="all")
    excel_rows = [int(i) + 1 for i in df.index]  # real Excel row numbers
    df = df.reset_index(drop=True)
    col_pos = {name: i + 1 for i, name in enumerate(df.columns)}

    date_col = _find_alias_column(df, PARTY_DATE_ALIASES)
    side_col = _find_alias_column(df, PARTY_SIDE_ALIASES)
    debit_col = _find_alias_column(df, PARTY_DEBIT_ALIASES, exclude=[side_col])
    credit_col = _find_alias_column(df, PARTY_CREDIT_ALIASES, exclude=[side_col, debit_col])
    amount_col = _find_alias_column(df, PARTY_AMOUNT_ALIASES, exclude=[side_col, debit_col, credit_col])
    balance_col = _find_alias_column(df, ["Balance", "Running Balance", "Closing Balance", "Balance Amount"], exclude=[side_col, debit_col, credit_col, amount_col])
    desc_col = _find_alias_column(df, PARTY_DESCRIPTION_ALIASES)
    voucher_col = _find_alias_column(df, PARTY_VOUCHER_ALIASES, exclude=[desc_col])
    vtype_col = _find_alias_column(df, PARTY_VOUCHER_TYPE_ALIASES, exclude=[desc_col, voucher_col])
    txn_type_col = _find_alias_column(df, ["Txn Type", "Transaction Type", "Entry Type", "Voucher Type", "Type"], exclude=[side_col, desc_col, voucher_col])
    wht_col = _find_alias_column(df, PARTY_WHT_ALIASES)

    # SAP-style signed Party Ledger: the SIGN of the amount is the accounting
    # side (positive = Debit, negative = Credit). Journal Entry Type / Document
    # Type values (RE, KZ, KR, CL...) are document codes, never Dr/Cr.
    normalized_headers = {_norm_header(c): c for c in df.columns}
    posting_key_col = normalized_headers.get("posting key")
    document_type_col = normalized_headers.get("document type")
    entry_type_col = normalized_headers.get("journal entry type")
    amount_header_norm = _norm_header(amount_col) if amount_col is not None else ""
    sap_signed_amount_mode = bool(
        amount_col is not None
        and debit_col is None
        and credit_col is None
        and (
            posting_key_col is not None
            or entry_type_col is not None
            or any(k in amount_header_norm for k in
                   ("local currency", "company code currency", "cocode", "crcy"))
        )
    )
    if sap_signed_amount_mode:
        side_col = None
        txn_type_col = None
        if entry_type_col is not None:
            vtype_col = entry_type_col
    # -1 when the Party Excel was exported from OUR books (vendor view)
    sap_flip = -1.0 if st.session_state.get("party_excel_from_our_books", False) else 1.0

    result = []
    balances = []
    unparsed = 0
    current_date = pd.NaT
    current_section = str(source_name)
    tally_marker_count = 0
    data_sample = df.head(min(len(df), 80))
    for _, sample_row in data_sample.iterrows():
        sample_text = " ".join(_value_text(v) for v in sample_row.tolist() if _value_text(v))
        if re.search(r"(?:^|\s)(?:DR|CR|TO|BY)(?:\s|$)", sample_text, re.I):
            tally_marker_count += 1
    tally_mode = tally_marker_count >= 2 and not sap_signed_amount_mode

    for pos, (_, row) in enumerate(df.iterrows()):
        local_row = excel_rows[pos]
        values = [_value_text(v) for v in row.tolist()]
        row_text = " | ".join(v for v in values if v)
        if not row_text:
            continue
        current_section = _detect_excel_section(values, current_section, source_name)
        upper = row_text.upper()
        if _header_score(values) >= 5 and "DATE" in upper:
            continue

        date_value = pd.to_datetime(row[date_col], errors="coerce", dayfirst=True) if date_col is not None else pd.NaT
        if not pd.isna(date_value):
            current_date = date_value
        elif not pd.isna(current_date):
            date_value = current_date

        # Balance rows are captured separately and never fed to matching.
        if re.search(r"\bOPENING\s+BALANCE\b|\bCLOSING\s+BALANCE\b|\bDEBIT\s+BALANCE\b|\bCREDIT\s+BALANCE\b", upper):
            nums = _PARTY_MONEY_RE.findall(row_text)
            if nums:
                amount = abs(_parse_signed_amount(nums[-1])[0])
                side = None
                if side_col is not None:
                    side = _detect_side_value(row[side_col])
                side = side or _balance_side_from_line(row_text)
                if side is None and debit_col is not None and credit_col is not None:
                    d = abs(_parse_signed_amount(row[debit_col])[0])
                    c = abs(_parse_signed_amount(row[credit_col])[0])
                    side = "DR" if d > c else "CR" if c > d else None
                if amount > 0 and side:
                    kind = "opening" if "OPENING BALANCE" in upper else "closing"
                    balances.append({
                        "kind": kind, "amount": amount, "side": side,
                        "date": date_value, "plant_section": current_section,
                        "source_page": source_name, "source_row": local_row,
                        "raw": row_text,
                    })
            continue

        # SAP "CL" rows are carried-forward balances, not transactions.
        if (sap_signed_amount_mode and entry_type_col is not None
                and _value_text(row[entry_type_col]).upper() == "CL"):
            cl_amt = _parse_signed_amount(row[amount_col])[0] * sap_flip
            if cl_amt and not pd.isna(date_value):
                balances.append({
                    "kind": "opening",
                    "amount": abs(cl_amt),
                    "side": "DR" if cl_amt > 0 else "CR",
                    "date": date_value + pd.Timedelta(days=1),
                    "plant_section": current_section,
                    "source_page": source_name,
                    "source_row": local_row,
                    "raw": row_text,
                })
            continue

        if pd.isna(date_value):
            continue

        side = _detect_side_value(row[side_col]) if side_col is not None else None
        d = abs(_parse_signed_amount(row[debit_col])[0]) if debit_col is not None else 0.0
        c = abs(_parse_signed_amount(row[credit_col])[0]) if credit_col is not None else 0.0
        amount = 0.0
        source_signed_amount = None

        if sap_signed_amount_mode and amount_col is not None:
            source_signed_amount = _parse_signed_amount(row[amount_col])[0] * sap_flip
            if abs(source_signed_amount) > 0:
                side = "DR" if source_signed_amount > 0 else "CR"
                amount = abs(source_signed_amount)

        row_side_from_text = None
        if tally_mode:
            row_side_from_text = _tally_side(
                re.match(r"^(?:.*?\b)?(DR|CR|TO|BY)\b", row_text, re.I).group(1)
                if re.match(r"^(?:.*?\b)?(DR|CR|TO|BY)\b", row_text, re.I) else ""
            )
        if sap_signed_amount_mode and source_signed_amount is not None and abs(source_signed_amount) > 0:
            pass
        elif tally_mode and row_side_from_text:
            side = row_side_from_text
            amount = max(d, c)
            if amount <= 0 and amount_col is not None:
                amount = abs(_parse_signed_amount(row[amount_col])[0])
        elif d > 0 and c <= 0:
            side, amount = "DR", d
        elif c > 0 and d <= 0:
            side, amount = "CR", c
        elif side and amount_col is not None:
            amount = abs(_parse_signed_amount(row[amount_col])[0])
        elif amount_col is not None:
            signed, embedded_side = _parse_signed_amount(row[amount_col])
            amount = abs(signed)
            side = side or embedded_side

        if side not in ("DR", "CR") and txn_type_col is not None:
            side = _semantic_side_from_transaction_type(row[txn_type_col])
            if amount_col is not None:
                amount = abs(_parse_signed_amount(row[amount_col])[0])

        if amount <= 0 and amount_col is None and balance_col is not None:
            amount = 0.0

        if amount <= 0 or side not in ("DR", "CR"):
            unparsed += 1
            continue

        desc = _value_text(row[desc_col]) if desc_col is not None else ""
        voucher = _value_text(row[voucher_col]) if voucher_col is not None else ""
        vtype = _value_text(row[vtype_col]) if vtype_col is not None else (_value_text(row[txn_type_col]) if txn_type_col is not None else "")
        if not desc:
            desc, _, _ = _extract_voucher_and_type(row_text)

        rec = _make_party_record(
            date_value, side, amount, desc, voucher, vtype,
            current_section, source_name, local_row, "EXCEL_CSV"
        )
        if rec:
            party_wht = (
                parse_amount(row[wht_col])
                if wht_col is not None and wht_col in row.index
                else 0.0
            )
            rec["Withholding Tax"] = float(party_wht)
            rec["Party Net Amount"] = float(amount)
            if sap_signed_amount_mode:
                hl_col = amount_col
            elif side == "DR" and d > 0 and debit_col is not None:
                hl_col = debit_col
            elif side == "CR" and c > 0 and credit_col is not None:
                hl_col = credit_col
            else:
                hl_col = amount_col
            rec["Excel Col"] = col_pos.get(hl_col) if hl_col is not None else None

            if sap_signed_amount_mode and source_signed_amount is not None:
                # Positive SAP amount = Debit, negative = Credit. When a WHT
                # column exists its sign is preserved; blank WHT = 0.
                signed_wht = float(party_wht) * sap_flip if wht_col is not None else 0.0
                effective_signed = float(source_signed_amount) + signed_wht

                rec["SAP Signed Amount"] = float(source_signed_amount)
                rec["Effective Signed Amount"] = float(effective_signed)
                rec["Match Amount"] = abs(float(effective_signed))

                rec["Type"] = "DR" if float(source_signed_amount) > 0 else "CR"
                rec["Debit"] = float(amount) if rec["Type"] == "DR" else 0.0
                rec["Credit"] = float(amount) if rec["Type"] == "CR" else 0.0
                rec["Net Amount"] = (
                    float(amount) if rec["Type"] == "DR" else -float(amount)
                )
            else:
                rec["Match Amount"] = float(amount)

            result.append(rec)

    return result, {
        "header_found": True,
        "header_score": header_score,
        "rows_seen": len(df),
        "rows_loaded": len(result),
        "unparsed": unparsed,
        "date_column": date_col,
        "debit_column": debit_col,
        "credit_column": credit_col,
        "amount_column": amount_col,
        "balance_column": balance_col,
        "side_column": side_col,
        "description_column": desc_col,
        "voucher_column": voucher_col,
        "voucher_type_column": vtype_col,
        "transaction_type_column": txn_type_col,
        "withholding_tax_column": wht_col,
        "sap_signed_amount_mode": sap_signed_amount_mode,
        "opening_records": balances,
    }


def _read_party_excel_or_csv(uploaded_file):
    name = str(getattr(uploaded_file, "name", "")).lower()
    uploaded_file.seek(0)
    if name.endswith(".csv"):
        raw = pd.read_csv(uploaded_file, header=None, dtype=object, keep_default_na=False, sep=None, engine="python")
        return [("CSV", raw)]
    excel = pd.ExcelFile(uploaded_file)
    sheets = []
    for sheet in excel.sheet_names:
        uploaded_file.seek(0)
        raw = pd.read_excel(uploaded_file, sheet_name=sheet, header=None, dtype=object)
        sheets.append((sheet, raw))
    return sheets


def extract_party_excel_transactions(uploaded_file):
    """Universal Excel/CSV reader with separate transaction and balance models."""
    sheets = _read_party_excel_or_csv(uploaded_file)
    result = []
    diagnostics = []
    opening_records = []
    closing_records = []
    sections_seen = {}

    for sheet_name, raw in sheets:
        txns, diag = _normalize_party_dataframe(raw, sheet_name)
        result.extend(txns)
        diagnostics.append({"sheet": sheet_name, **diag})
        for rec in diag.get("opening_records", []):
            (opening_records if rec.get("kind") == "opening" else closing_records).append(rec)
        for section in sorted(set(x.get("Plant/Section", sheet_name) for x in txns)):
            sections_seen.setdefault(section, {"plant_section": section, "pages": [sheet_name]})

    result = _dedupe_party_transactions(result)
    sections_detail = []
    all_sections = set(sections_seen) | {x.get("plant_section") for x in opening_records + closing_records if x.get("plant_section")}
    for section in sorted(x for x in all_sections if x):
        sections_detail.append({
            "plant_section": section,
            "pages": sections_seen.get(section, {}).get("pages", []),
            "opening": [x for x in opening_records if x.get("plant_section") == section],
            "closing": [x for x in closing_records if x.get("plant_section") == section],
            "transaction_count": sum(1 for x in result if x.get("Plant/Section") == section),
        })

    metadata = _finalize_party_metadata(
        opening_records, closing_records, result, diagnostics, sections_detail
    )
    metadata["manual_review"] = not bool(result) or any(
        int(d.get("unparsed", 0) or 0) > max(5, int(d.get("rows_seen", 0) or 0) * 0.60)
        for d in diagnostics
    )
    return result, metadata


def extract_party_metadata(party_file, party_pages=None, party_transactions=None, party_metadata=None):
    """Return (party_name, closing_date, closing_balance, last_transaction_date)."""
    party_name = ""
    closing_date = pd.NaT
    closing_balance = None
    last_transaction_date = pd.NaT

    if party_metadata:
        closing_date = pd.to_datetime(party_metadata.get("closing_date"), errors="coerce")
        closing_balance = party_metadata.get("closing_balance")
        last_transaction_date = pd.to_datetime(party_metadata.get("last_transaction_date"), errors="coerce")

    if party_pages:
        for line in str(party_pages[0].get("Text", "")).splitlines():
            text = re.sub(r"\s+", " ", line).strip()
            if not text:
                continue
            if re.search(
                r"\b(?:LEDGER|ACCOUNT|PAGE|DATE|PARTICULARS|CIN|GSTIN?|PAN|E-?MAIL|PHONE|MOBILE|TEL)\b"
                r"|FROM\s+\d|^\d",
                text, re.I,
            ):
                continue
            # Our own company (the account name in the party's ledger) is never the party.
            if re.search(r"DURGA\s+INFRA", text, re.I) or len(text) < 4:
                continue
            # First letterhead line of the Party ledger = the party's own name.
            party_name = _clean_party_name(text)
            if party_name:
                break
        if not party_name:
            for line in str(party_pages[0].get("Text", "")).splitlines():
                text = re.sub(r"\s+", " ", line).strip()
                if text and not re.search(r"LEDGER|ACCOUNT|PAGE|DATE|PARTICULARS", text, re.I):
                    party_name = _clean_party_name(text)
                    if party_name:
                        break

    if party_transactions:
        dates = pd.to_datetime([x.get("Date") for x in party_transactions], errors="coerce").dropna()
        if not dates.empty:
            last_transaction_date = dates.max()

    if pd.isna(closing_date):
        close_records = (party_metadata or {}).get("closing_records", []) if party_metadata else []
        close_dates = pd.to_datetime([x.get("date") for x in close_records], errors="coerce").dropna()
        if not close_dates.empty:
            closing_date = close_dates.max()
        elif not pd.isna(last_transaction_date):
            closing_date = last_transaction_date

    if not party_name and party_file is not None:
        party_name = Path(getattr(party_file, "name", "Party Ledger")).stem
        party_name = re.sub(r"(?i)\b(supplier|party)[ _-]*ledger\b", "", party_name).strip(" _-")
    return party_name or "Party Ledger", closing_date, closing_balance, last_transaction_date


# =========================================================
# PARTY SUMMARY
# =========================================================
def _fy_start_date(fy):
    """Return the 1-Apr start date for an FY string such as FY 2026-27."""
    m = re.search(r"(\d{4})-(\d{2})$", str(fy or ""))
    if not m:
        return pd.NaT
    return pd.Timestamp(int(m.group(1)), 4, 1)


def _signed_balance(amount, side):
    """Internal convention: Debit positive, Credit negative."""
    amount = abs(float(amount or 0.0))
    side = str(side or "").upper()
    if side == "DR":
        return amount
    if side == "CR":
        return -amount
    return 0.0


def _balance_from_signed(value):
    value = float(value or 0.0)
    if value > 0:
        return abs(value), "DR"
    if value < 0:
        return abs(value), "CR"
    return 0.0, ""


def _calculate_party_fy_balances(transactions, party_metadata, selected_fy):
    """Calculate Party FY opening/closing independently for every plant.

    Only genuine transaction rows participate in the arithmetic. An explicit
    opening balance is used with its actual Dr/Cr side; otherwise the previous
    FY calculated closing is carried forward for that SAME plant/section.
    """
    target_start = _fy_start_date(selected_fy)
    if pd.isna(target_start):
        return {
            "opening_balance": 0.0,
            "opening_source": "No valid FY",
            "closing_balance": 0.0,
            "sections": [],
        }

    tx_rows = []
    for row in transactions or []:
        d = pd.to_datetime(row.get("Date"), errors="coerce")
        if pd.isna(d):
            continue
        section = row.get("Plant/Section") or row.get("plant_section") or "Unidentified Section"
        debit = abs(float(row.get("Debit", 0) or 0))
        credit = abs(float(row.get("Credit", 0) or 0))
        tx_rows.append({"date": d, "section": str(section), "debit": debit, "credit": credit})

    opening_records = list((party_metadata or {}).get("opening_records", []) or [])
    closing_records = list((party_metadata or {}).get("closing_records", []) or [])

    sections = set(x["section"] for x in tx_rows)
    sections.update(str(x.get("plant_section") or "Unidentified Section") for x in opening_records)
    sections.update(str(x.get("plant_section") or "Unidentified Section") for x in closing_records)
    sections = sorted(sections)

    explicit_openings = {}
    for rec in opening_records:
        section = str(rec.get("plant_section") or "Unidentified Section")
        d = pd.to_datetime(rec.get("date"), errors="coerce")
        if pd.isna(d):
            continue
        explicit_openings.setdefault(section, []).append(rec)
    for section in explicit_openings:
        explicit_openings[section].sort(key=lambda x: pd.to_datetime(x.get("date"), errors="coerce"))

    section_results = []
    aggregate_opening = 0.0
    aggregate_closing = 0.0

    for section in sections:
        section_tx = [x for x in tx_rows if x["section"] == section]
        section_tx.sort(key=lambda x: x["date"])
        explicit = explicit_openings.get(section, [])

        dates = [x["date"] for x in section_tx]
        dates += [pd.to_datetime(x.get("date"), errors="coerce") for x in explicit]
        dates = [d for d in dates if not pd.isna(d)]
        if not dates:
            continue
        first_year = min(d.year if d.month >= 4 else d.year - 1 for d in dates)
        target_year = target_start.year

        signed_opening = 0.0
        opening_source = "Calculated carry-forward"
        current_fy = first_year
        target_opening = None
        target_closing = None

        while current_fy <= target_year:
            fy_start = pd.Timestamp(current_fy, 4, 1)
            fy_end = pd.Timestamp(current_fy + 1, 3, 31, 23, 59, 59)
            fy_name = f"FY {current_fy}-{str(current_fy + 1)[-2:]}"

            fy_explicit = [
                r for r in explicit
                if pd.to_datetime(r.get("date"), errors="coerce") >= fy_start
                and pd.to_datetime(r.get("date"), errors="coerce") <= fy_end
            ]
            if fy_explicit:
                rec = fy_explicit[-1]
                signed_opening = _signed_balance(rec.get("amount", 0), rec.get("side"))
                opening_source = (
                    f"{section} — explicit opening "
                    f"{str(rec.get('side') or '').upper() or 'balance'}"
                )
            elif current_fy == first_year and signed_opening == 0.0:
                opening_source = f"{section} — calculated from available ledger data"

            fy_tx = [x for x in section_tx if fy_start <= x["date"] <= fy_end]
            debit = sum(x["debit"] for x in fy_tx)
            credit = sum(x["credit"] for x in fy_tx)
            signed_closing = signed_opening + debit - credit

            if current_fy == target_year:
                target_opening = signed_opening
                target_closing = signed_closing
                section_results.append({
                    "plant_section": section,
                    "financial_year": fy_name,
                    "opening_balance": signed_opening,
                    "opening_side": _balance_from_signed(signed_opening)[1],
                    "total_debit": debit,
                    "total_credit": credit,
                    "closing_balance": signed_closing,
                    "closing_side": _balance_from_signed(signed_closing)[1],
                    "opening_source": opening_source,
                    "transactions": len(fy_tx),
                })

            signed_opening = signed_closing
            current_fy += 1

        if target_opening is not None:
            aggregate_opening += target_opening
            aggregate_closing += target_closing

    return {
        "opening_balance": aggregate_opening,
        "opening_source": "Per-plant FY opening / previous FY closing carry-forward",
        "closing_balance": aggregate_closing,
        "sections": section_results,
    }


def calculate_party_summary(transactions, opening_balance=0.0, selected_fy=None, party_metadata=None):
    """Party summary with per-plant FY carry-forward and correct balance side."""
    if not transactions:
        return {
            "financial_year": selected_fy,
            "transactions": 0,
            "opening_balance": opening_balance,
            "total_debit": 0.0,
            "total_credit": 0.0,
            "closing_balance": opening_balance,
            "opening_source": "No Party Ledger transactions",
            "sections": [],
        }

    df = pd.DataFrame(transactions)
    if "Date" not in df.columns:
        return {
            "financial_year": selected_fy,
            "transactions": 0,
            "opening_balance": opening_balance,
            "total_debit": 0.0,
            "total_credit": 0.0,
            "closing_balance": opening_balance,
            "opening_source": "No valid transaction date",
            "sections": [],
        }

    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df["FY"] = df["Date"].apply(get_fy)
    fy_df = df[df["FY"] == selected_fy].copy() if selected_fy is not None else df.copy()

    debit = pd.to_numeric(fy_df.get("Debit", 0), errors="coerce").fillna(0).abs().sum()
    credit = pd.to_numeric(fy_df.get("Credit", 0), errors="coerce").fillna(0).abs().sum()

    if party_metadata and selected_fy is not None:
        balance_info = _calculate_party_fy_balances(transactions, party_metadata, selected_fy)
        opening = float(balance_info["opening_balance"])
        closing = float(balance_info["closing_balance"])
        source = balance_info["opening_source"]
        sections = balance_info["sections"]
    else:
        opening = float(opening_balance or 0.0)
        closing = opening + float(debit) - float(credit)
        source = "Legacy signed opening balance"
        sections = []

    return {
        "financial_year": selected_fy,
        "transactions": len(fy_df),
        "opening_balance": opening,
        "total_debit": float(debit),
        "total_credit": float(credit),
        "closing_balance": closing,
        "opening_source": source,
        "sections": sections,
    }


# =========================================================
# INTERNAL REVERSAL / CANCELLATION DETECTION
# =========================================================
def _pair_opposite_reversal_indexes(
    df,
    date_col,
    side_getter,
    amount_getter,
    amount_tolerance=1.0,
):
    """Indexes of same-date opposite-side rows that cancel each other
    (one-for-one, closest amounts first, within the amount tolerance)."""
    if df is None or getattr(df, "empty", True) or date_col not in df.columns:
        return set()

    try:
        tolerance = abs(float(amount_tolerance))
    except Exception:
        tolerance = 1.0

    by_date = {}
    for idx, row in df.iterrows():
        dt = pd.to_datetime(row.get(date_col), errors="coerce")
        if pd.isna(dt):
            continue
        side = str(side_getter(row) or "").strip().upper()
        if side not in ("DR", "CR", "DEBIT", "CREDIT"):
            continue
        normalized_side = "DR" if side in ("DR", "DEBIT") else "CR"
        try:
            amount = abs(float(amount_getter(row) or 0.0))
        except Exception:
            amount = 0.0
        if amount <= 0.0000001:
            continue
        by_date.setdefault(dt.normalize(), {"DR": [], "CR": []})[normalized_side].append(
            (idx, amount)
        )

    cancelled = set()
    for groups in by_date.values():
        debit_rows = groups["DR"]
        credit_rows = groups["CR"]
        candidates = []
        for d_idx, d_amt in debit_rows:
            for c_idx, c_amt in credit_rows:
                diff = abs(d_amt - c_amt)
                if diff <= tolerance + 1e-9:
                    candidates.append((diff, d_idx, c_idx))

        candidates.sort(key=lambda x: x[0])
        used_debit = set()
        used_credit = set()
        for _, d_idx, c_idx in candidates:
            if d_idx in used_debit or c_idx in used_credit:
                continue
            used_debit.add(d_idx)
            used_credit.add(c_idx)
            cancelled.add(d_idx)
            cancelled.add(c_idx)

    return cancelled


def _ensure_our_match_fields(df):
    """Populate WHT and effective Match Amount without changing SAP net amount."""
    working = df.copy()
    withholding_col = find_column(
        working,
        ["Withholding Tax", "Withholding Tax Amount", "WHT", "TDS", "TDS Amount"],
    )
    if withholding_col is not None:
        working["Withholding Tax"] = working[withholding_col].apply(parse_amount)
    else:
        working["Withholding Tax"] = 0.0

    def calc(row):
        amount = float(row.get("__amount", 0.0) or 0.0)
        tax = float(row.get("Withholding Tax", 0.0) or 0.0)
        return abs(amount) + abs(tax) if amount < 0 else abs(amount)

    working["Match Amount"] = working.apply(calc, axis=1)
    working["SAP Net Amount"] = pd.to_numeric(
        working.get("__amount", 0.0), errors="coerce"
    ).fillna(0.0).abs()
    working["Match Side"] = pd.to_numeric(
        working.get("__amount", 0.0), errors="coerce"
    ).fillna(0.0).apply(lambda x: "Debit" if x > 0 else "Credit")
    return working


def _our_reversal_row_indexes(df, amount_tolerance=1.0):
    """Our-Ledger same-date Debit/Credit reversals using effective amount."""
    if df is None or getattr(df, "empty", True):
        return set()
    if "__date" not in df.columns or "__amount" not in df.columns:
        return set()

    working = _ensure_our_match_fields(df)
    return _pair_opposite_reversal_indexes(
        working,
        "__date",
        lambda row: row.get("Match Side", ""),
        lambda row: row.get("Match Amount", abs(float(row.get("__amount", 0.0) or 0.0))),
        amount_tolerance,
    )


def _party_reversal_row_indexes(df, amount_tolerance=1.0):
    """Party-Ledger same-date DR/CR reversals within amount tolerance."""
    if df is None or getattr(df, "empty", True):
        return set()
    if "Date" not in df.columns or "Type" not in df.columns:
        return set()

    return _pair_opposite_reversal_indexes(
        df,
        "Date",
        lambda row: row.get("Type", ""),
        lambda row: row.get("Match Amount", row.get("Amount", 0.0)),
        amount_tolerance,
    )


# =========================================================
# TRANSACTION MATCHING ENGINE
# =========================================================
def build_our_matching_df(prepared_our, selected_fy, amount_tolerance=1.0):
    if prepared_our is None or prepared_our.empty:
        return pd.DataFrame()

    df = prepared_our[prepared_our["__fy"] == selected_fy].copy()
    if df.empty:
        return pd.DataFrame()

    cl = find_our_cl_entries(df)
    df = df[~df.index.isin(set(cl.index))].copy()
    if df.empty:
        return pd.DataFrame()

    df["Our Row"] = df.index.astype(str)
    df["Match Side"] = df["__amount"].apply(
        lambda x: "Debit" if x > 0 else "Credit"
    )

    # SAP credit rows are grossed up by their own WHT/TDS for matching:
    #   ABS(-10,401.27) + ABS(-4.73) = 10,406.00
    # The original SAP amount is never changed.
    df = _ensure_our_match_fields(df)

    # Same-date opposite rows within tolerance cancel (effective amounts).
    cancellation_rows = _our_reversal_row_indexes(df, amount_tolerance)
    if cancellation_rows:
        df = df[~df.index.isin(cancellation_rows)].copy()

    if df.empty:
        return pd.DataFrame()

    visible_cols = [c for c in df.columns if not str(c).startswith("__")]

    def make_description(row):
        values = []
        for col in visible_cols:
            value = row.get(col)
            if value is not None and not pd.isna(value) and str(value).strip():
                values.append(str(value).strip())
        return " | ".join(values[:8])

    reference_col = find_column(df, ["Reference"])
    if reference_col is not None:
        df["Our Particulars"] = df[reference_col].apply(
            lambda v: "" if v is None or pd.isna(v) else str(v).strip()
        )
    else:
        df["Our Particulars"] = ""

    df["Our Description"] = df.apply(make_description, axis=1)
    return df.reset_index(drop=True)


def match_transactions(our_df, party_df, date_tolerance, amount_tolerance):
    """
    Matching engine (opposite sides):
        Our Debit  <-> Party Credit
        Our Credit <-> Party Debit

    Pass 1: ONE_TO_ONE            (voucher > date > amount > text similarity)
    Pass 2: ONE_TO_MANY_SPLIT     (one Our row = several Party rows)
    Pass 3: MANY_TO_ONE_NETTING   (several Our rows net to one Party row)

    Every Our row and every Party row is used at most once.
    """
    amount_tolerance = float(amount_tolerance)
    date_tolerance = int(date_tolerance)

    our = our_df.copy() if our_df is not None else pd.DataFrame()
    party = party_df.copy() if party_df is not None else pd.DataFrame()

    # ---------- internal reversals removed BEFORE matching ----------
    if not our.empty:
        our_rev = _pair_opposite_reversal_indexes(
            our, "__date",
            lambda r: r.get("Match Side", ""),
            lambda r: r.get("Match Amount", abs(float(r.get("__amount", 0.0) or 0.0))),
            amount_tolerance,
        )
        if our_rev:
            our = our[~our.index.isin(our_rev)].copy()

    if not party.empty:
        party_rev = _party_reversal_row_indexes(party, amount_tolerance)
        if party_rev:
            party = party[~party.index.isin(party_rev)].copy()

    if our.empty:
        return pd.DataFrame(), pd.DataFrame(), party.reset_index(drop=True)
    if party.empty:
        return pd.DataFrame(), our.copy().reset_index(drop=True), pd.DataFrame()

    party = party.reset_index(drop=True)
    party["Party Row"] = party.index.astype(int)
    our = our.reset_index(drop=True)

    # ---------- helpers ----------
    def party_amount(p):
        try:
            return float(p.get("Match Amount", p.get("Amount", 0)) or 0)
        except Exception:
            return float(p.get("Amount", 0) or 0)

    def date_diff(a, b):
        a = pd.to_datetime(a, errors="coerce")
        b = pd.to_datetime(b, errors="coerce")
        if pd.isna(a) or pd.isna(b):
            return 999999
        return abs((a - b).days)

    def voucher_hit(o, p):
        pv = normalize_voucher(p.get("Vch No.", ""))
        if not pv:
            return 0
        for col in o.index:
            if str(col).startswith("__"):
                continue
            ov = normalize_voucher(o.get(col, ""))
            if ov and (pv in ov or ov in pv):
                return 1
        return 0

    used_party, used_our = set(), set()
    matches = []

    # =====================================================
    # PASS 1 + 2: per Our row -> one-to-one, else split
    # =====================================================
    for oi, o in our.iterrows():
        our_side = o["Match Side"]
        want = "CR" if our_side == "Debit" else "DR"
        our_amt = float(o["Match Amount"])
        our_date = pd.to_datetime(o["__date"], errors="coerce")

        # ---------- ONE-TO-ONE ----------
        cands = []
        for pi, p in party.iterrows():
            if pi in used_party or str(p.get("Type", "")).upper() != want:
                continue
            p_amt = party_amount(p)
            ad = abs(our_amt - p_amt)
            if ad > amount_tolerance:
                continue
            dd = date_diff(our_date, p.get("Date"))
            if dd > date_tolerance:
                continue
            sim = text_similarity(o.get("Our Description", ""), p.get("Particulars", ""))
            cands.append((-voucher_hit(o, p), dd, ad, -sim, pi))

        if cands:
            cands.sort()
            _, dd, ad, nsim, pi = cands[0]
            p = party.loc[pi]
            used_party.add(pi)
            used_our.add(oi)
            matches.append({
                "Our Row": o["Our Row"],
                "Our Date": o["__date"],
                "Our Side": our_side,
                "Our Amount": our_amt,
                "Our SAP Net Amount": float(o.get("SAP Net Amount", our_amt)),
                "Withholding Tax": float(o.get("Withholding Tax", 0.0)),
                "Party Row": int(pi),
                "Party Rows": str(int(pi)),
                "Match Type": "ONE_TO_ONE",
                "Party Date": p["Date"],
                "Party Side": p["Type"],
                "Party Amount": p["Amount"],
                "Party Total Amount": party_amount(p),
                "Party Vch No.": p.get("Vch No.", ""),
                "Party Particulars": p.get("Particulars", ""),
                "Date Difference": dd,
                "Amount Difference": ad,
                "Description Similarity": round(-nsim * 100, 1),
                "Confidence": "High" if ad == 0 and dd <= 1 else ("Medium" if ad == 0 else "Low"),
            })
            continue

        # ---------- ONE-TO-MANY SPLIT ----------
        pool = []
        for pi, p in party.iterrows():
            if pi in used_party or str(p.get("Type", "")).upper() != want:
                continue
            p_amt = party_amount(p)
            if p_amt <= 0 or p_amt > our_amt + amount_tolerance:
                continue
            dd = date_diff(our_date, p.get("Date"))
            if dd <= date_tolerance:
                pool.append((pi, p_amt, dd))
        pool.sort(key=lambda x: (x[2], -x[1]))
        pool = pool[:18]

        best = None

        def search(pos, chosen, total, max_dd):
            nonlocal best
            if best is not None:
                return
            if len(chosen) >= 2 and abs(our_amt - total) <= amount_tolerance:
                best = (chosen[:], total, max_dd)
                return
            if pos >= len(pool) or len(chosen) >= 8 or total > our_amt + amount_tolerance:
                return
            pi, amt, dd = pool[pos]
            search(pos + 1, chosen + [pi], total + amt, max(max_dd, dd))
            search(pos + 1, chosen, total, max_dd)

        search(0, [], 0.0, 0)
        if best is None:
            continue

        rows, p_total, max_dd = best
        combo = party.loc[rows]
        used_party.update(rows)
        used_our.add(oi)
        ad = abs(our_amt - p_total)
        matches.append({
            "Our Row": o["Our Row"],
            "Our Date": o["__date"],
            "Our Side": our_side,
            "Our Amount": our_amt,
            "Our SAP Net Amount": float(o.get("SAP Net Amount", our_amt)),
            "Withholding Tax": float(o.get("Withholding Tax", 0.0)),
            "Party Row": int(rows[0]),
            "Party Rows": ", ".join(map(str, rows)),
            "Match Type": "ONE_TO_MANY_SPLIT",
            "Party Date": " | ".join(str(x) for x in combo["Date"]),
            "Party Side": want,
            "Party Amount": p_total,
            "Party Total Amount": p_total,
            "Party Vch No.": " | ".join(str(x) for x in combo["Vch No."] if str(x).strip()),
            "Party Particulars": " + ".join(str(x) for x in combo["Particulars"]),
            "Split Details": " ; ".join(
                f"Row {int(r['Party Row'])}: {r.get('Particulars','')} ₹{float(r.get('Amount',0) or 0):.2f}"
                for _, r in combo.iterrows()),
            "Date Difference": max_dd,
            "Amount Difference": ad,
            "Description Similarity": 0.0,
            "Confidence": "High" if ad == 0 and max_dd <= 1 else "Medium",
        })

    # =====================================================
    # PASS 3: MANY-TO-ONE NETTING (several Our rows -> one Party row)
    # =====================================================
    for pi, p in party.iterrows():
        if pi in used_party:
            continue
        p_amt = party_amount(p)
        p_side = str(p.get("Type", "")).upper()
        if p_amt <= 0 or p_side not in ("DR", "CR"):
            continue
        p_date = pd.to_datetime(p.get("Date"), errors="coerce")

        pool = []
        for oi, o in our.iterrows():
            if oi in used_our:
                continue
            dd = date_diff(o.get("__date"), p_date)
            if dd > date_tolerance:
                continue
            signed = float(o.get("Match Amount", 0) or 0)
            if str(o.get("Match Side", "")) == "Credit":
                signed = -signed
            pool.append((oi, signed, dd))
        pool.sort(key=lambda x: (x[2], -abs(x[1])))
        pool = pool[:18]

        target = p_amt if p_side == "CR" else -p_amt
        best = None

        def search_net(pos, chosen, total, max_dd):
            nonlocal best
            if best is not None:
                return
            if len(chosen) >= 2 and abs(total - target) <= amount_tolerance:
                best = (chosen[:], total, max_dd)
                return
            if pos >= len(pool) or len(chosen) >= 8:
                return
            oi, s, dd = pool[pos]
            search_net(pos + 1, chosen + [oi], total + s, max(max_dd, dd))
            search_net(pos + 1, chosen, total, max_dd)

        search_net(0, [], 0.0, 0)
        if best is None:
            continue

        rows, net, max_dd = best
        combo = our.loc[rows]
        used_party.add(pi)
        used_our.update(rows)
        ad = abs(net - target)
        gross = float(combo["Match Amount"].abs().sum())
        matches.append({
            "Our Row": ", ".join(combo["Our Row"].astype(str)),
            "Our Date": " | ".join(str(x) for x in combo["__date"]),
            "Our Side": " | ".join(combo["Match Side"].astype(str)),
            "Our Amount": gross,
            "Our Net Match Amount": abs(net),
            "Our SAP Net Amount": float(combo["SAP Net Amount"].sum()) if "SAP Net Amount" in combo else gross,
            "Withholding Tax": float(combo["Withholding Tax"].sum()) if "Withholding Tax" in combo else 0.0,
            "Party Row": int(pi),
            "Party Rows": str(int(pi)),
            "Match Type": "MANY_TO_ONE_NETTING",
            "Our Netting Details": " ; ".join(
                f"Row {r['Our Row']}: {r['Match Side']} ₹{float(r['Match Amount']):.2f}"
                for _, r in combo.iterrows()),
            "Party Date": p.get("Date"),
            "Party Side": p_side,
            "Party Amount": p_amt,
            "Party Total Amount": p_amt,
            "Party Vch No.": p.get("Vch No.", ""),
            "Party Particulars": p.get("Particulars", ""),
            "Date Difference": max_dd,
            "Amount Difference": ad,
            "Description Similarity": 0.0,
            "Confidence": "High" if ad == 0 and max_dd <= 1 else "Medium",
        })

    # ---------- invoice number check ----------
    # Amount/date matched but the invoice numbers differ (extra party invoice or a
    # wrong invoice number in our books): both sides go back to unmatched, marked,
    # so they appear in the RECO statement.
    matched = verify_document_numbers(pd.DataFrame(matches), our)
    our_notes, party_notes = {}, {}
    mismatched = pd.DataFrame()
    if not matched.empty:
        is_mismatch = matched["Doc No. Check"].eq(DOC_CHECK_MISMATCH)
        mismatched = matched[is_mismatch].reset_index(drop=True)
        our_index = {str(v): i for i, v in our["Our Row"].items()}
        for _, m in mismatched.iterrows():
            party_doc = str(m.get("Party Vch No.", "") or "").strip()
            our_doc = str(m.get("Our Doc No.", "") or "").strip()
            for r in str(m["Our Row"]).split(","):
                oi = our_index.get(r.strip())
                if oi is not None:
                    used_our.discard(oi)
                    our_notes[oi] = f"{DOC_CHECK_MISMATCH} (party: {party_doc})"
            for r in str(m["Party Rows"]).split(","):
                if r.strip().isdigit():
                    used_party.discard(int(r))
                    party_notes[int(r)] = f"{DOC_CHECK_MISMATCH} (ours: {our_doc})"
        matched = matched[~is_mismatch].reset_index(drop=True)

    # ---------- results ----------
    unmatched_our = our[~our.index.isin(used_our)].copy()
    unmatched_party = party[~party["Party Row"].astype(int).isin(used_party)].copy()
    if our_notes:
        unmatched_our.insert(0, "Doc No. Check", [our_notes.get(i, "") for i in unmatched_our.index])
    if party_notes:
        unmatched_party.insert(0, "Doc No. Check", [party_notes.get(int(i), "") for i in unmatched_party["Party Row"]])
    matched.attrs["doc_mismatch"] = mismatched
    return matched, unmatched_our, unmatched_party


DOC_CHECK_SAME = "✅ Same"
DOC_CHECK_MISMATCH = "⚠️ Mismatch"
DOC_CHECK_SKIPPED = "—"


def _our_document_number(row):
    """Invoice number on Our side.

    SAP Excel: the Reference column (e.g. MEW/285/25-26).
    SAP PDF:   the Vendor Document, i.e. the first token of the row
               (e.g. 5750052401024 5100001971 Invoice - Gross (RE )).
    """
    if str(row.get("__ledger_source", "")) == "PDF":
        tokens = str(row.get("Particulars", "") or "").split()
        return tokens[0] if tokens and re.search(r"\d", tokens[0]) else ""
    value = row.get("Reference", "")
    return "" if value is None or pd.isna(value) else str(value).strip()


def _our_row_is_payment(row):
    entry_type = str(row.get("Journal Entry Type", "") or "").strip().upper()
    text = f"{row.get('Particulars', '')} {row.get('Our Description', '')}".upper()
    return entry_type in ("KZ", "ZP") or "PAYMENT" in text or "(KZ" in text


def _party_document_numbers(vch_no, particulars):
    """Document-like tokens (at least 4 characters with a digit) on the Party side."""
    docs = []
    for part in [vch_no] + str(particulars or "").split():
        for piece in str(part or "").split(" | "):
            norm = normalize_voucher(piece)
            if len(norm) >= 4 and re.search(r"\d", norm):
                docs.append(norm)
    return docs


def verify_document_numbers(matched, our_df):
    """Second check on matched invoices: is the invoice number the same on both sides?

    Adds two columns to the matched pairs:
        Our Doc No.    - Our invoice number (Excel: Reference, PDF: Vendor Document)
        Doc No. Check  - ✅ Same / ⚠️ Mismatch / — (payments, or no number on one side)
    A mismatch means the amounts agree but the invoice numbers do not, e.g. the
    party raised an extra invoice or an invoice number was booked wrongly;
    match_transactions then moves both sides back to unmatched.
    """
    if matched is None or matched.empty:
        return matched

    our_rows = {}
    if our_df is not None and not our_df.empty and "Our Row" in our_df.columns:
        our_rows = {str(r["Our Row"]): r for _, r in our_df.iterrows()}

    our_docs, checks = [], []
    for _, m in matched.iterrows():
        rows = [our_rows[r.strip()] for r in str(m.get("Our Row", "")).split(",") if r.strip() in our_rows]
        invoices = [r for r in rows if not _our_row_is_payment(r)]
        docs = [d for d in (_our_document_number(r) for r in invoices) if d]
        our_docs.append(" | ".join(docs))

        party_docs = _party_document_numbers(m.get("Party Vch No.", ""), m.get("Party Particulars", ""))
        norm_docs = [normalize_voucher(d) for d in docs if len(normalize_voucher(d)) >= 3]
        same = lambda d, p: d == p or (len(d) >= 6 and d in p) or (len(p) >= 6 and p in d)
        if not norm_docs or not party_docs:
            checks.append(DOC_CHECK_SKIPPED)
        elif any(same(d, p) for d in norm_docs for p in party_docs):
            checks.append(DOC_CHECK_SAME)
        else:
            checks.append(DOC_CHECK_MISMATCH)

    result = matched.copy()
    at = list(result.columns).index("Match Type") + 1 if "Match Type" in result.columns else 0
    result.insert(at, "Doc No. Check", checks)
    result.insert(at + 1, "Our Doc No.", our_docs)
    # Show both invoice numbers side by side.
    if "Party Vch No." in result.columns:
        result.insert(at + 2, "Party Vch No.", result.pop("Party Vch No."))
    return result


def _reco_period(selected_fy):
    """Return period text and FY end date from labels such as FY 2026-27."""
    try:
        start_year = int(str(selected_fy).replace("FY ", "").split("-")[0])
        return (
            f"1-Apr-{start_year} to 31-Mar-{start_year + 1}",
            pd.Timestamp(start_year + 1, 3, 31),
            start_year,
        )
    except Exception:
        return str(selected_fy), pd.NaT, None


def _calculate_fy_withholding_tax(our_ledger_df, selected_fy, amount_tolerance=1.0):
    """Total Withholding Tax directly from Our Ledger for the selected FY."""
    if our_ledger_df is None or getattr(our_ledger_df, "empty", True):
        return 0.0

    df = our_ledger_df.copy()
    if "Withholding Tax" not in df.columns:
        withholding_col = find_column(
            df,
            ["Withholding Tax", "Withholding Tax Amount", "WHT", "TDS", "TDS Amount"],
        )
        if withholding_col is None:
            return 0.0
        df["Withholding Tax"] = df[withholding_col].apply(parse_amount)
    else:
        df["Withholding Tax"] = df["Withholding Tax"].apply(parse_amount)

    if "__fy" in df.columns:
        fy_mask = df["__fy"].astype(str).eq(str(selected_fy))
    elif "__date" in df.columns:
        fy_mask = df["__date"].apply(get_fy).astype(str).eq(str(selected_fy))
    else:
        return 0.0

    return float(df.loc[fy_mask, "Withholding Tax"].sum())


def _rebuild_reco_sheet_from_template(
    template_path,
    output_path,
    selected_fy,
    unmatched_our,
    unmatched_party,
    our_summary=None,
    party_summary=None,
    party_name="Party Ledger",
    party_closing_date=None,
    party_closing_balance=None,
    party_last_entry_date=None,
    our_closing_date=None,
    our_closing_balance=None,
    our_last_entry_date=None,
    our_last_entry_fy=None,
    withholding_tax_total=0.0,
):
    """
    Build the user's RECO FORMAT.xlsx layout.

    A = Party Debit  -> not in Our Credit
    B = Party Credit -> not in Our Debit
    C = Our Debit    -> not in Party Credit
    D = Our Credit   -> not in Party Debit
    """

    from openpyxl import load_workbook
    from openpyxl.utils.cell import range_boundaries
    from copy import copy

    wb = load_workbook(template_path)
    ws = wb["RECO"] if "RECO" in wb.sheetnames else wb.active

    original_max_row = ws.max_row
    original_max_col = ws.max_column

    original_styles = {}
    for r in range(1, original_max_row + 1):
        original_styles[r] = []
        for c in range(1, original_max_col + 1):
            cell = ws.cell(r, c)
            original_styles[r].append({
                "style": copy(cell._style),
                "number_format": cell.number_format,
                "alignment": copy(cell.alignment),
                "protection": copy(cell.protection),
                "fill": copy(cell.fill),
                "font": copy(cell.font),
                "border": copy(cell.border),
            })

    original_heights = {
        r: ws.row_dimensions[r].height
        for r in range(1, original_max_row + 1)
    }

    original_merges = [str(rng) for rng in ws.merged_cells.ranges]

    party = unmatched_party.copy() if unmatched_party is not None else pd.DataFrame()
    our = unmatched_our.copy() if unmatched_our is not None else pd.DataFrame()

    if not party.empty and "Type" in party.columns:
        party_type = party["Type"].astype(str).str.upper()
        a_df = party[party_type.eq("DR")].copy()
        b_df = party[party_type.eq("CR")].copy()
    else:
        a_df = pd.DataFrame()
        b_df = pd.DataFrame()

    if not our.empty and "Match Side" in our.columns:
        c_df = our[our["Match Side"].eq("Debit")].copy()
        d_df = our[our["Match Side"].eq("Credit")].copy()
    else:
        c_df = pd.DataFrame()
        d_df = pd.DataFrame()

    # When a selected-FY withholding-tax total exists, reserve one extra row
    # directly below the C-3 transactions for the WHT summary (included in C-3 total).
    c3_wht_row_needed = bool(withholding_tax_total and abs(float(withholding_tax_total)) > 0.0000001)

    counts = {
        "A": max(2, len(a_df)),
        "B": max(2, len(b_df)),
        "C": max(1, len(c_df) + (1 if c3_wht_row_needed else 0)),
        "D": max(2, len(d_df)),
    }

    base_counts = {"A": 2, "B": 2, "C": 1, "D": 2}
    growth = {
        k: max(0, counts[k] - base_counts[k])
        for k in counts
    }

    for rng in list(ws.merged_cells.ranges):
        ws.unmerge_cells(str(rng))

    insert_points = [
        ("D", 32),
        ("C", 26),
        ("B", 22),
        ("A", 18),
    ]

    for key, row in insert_points:
        if growth[key] > 0:
            ws.insert_rows(row, growth[key])

    def mapped_row(original_row):
        offset = 0
        if original_row >= 18:
            offset += growth["A"]
        if original_row >= 22:
            offset += growth["B"]
        if original_row >= 26:
            offset += growth["C"]
        if original_row >= 32:
            offset += growth["D"]
        return original_row + offset

    def apply_saved_style(source_row, target_row):
        height = original_heights.get(source_row)
        if height is not None:
            ws.row_dimensions[target_row].height = height

        for c in range(1, original_max_col + 1):
            src = original_styles[source_row][c - 1]
            dst = ws.cell(target_row, c)

            dst._style = copy(src["style"])
            dst.number_format = src["number_format"]
            dst.alignment = copy(src["alignment"])
            dst.protection = copy(src["protection"])
            dst.fill = copy(src["fill"])
            dst.font = copy(src["font"])
            dst.border = copy(src["border"])

    section_templates = {
        "A": (16, [16, 17]),
        "B": (20, [20, 21]),
        "C": (25, [25]),
        "D": (30, [30, 31]),
    }

    for key, (original_start, template_rows) in section_templates.items():
        new_start = mapped_row(original_start)

        for i in range(counts[key]):
            target_row = new_start + i
            source_row = template_rows[i % len(template_rows)]
            apply_saved_style(source_row, target_row)

            for c in range(1, original_max_col + 1):
                ws.cell(target_row, c).value = None

    # ---------------- Header / period ----------------
    period_text, fy_end_date, start_year = _reco_period(selected_fy)
    fy_label = str(selected_fy).replace("FY ", "FY-")

    # MY BOOKS = OUR SAP ledger (label uses actual latest transaction date).
    my_books_date = pd.to_datetime(our_last_entry_date, errors="coerce")
    my_books_fy_label = str(
        our_last_entry_fy or (
            get_fy(my_books_date) if not pd.isna(my_books_date) else selected_fy
        )
    ).replace("FY ", "FY-")

    my_books_balance = our_closing_balance
    if my_books_balance is None and our_summary:
        my_books_balance = float(our_summary.get("closing_balance", 0.0))
    if my_books_balance is None:
        my_books_balance = 0.0

    # YOUR BOOKS = PARTY ledger.
    your_books_date = pd.to_datetime(party_last_entry_date, errors="coerce")
    your_books_fy_label = str(
        get_fy(your_books_date) if not pd.isna(your_books_date) else selected_fy
    ).replace("FY ", "FY-")

    if pd.isna(my_books_date):
        my_books_date = fy_end_date
    if pd.isna(your_books_date):
        your_books_date = fy_end_date

    ws["A6"] = f"Dated: {pd.Timestamp.today().strftime('%d.%m.%Y')}"
    ws["A8"] = period_text

    # ---------------- Party name in the "To:-" value cell ----------------
    # The company name already printed in the template (FROM side) is never overwritten.
    party_display_name = _clean_party_name(party_name) or "Party Ledger"

    def put_party_name_in_to_field(sheet, name):
        name = _clean_party_name(name) or "Party Ledger"

        to_cell = None
        for row in sheet.iter_rows():
            for cell in row:
                value = str(cell.value or "").strip()
                if not value:
                    continue
                norm = re.sub(r"\s+", " ", value).strip().casefold()
                norm = re.sub(r"\s*[:\-]+\s*$", "", norm)
                if norm in {"to", "to party", "party"} or re.match(r"^to\s*[:\-]", value, re.I):
                    to_cell = cell
                    break
            if to_cell is not None:
                break

        if to_cell is None:
            return False

        target_row = to_cell.row + 1
        target_col = to_cell.column

        for merged_range in sheet.merged_cells.ranges:
            if (merged_range.min_row <= target_row <= merged_range.max_row and
                    merged_range.min_col <= target_col <= merged_range.max_col):
                target_row = merged_range.min_row
                target_col = merged_range.min_col
                sheet.cell(target_row, target_col).value = name
                return True

        sheet.cell(target_row, target_col).value = name
        return True

    put_party_name_in_to_field(ws, party_display_name)

    ws["A12"] = (
        f"Closing Balance in my Books of Account ({my_books_fy_label}) "
        f"As At {my_books_date.strftime('%d/%m/%Y') if not pd.isna(my_books_date) else ''}"
    )
    # Our closing balance goes in with the opposite sign (Excel or PDF ledger):
    # negative in our books -> positive in F12, positive -> negative.
    ws["F12"] = -float(my_books_balance)
    ws["F12"].number_format = "#,##0.00"

    starts = {
        "A": mapped_row(16),
        "B": mapped_row(20),
        "C": mapped_row(25),
        "D": mapped_row(30),
    }

    heading_rows = {
        "A": mapped_row(15),
        "B": mapped_row(19),
        "C": mapped_row(24),
        "D": mapped_row(29),
    }

    section_title_rows = {
        "A": mapped_row(14),
        "B": mapped_row(18),
        "C": mapped_row(23),
        "D": mapped_row(28),
    }

    labels = {
        "A": ("A-1", "Party's Debit Site Not Show In Our Credit Site."),
        "B": ("B-2", "Party's Credit Site Not Show In Our Debit Site."),
        "C": ("C-3", "Our Debit Site Not Show In Party's Credit Site."),
        "D": ("D-4", "Our Credit Site Not Show In Party's Debit Site."),
    }

    for key, (label, description) in labels.items():
        title_row = section_title_rows[key]
        ws.cell(title_row, 1).value = label
        ws.cell(title_row, 2).value = description

    for key, row in heading_rows.items():
        ws.cell(row, 2).value = "Particulars"
        ws.cell(row, 3).value = "Date"
        ws.cell(row, 4).value = "Amount"

    def amount_for_our(row):
        return float(
            row.get(
                "Match Amount",
                abs(float(row.get("__amount", 0.0)))
            )
        )

    def write_section(key, frame):
        start_row = starts[key]

        for i in range(counts[key]):
            r = start_row + i
            ws.cell(r, 1).value = i + 1
            ws.cell(r, 2).value = None
            ws.cell(r, 3).value = None
            ws.cell(r, 4).value = None

        for i, (_, row) in enumerate(frame.iterrows()):
            r = start_row + i

            if key in ("A", "B"):
                particulars = row.get("Particulars", "")
                date_value = row.get("Date")
                amount = float(row.get("Amount", 0.0) or 0.0)
            else:
                # RECO C/D Particulars come from SAP's Reference column.
                particulars = row.get("Our Particulars", "")
                if not str(particulars).strip():
                    particulars = row.get("Our Description", "")
                date_value = row.get("__date")
                amount = amount_for_our(row)

            # Invoice number mismatch (amount matched, invoice no. differs): say so and highlight.
            doc_note = str(row.get("Doc No. Check", "") or "").strip()
            if doc_note:
                vch_no = str(row.get("Vch No.", "") or "").strip() if key in ("A", "B") else ""
                note = doc_note.replace(DOC_CHECK_MISMATCH, "Invoice No. mismatch")
                particulars = " ".join(x for x in (str(particulars or "").strip(), vch_no, f"- {note}") if x)
                for col in (2, 3, 4):
                    cell = ws.cell(r, col)
                    cell.fill = PatternFill("solid", fgColor="E9D5FF")
                    cell.font = Font(name=cell.font.name, size=cell.font.size, bold=True, color="5B21B6")

            date_parsed = pd.to_datetime(
                date_value,
                errors="coerce"
            )

            ws.cell(r, 1).value = i + 1
            ws.cell(r, 2).value = str(particulars or "")

            if not pd.isna(date_parsed):
                ws.cell(r, 3).value = date_parsed.to_pydatetime()
                ws.cell(r, 3).number_format = "dd.mm.yyyy"
            else:
                ws.cell(r, 3).value = None

            ws.cell(r, 4).value = amount
            ws.cell(r, 4).number_format = "#,##0.00"

    write_section("A", a_df)
    write_section("B", b_df)
    write_section("C", c_df)

    # C-3 WHT summary row: immediately below the last C-3 transaction,
    # included in the C-3 total.
    if c3_wht_row_needed:
        c3_wht_row = starts["C"] + len(c_df)
        wht_as_at = pd.to_datetime(our_last_entry_date, errors="coerce")
        if pd.isna(wht_as_at):
            wht_date_text = ""
        else:
            wht_date_text = wht_as_at.strftime("%d.%m.%Y")

        ws.cell(c3_wht_row, 1).value = len(c_df) + 1
        ws.cell(c3_wht_row, 2).value = "Withholding Tax"
        if wht_date_text:
            wht_date_value = pd.to_datetime(wht_as_at, errors="coerce")
            if not pd.isna(wht_date_value):
                ws.cell(c3_wht_row, 3).value = wht_date_value.to_pydatetime()
                ws.cell(c3_wht_row, 3).number_format = "dd.mm.yyyy"
            else:
                ws.cell(c3_wht_row, 3).value = None
        else:
            ws.cell(c3_wht_row, 3).value = None
        ws.cell(c3_wht_row, 4).value = abs(float(withholding_tax_total))
        ws.cell(c3_wht_row, 4).number_format = "#,##0.00"

    write_section("D", d_df)

    # ---------------- Section totals ----------------
    for key, total_row in heading_rows.items():
        start = starts[key]
        end = start + counts[key] - 1

        # Template convention: A/C positive, B/D negative.
        sign = "-" if key in ("B", "D") else ""

        ws.cell(total_row, 5).value = (
            f"={sign}SUM(D{start}:D{end})"
        )
        ws.cell(total_row, 5).number_format = "#,##0.00"

    # ---------------- Dispute / round-off / final balance ----------------
    dispute_row = mapped_row(32)
    kasar_row = mapped_row(33)
    closing_row = mapped_row(34)
    difference_row = mapped_row(35)

    total_cells = {
        key: heading_rows[key]
        for key in ("A", "B", "C", "D")
    }

    ws.cell(dispute_row, 6).value = (
        f"=ROUND(SUM("
        f"E{total_cells['A']},"
        f"E{total_cells['B']},"
        f"E{total_cells['C']},"
        f"E{total_cells['D']}"
        f"),2)"
    )
    ws.cell(dispute_row, 6).number_format = "#,##0.00"

    ws.cell(kasar_row, 6).value = 0
    ws.cell(kasar_row, 6).number_format = "#,##0.00"

    ws.cell(closing_row, 1).value = (
        f"Closing Balance in your Books of Account ({your_books_fy_label}) "
        f"As At {your_books_date.strftime('%d/%m/%Y') if not pd.isna(your_books_date) else ''}"
    )

    ws.cell(closing_row, 6).value = (
        f"=ROUND(F12+F{dispute_row}+F{kasar_row},2)"
    )
    ws.cell(closing_row, 6).number_format = "#,##0.00"

    # Raw Party Ledger closing balance (sign preserved) in column G of the
    # dynamic closing row.
    party_close_value = (
        float(party_closing_balance)
        if party_closing_balance is not None else 0.0
    )

    closing_label_prefix = "Closing Balance in your Books of Account"
    party_closing_row = None
    for r in range(1, ws.max_row + 1):
        value = ws.cell(r, 1).value
        if isinstance(value, str) and value.strip().startswith(closing_label_prefix):
            party_closing_row = r
            break

    if party_closing_row is None:
        raise RuntimeError(
            "Could not locate the dynamic 'Closing Balance in your Books of Account' row in RECO."
        )

    ws.cell(party_closing_row, 7).value = party_close_value
    ws.cell(party_closing_row, 7).number_format = "#,##0.00"

    # Difference amount directly below the Party closing balance (no label).
    difference_row = party_closing_row + 1

    for c in range(1, ws.max_column + 1):
        ws.cell(difference_row, c).value = None

    ws.cell(difference_row, 7).value = (
        f"=ABS(ABS(F{party_closing_row})-ABS(G{party_closing_row}))"
    )
    ws.cell(difference_row, 7).number_format = "#,##0.00"

    # ---------------- Note ----------------
    note_row = mapped_row(36)

    ws.cell(note_row, 1).value = "Note:1"
    ws.cell(note_row, 2).value = (
        "Unmatched transactions are listed from the transaction matching "
        "engine. Party Debit/Credit is shown using the reconciliation "
        "convention, SAP withholding tax/TDS is grossed up for matching, "
        f"and the configured amount tolerance is ₹{float(st.session_state.get('amount_tolerance', 1.0)):.2f}."
    )

    # Recreate original merges at their shifted rows AFTER all writes.
    for merge in original_merges:
        min_col, min_row, max_col, max_row = range_boundaries(merge)

        mapped_min_row = mapped_row(min_row)
        mapped_max_row = mapped_row(max_row)

        if mapped_min_row <= difference_row <= mapped_max_row:
            continue

        ws.merge_cells(
            start_row=mapped_min_row,
            start_column=min_col,
            end_row=mapped_max_row,
            end_column=max_col,
        )

    ws.freeze_panes = "A10"
    ws.sheet_view.showGridLines = False
    ws.print_title_rows = "1:10"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True

    wb.save(output_path)


def generate_reconciliation_excel(selected_fy, unmatched_our, unmatched_party,
                                   our_summary=None, party_summary=None,
                                   amount_tolerance=1.0,
                                   party_name="Party Ledger",
                                   party_closing_date=None,
                                   party_closing_balance=None,
                                   party_last_entry_date=None,
                                   our_closing_date=None,
                                   our_closing_balance=None,
                                   our_last_entry_date=None,
                                   our_last_entry_fy=None,
                                   withholding_tax_total=0.0):
    """Generate the user's reconciliation-format Excel in memory."""
    template_path = Path(__file__).with_name("RECO FORMAT.xlsx")
    if not template_path.exists():
        raise FileNotFoundError(
            f"RECO FORMAT.xlsx was not found beside this app. Expected: {template_path}"
        )

    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        output_path = Path(tmp.name)

    _rebuild_reco_sheet_from_template(
        template_path,
        output_path,
        selected_fy,
        unmatched_our,
        unmatched_party,
        our_summary=our_summary,
        party_summary=party_summary,
        party_name=party_name,
        party_closing_date=party_closing_date,
        party_closing_balance=party_closing_balance,
        party_last_entry_date=party_last_entry_date,
        our_closing_date=our_closing_date,
        our_closing_balance=our_closing_balance,
        our_last_entry_date=our_last_entry_date,
        our_last_entry_fy=our_last_entry_fy,
        withholding_tax_total=withholding_tax_total,
    )
    data = output_path.read_bytes()
    try:
        output_path.unlink()
    except Exception:
        pass
    return data


def _matched_rows_for_export(our_match, party_match, matched, amount_tolerance):
    """Rows of each ledger that were matched (used to decide what to highlight)."""
    our_ids, party_ids = set(), set()
    if matched is not None and not matched.empty:
        for v in matched["Our Row"].astype(str):
            our_ids.update(p.strip() for p in v.split(",") if p.strip())
        pcol = "Party Rows" if "Party Rows" in matched.columns else "Party Row"
        for v in matched[pcol].astype(str):
            party_ids.update(p.strip() for p in v.split(",") if p.strip())

    our_rows = our_match[our_match["Our Row"].astype(str).isin(our_ids)].copy()

    # same numbering the matching engine used (reversals removed, index reset)
    survivors = party_match.copy()
    rev = _party_reversal_row_indexes(survivors, amount_tolerance)
    if rev:
        survivors = survivors[~survivors.index.isin(rev)]
    survivors = survivors.reset_index(drop=True)
    party_rows = survivors[survivors.index.astype(str).isin(party_ids)].copy()
    return our_rows, party_rows


def render_highlighted_exports(our_match, party_match, matched, selected_fy, amount_tolerance):
    """Button + download buttons: highlighted ORIGINAL ledgers (PDF -> PDF, Excel -> Excel)."""
    st.subheader("🖍️ Highlighted Ledgers (same format as uploaded)")
    st.caption("PDF → highlighted PDF. Excel → highlighted Excel (matched amount cells only).")

    sig = (selected_fy, float(amount_tolerance), len(matched),
           st.session_state.get("loaded_our_name"), st.session_state.get("loaded_party_name"))

    if st.button("🖍️ Generate Highlighted Ledgers", key="gen_highlight"):
        our_rows, party_rows = _matched_rows_for_export(
            our_match, party_match, matched, amount_tolerance)
        our_df_raw = st.session_state.get("our_df")
        header = (find_column(our_df_raw, ["Amount (CoCode Crcy)", "Amount",
                                           "Amount (Company Code Currency)"])
                  if our_df_raw is not None else None)
        res = {"sig": sig}
        with st.spinner("Highlighting original ledgers..."):
            for kind, rows in (("our", our_rows), ("party", party_rows)):
                try:
                    res[kind] = build_highlighted_ledger(
                        kind,
                        st.session_state[f"{kind}_file_bytes"],
                        st.session_state[f"{kind}_file_name"],
                        rows,
                        sheet=st.session_state.get("our_sheet") if kind == "our" else None,
                        amount_header=header if kind == "our" else None,
                    )
                except Exception as e:
                    res[kind] = {"error": str(e)}
        st.session_state["hl_results"] = res

    res = st.session_state.get("hl_results")
    if res and res.get("sig") == sig:
        fy = str(selected_fy).replace(" ", "_")
        for kind, label in (("our", "Our_Ledger"), ("party", "Party_Ledger")):
            out = res.get(kind, {})
            if "error" in out:
                st.warning(f"⚠️ {label}: {out['error']}")
                continue
            st.download_button(
                f"📥 {label.replace('_', ' ')} — highlighted ({out['ext'].upper()[1:]})",
                data=out["data"],
                file_name=f"{label}_Highlighted_{fy}{out['ext']}",
                mime=out["mime"],
                key=f"dl_hl_{kind}",
            )
            msg = f"{label.replace('_', ' ')}: {out['highlighted']} amounts highlighted"
            if out["missed"]:
                st.warning(f"{msg}; {out['missed']} could not be located in the file.")
            else:
                st.caption(msg)


def display_matching_engine(
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
    st.divider()
    st.header(f"🔎 Transaction Matching Engine — {selected_fy}")

    our_match = build_our_matching_df(prepared_our, selected_fy, amount_tolerance)
    party_match = party_df_all[
        party_df_all["FY"] == selected_fy
    ].copy() if not party_df_all.empty else pd.DataFrame()

    if our_match.empty:
        st.warning(
            f"⚠️ Matching engine found 0 Our Ledger transactions for {selected_fy}. "
            "Check the Journal Entry Date and Amount columns in the SAP export."
        )
        st.caption(
            f"Prepared Our Ledger rows: {0 if prepared_our is None else len(prepared_our):,} | "
            f"Selected FY rows: {0 if prepared_our is None else int((prepared_our.get('__fy') == selected_fy).sum()):,}"
        )
        return

    if party_match.empty:
        st.warning(
            f"⚠️ Matching engine found 0 Party Ledger transactions for {selected_fy}. "
            "Check that the Party Ledger dates are parsed correctly and belong to this FY."
        )
        st.caption(
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

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Our Transactions", f"{total_our:,}")
    c2.metric("Party Transactions", f"{total_party:,}")
    c3.metric("Matched", f"{total_matched:,}")
    c4.metric("Match Rate", f"{rate:.1f}%")

    st.caption(
        "Matching uses opposite accounting sides: "
        "Our Debit ↔ Party Credit and Our Credit ↔ Party Debit. "
        "For SAP credit rows, Withholding Tax/TDS is added back to the "
        "negative SAP amount before matching the gross Party Ledger amount. "
        f"Amount matching allows a variance of ±₹{float(amount_tolerance):.2f}."
    )

    if not matched.empty:
        st.subheader(f"✅ Matched Transactions ({len(matched):,})")
        st.dataframe(
            matched,
            use_container_width=True,
            hide_index=True
        )
    else:
        st.warning("No transactions matched with the current tolerances.")

    st.subheader(f"⚠️ Invoice No. Mismatch ({len(doc_mismatch):,})")
    if doc_mismatch.empty:
        st.success("Every matched invoice has the same invoice number on both sides.")
    else:
        st.warning(
            "Amount and date match, but the invoice number differs, so these entries "
            "are kept unmatched and listed in the RECO statement. Check for an extra "
            "party invoice or a wrong invoice number in our books."
        )
        st.dataframe(doc_mismatch, use_container_width=True, hide_index=True)

    c1, c2 = st.columns(2)

    with c1:
        st.subheader(f"❌ Unmatched Our Ledger ({len(unmatched_our):,})")
        if unmatched_our.empty:
            st.success("All Our Ledger transactions are matched.")
        else:
            cols = [
                "Doc No. Check", "__date", "Match Side", "Match Amount",
                "Our Description", "Our Row"
            ]
            st.dataframe(
                unmatched_our[[c for c in cols if c in unmatched_our.columns]],
                use_container_width=True,
                hide_index=True
            )

    with c2:
        st.subheader(f"❌ Unmatched Party Ledger ({len(unmatched_party):,})")
        if unmatched_party.empty:
            st.success("All Party Ledger transactions are matched.")
        else:
            cols = [
                "Doc No. Check", "Date", "Type", "Amount",
                "Particulars", "Vch Type", "Vch No."
            ]
            st.dataframe(
                unmatched_party[[c for c in cols if c in unmatched_party.columns]],
                use_container_width=True,
                hide_index=True
            )

    st.success(
        f"✅ Matching engine completed: {total_matched:,} matched, "
        f"{len(unmatched_our):,} unmatched Our Ledger, "
        f"{len(unmatched_party):,} unmatched Party Ledger."
    )

    render_highlighted_exports(our_match, party_match, matched, selected_fy, amount_tolerance)

    # ---------------- Reconciliation format export ----------------
    st.session_state["reco_unmatched_our"] = unmatched_our.copy()
    st.session_state["reco_unmatched_party"] = unmatched_party.copy()
    st.session_state["reco_selected_fy"] = selected_fy

    st.divider()
    st.subheader("📄 Reconciliation Statement")
    st.caption(
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
        st.download_button(
            "📥 Download Reconciliation Statement (RECO Format)",
            data=reco_bytes,
            file_name=f"RECO_{selected_fy.replace(' ', '_')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
            help="Downloads the unmatched transactions in the supplied RECO FORMAT.xlsx layout.",
        )
    except Exception as e:
        st.warning(f"⚠️ RECO export is not available: {e}")


# =========================================================
# SUMMARY DISPLAY
# =========================================================
def display_summary(title, summary):
    st.subheader(title)
    st.write(f"**Financial Year:** {summary['financial_year']}")

    c1, c2, c3 = st.columns(3)
    c1.metric("Transactions", f"{summary['transactions']:,}")
    c2.metric("Total Debit", format_amount(summary["total_debit"]))
    c3.metric("Total Credit", format_amount(summary["total_credit"]))

    st.divider()
    c1, c2 = st.columns(2)

    with c1:
        st.info(
            "🔵 Opening Balance\n\n"
            f"### {balance_text(summary['opening_balance'])}"
        )

    with c2:
        closing = summary["closing_balance"]
        if closing > 0:
            st.success(
                "🟢 Closing Balance\n\n"
                f"### {balance_text(closing)}"
            )
        elif closing < 0:
            st.warning(
                "🟠 Closing Balance\n\n"
                f"### {balance_text(closing)}"
            )
        else:
            st.success(
                "🟢 Closing Balance\n\n"
                "### ₹0.00 Nil"
            )


# =========================================================
# FILE UPLOAD
# =========================================================
c1, c2 = st.columns(2)

with c1:
    st.subheader("📘 Our Ledger")
    our_file = st.file_uploader(
        "Upload Our Ledger Excel / PDF File",
        type=["xlsx", "xls", "pdf"],
        key="our_ledger"
    )
    if our_file:
        st.success(f"✅ Uploaded: {our_file.name}")
        st.caption(f"File size: {our_file.size / (1024 * 1024):.2f} MB")

with c2:
    st.subheader("📕 Party / Supplier Ledger")
    party_file = st.file_uploader(
        "Upload Party Ledger",
        type=["pdf", "xlsx", "xls", "csv"],
        key="party_ledger"
    )
    if party_file:
        st.success(f"✅ Uploaded: {party_file.name}")
        st.caption(
            f"File size: {party_file.size / (1024 * 1024):.2f} MB • "
            "Universal reader: PDF / XLSX / XLS / CSV"
        )

st.divider()

# =========================================================
# MATCHING SETTINGS
# =========================================================
st.subheader("⚙️ Matching Settings")
c1, c2 = st.columns(2)

with c1:
    date_tolerance = st.number_input(
        "Date tolerance (days)",
        min_value=0,
        max_value=365,
        value=3,
        step=1
    )

with c2:
    amount_tolerance = st.number_input(
        "Amount tolerance (₹)",
        min_value=0.0,
        value=1.0,
        step=0.01,
        help="Transactions can differ by up to ₹1.00 and still match. Example: ₹19,460 can match ₹19,459 or ₹19,461."
    )
st.session_state["amount_tolerance"] = float(amount_tolerance)

st.checkbox(
    "Party Excel is exported from OUR books (vendor view) - invert sign",
    key="party_excel_from_our_books",
    help="Tick when the Party Excel is a SAP vendor ledger from our own system "
         "(invoice = negative). Leave off if it comes from the party's own books.",
)

st.divider()

# =========================================================
# SESSION STATE
# =========================================================
defaults = {
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

for key, value in defaults.items():
    if key not in st.session_state:
        st.session_state[key] = value


# =========================================================
# READ LEDGERS
# =========================================================
if st.button("▶️ READ LEDGERS", type="primary"):

    if our_file is None:
        st.warning("⚠️ Please upload the Our Ledger Excel / PDF file.")

    elif party_file is None:
        st.warning("⚠️ Please upload the Party Ledger file.")

    else:
        for key in [
            "our_df", "party_transactions", "party_pages",
            "party_page_count", "our_sheet"
        ]:
            st.session_state[key] = None if key == "our_df" else (
                [] if key in ["party_transactions", "party_pages"] else
                0 if key == "party_page_count" else None
            )

        st.session_state["selected_fy"] = None

        # ---------------- Our Ledger ----------------
        try:
            if our_file.name.lower().endswith(".pdf"):
                our_df = extract_our_pdf_transactions(our_file)
                our_sheet = "PDF Ledger"
                st.info("📘 Our Ledger PDF detected: standard Debit / Credit / TDS format")
            else:
                our_file.seek(0)
                excel = pd.ExcelFile(our_file)
                sheets = excel.sheet_names
                st.info("📘 Our Ledger sheets found: " + ", ".join(sheets))

                our_sheet = "SAPUI5 Export" if "SAPUI5 Export" in sheets else sheets[0]
                our_file.seek(0)
                our_df = pd.read_excel(our_file, sheet_name=our_sheet)
        except Exception as e:
            st.error(f"❌ Could not read Our Ledger: {e}")
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
                st.error(f"❌ Could not read Party Ledger: {e}")
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

            st.session_state["our_df"] = our_df
            st.session_state["party_transactions"] = party_transactions
            st.session_state["party_pages"] = party_pages
            st.session_state["party_page_count"] = party_page_count
            st.session_state["our_sheet"] = our_sheet
            st.session_state["loaded_our_name"] = our_file.name
            st.session_state["loaded_party_name"] = party_file.name
            st.session_state["our_file_bytes"] = our_file.getvalue()
            st.session_state["our_file_name"] = our_file.name
            st.session_state["party_file_bytes"] = party_file.getvalue()
            st.session_state["party_file_name"] = party_file.name
            st.session_state["party_name"] = party_name
            st.session_state["party_closing_date"] = party_closing_date
            st.session_state["party_closing_balance"] = party_closing_balance
            st.session_state["party_last_entry_date"] = party_last_entry_date
            st.session_state["party_opening_balance"] = party_metadata.get("opening_balance")
            st.session_state["party_manual_review"] = bool(party_metadata.get("manual_review", False))
            st.session_state["party_parser_diagnostics"] = party_metadata
            st.session_state["ledgers_loaded"] = True

            if st.session_state["party_manual_review"]:
                st.warning(
                    "⚠️ Party Ledger was uploaded, but some or all rows could not be "
                    "automatically normalized. Review the Party Ledger manually; the original file was not changed."
                )
            else:
                st.success("✅ Universal Party Ledger normalized successfully.")
        else:
            st.session_state["ledgers_loaded"] = False


# =========================================================
# DISPLAY
# =========================================================
if st.session_state["ledgers_loaded"]:

    our_df = st.session_state["our_df"]
    party_transactions = st.session_state["party_transactions"]
    party_pages = st.session_state["party_pages"]

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

    if st.session_state.get("selected_fy") not in all_fys:
        st.session_state["selected_fy"] = default_fy

    # ---------------- FY selector ----------------
    if all_fys:
        st.divider()
        st.header("📅 Financial Year")

        summary_fys = {"FY 2024-25", "FY 2025-26", "FY 2026-27"}
        selectable_fys = sorted(set(all_fys) | summary_fys)

        selected_fy = st.selectbox(
            "Select Financial Year",
            selectable_fys,
            key="selected_fy"
        )

        if selected_fy in common_fys:
            st.success(
                f"✅ {selected_fy} is available in BOTH Our Ledger and Party Ledger."
            )
        elif selected_fy in summary_fys:
            st.warning(
                f"ℹ️ {selected_fy} uses the new Ledger Summary carry-forward logic. "
                "The Our Ledger summary is calculated from the 31.12.2024 CL base."
            )
        else:
            st.warning(
                f"⚠️ {selected_fy} is available in only one ledger."
            )
    else:
        selected_fy = None
        st.error("❌ No Financial Year could be detected.")

    # ---------------- Our Entries ----------------
    if selected_fy is not None:
        st.divider()
        st.header(f"📘 Our Ledger Entries — {selected_fy}")

        if prepared_our is not None:
            our_fy_df = prepared_our[
                prepared_our["__fy"] == selected_fy
            ].copy()

            if not our_fy_df.empty:
                st.success(
                    f"✅ {len(our_fy_df):,} Our Ledger transactions found for {selected_fy}."
                )
                display_our_df = our_fy_df.drop(
                    columns=["__date", "__amount", "__entry_type", "__fy"],
                    errors="ignore"
                )
                st.dataframe(
                    display_our_df,
                    use_container_width=True,
                    hide_index=True
                )
            else:
                st.info(f"ℹ️ No Our Ledger transactions were found for {selected_fy}.")

    our_summary = None
    party_summary = None

    # ---------------- Our Summary ----------------
    if selected_fy is not None and our_df is not None:
        st.divider()
        our_summary, our_error = calculate_our_summary(
            our_df, selected_fy
        )
        if our_summary is not None:
            display_summary("📊 Our Ledger Summary", our_summary)
        else:
            st.warning(
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
            st.caption(
                f"📌 RECO My Books = Our SAP Ledger calculated FY closing: "
                f"{format_amount(_our_reco_balance)} "
                f"as at {_our_reco_date_text}"
            )

    # ---------------- Party Entries ----------------
    if selected_fy is not None:
        st.divider()
        st.header(f"📕 Party Ledger Entries — {selected_fy}")

        if st.session_state.get("party_name"):
            st.caption(f"Party Ledger: **{st.session_state.get('party_name')}**")
        parser_diag = st.session_state.get("party_parser_diagnostics", {})
        if parser_diag:
            loaded_count = len(party_transactions)
            if st.session_state.get("party_manual_review"):
                st.warning(
                    f"⚠️ Universal parser status: Manual review recommended. "
                    f"Normalized transactions: {loaded_count:,}. "
                    f"The source file was not modified."
                )
            else:
                st.success(
                    f"🔄 Universal parser: {loaded_count:,} transactions normalized "
                    "from the uploaded Party Ledger."
                )
        if st.session_state.get("party_closing_balance") is not None:
            close_date = pd.to_datetime(
                st.session_state.get("party_closing_date"), errors="coerce"
            )
            close_date_text = (
                close_date.strftime("%d/%m/%Y")
                if not pd.isna(close_date) else "last available date"
            )
            st.info(
                f"📌 Party Ledger last available balance: "
                f"{format_amount(st.session_state.get('party_closing_balance'))} "
                f"as at {close_date_text}"
            )

        party_fy_df = party_df_all[
            party_df_all["FY"] == selected_fy
        ].copy()

        if not party_fy_df.empty:
            st.success(
                f"✅ {len(party_fy_df):,} Party Ledger transactions found for {selected_fy}."
            )

            st.dataframe(
                party_fy_df.drop(columns=["FY"], errors="ignore"),
                use_container_width=True,
                hide_index=True
            )
        else:
            st.info(f"ℹ️ No Party Ledger transactions were found for {selected_fy}.")

    # ---------------- Party Summary ----------------
    if selected_fy is not None:
        st.divider()
        party_fy_df = party_df_all[
            party_df_all["FY"] == selected_fy
        ].copy()

        if not party_fy_df.empty:
            party_summary = calculate_party_summary(
                party_fy_df.drop(columns=["FY"], errors="ignore").to_dict("records"),
                opening_balance=float(st.session_state.get("party_opening_balance") or 0.0),
                selected_fy=selected_fy,
                party_metadata=st.session_state.get("party_parser_diagnostics") or {}
            )
        else:
            party_summary = {
                "financial_year": selected_fy,
                "transactions": 0,
                "opening_balance": float(st.session_state.get("party_opening_balance") or 0.0),
                "total_debit": 0.0,
                "total_credit": 0.0,
                "closing_balance": 0.0
            }

        display_summary("📊 Party Ledger Summary", party_summary)

        if party_fy_df.empty:
            st.info(
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
            prepared_our,
            party_df_all,
            selected_fy,
            date_tolerance,
            amount_tolerance,
            our_summary=our_summary,
            party_summary=party_summary,
            party_name=st.session_state.get("party_name", "Party Ledger"),
            party_closing_date=st.session_state.get("party_closing_date", pd.NaT),
            party_closing_balance=st.session_state.get("party_closing_balance"),
            party_last_entry_date=st.session_state.get("party_last_entry_date", pd.NaT),
            our_closing_date=our_closing_date,
            our_closing_balance=our_closing_balance,
            our_last_entry_date=our_last_entry_date_for_reco,
            our_last_entry_fy=our_last_entry_fy_for_reco,
        )

    # ---------------- Raw PDF ----------------
    if party_pages:
        with st.expander("🔎 Show Raw Party PDF Text"):
            for page in party_pages:
                st.markdown(f"**Page {page['Page']}**")
                st.text(page["Text"])

    # ---------------- Settings / Status ----------------
    st.divider()
    st.subheader("⚙️ Reconciliation Settings")
    c1, c2 = st.columns(2)
    c1.info(f"📅 Date tolerance: {date_tolerance} days")
    c2.info(f"💰 Amount tolerance: {amount_tolerance:.2f}")

    st.divider()
    st.success(
        f"✅ Currently displaying {selected_fy}. "
        "Changing the Financial Year changes both ledger entries, "
        "both summaries, and transaction matching."
    )
else:
    st.info("👆 Upload both ledgers and click **READ LEDGERS** to start.")