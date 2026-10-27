"""
Records the Streamlit calls made by the UI flow as a JSON element list that the
React frontend renders in the same order and layout.

Method names and arguments mirror the `st.*` calls used in app.py, so
api/_pipeline.py reads line-for-line like the original UI section.
"""
import base64
import datetime
import math

import numpy as np
import pandas as pd


def _cell(value):
    if value is None:
        return None
    if isinstance(value, (pd.Timestamp, datetime.datetime)):
        return None if pd.isna(value) else str(value)
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        value = float(value)
        if math.isnan(value):
            return None
        return value if math.isfinite(value) else str(value)
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return str(value)


def _column_kind(series):
    if pd.api.types.is_bool_dtype(series):
        return "bool"
    if pd.api.types.is_numeric_dtype(series):
        return "number"
    if pd.api.types.is_datetime64_any_dtype(series):
        return "datetime"
    return "text"


def dataframe_payload(df):
    return {
        "columns": [str(c) for c in df.columns],
        "kinds": [_column_kind(df.iloc[:, i]) for i in range(df.shape[1])],
        "rows": [[_cell(v) for v in row] for row in df.itertuples(index=False, name=None)],
    }


class Container:
    def __init__(self, ui, elements):
        self._ui = ui
        self.elements = elements

    def __enter__(self):
        self._ui._stack.append(self)
        return self

    def __exit__(self, *exc):
        self._ui._stack.pop()

    def _add(self, element):
        self.elements.append(element)

    def header(self, body):
        self._add({"type": "header", "body": body})

    def subheader(self, body):
        self._add({"type": "subheader", "body": body})

    def divider(self):
        self._add({"type": "divider"})

    def caption(self, body):
        self._add({"type": "caption", "body": body})

    def write(self, body):
        self._add({"type": "markdown", "body": body})

    def markdown(self, body):
        self._add({"type": "markdown", "body": body})

    def text(self, body):
        self._add({"type": "text", "body": body})

    def success(self, body):
        self._add({"type": "alert", "kind": "success", "body": body})

    def info(self, body):
        self._add({"type": "alert", "kind": "info", "body": body})

    def warning(self, body):
        self._add({"type": "alert", "kind": "warning", "body": body})

    def error(self, body):
        self._add({"type": "alert", "kind": "error", "body": body})

    def metric(self, label, value):
        self._add({"type": "metric", "label": label, "value": value})

    def dataframe(self, data, use_container_width=True, hide_index=True):
        self._add({"type": "dataframe", **dataframe_payload(data)})

    def columns(self, count):
        columns = [Container(self._ui, []) for _ in range(count)]
        self._add({"type": "columns", "columns": [c.elements for c in columns]})
        return columns

    def expander(self, label):
        children = Container(self._ui, [])
        self._add({"type": "expander", "label": label, "children": children.elements})
        return children

    def selectbox(self, label, options, value, key):
        self._add({"type": "selectbox", "label": label, "options": list(options), "value": value, "key": key})
        return value

    def button(self, label, key):
        """True only on the rerun the button was clicked (as in Streamlit)."""
        self._add({"type": "button", "label": label, "key": key})
        return key == self._ui.clicked

    def download_button(self, label, data, file_name, mime, type="secondary", help=None, key=None):
        self._add({
            "type": "download_button",
            "label": label,
            "data": base64.b64encode(data).decode("ascii"),
            "file_name": file_name,
            "mime": mime,
            "kind": type,
            "help": help,
        })


class UI(Container):
    """Root container; `with column:` blocks redirect output like Streamlit."""

    def __init__(self, clicked=None):
        super().__init__(self, [])
        self._stack = [self]
        self.clicked = clicked  # key of the button clicked on this rerun

    def _add(self, element):
        target = self._stack[-1]
        if target is self:
            self.elements.append(element)
        else:
            target._add(element)
