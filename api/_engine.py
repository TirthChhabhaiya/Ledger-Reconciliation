"""
Loads the reconciliation logic directly from the original app.py.

Nothing is copied or rewritten: the source of app.py from the first line up to
`def display_matching_engine(` (every parser, FY summary, matching and Excel
export function) is compiled and executed as-is. Only the Streamlit UI section
after that point is replaced by api/_pipeline.py.

The logic section touches Streamlit in exactly three places:
  * st.set_page_config(...)                              -> no-op here
  * st.session_state.get("party_excel_from_our_books")   -> per-request value
  * st.session_state.get("amount_tolerance")             -> per-request value
so `streamlit` is replaced with a tiny stand-in module while app.py executes.
"""
import sys
import types
from contextvars import ContextVar
from pathlib import Path

APP_PATH = Path(__file__).resolve().parent.parent / "app.py"
UI_SECTION_MARKER = "\ndef display_matching_engine("

# app.py imports highlight_export.py, which sits beside it.
if str(APP_PATH.parent) not in sys.path:
    sys.path.insert(0, str(APP_PATH.parent))

_session_state = ContextVar("session_state")


class _SessionState:
    """Dict-like view of the current request's session values."""

    def _data(self):
        return _session_state.get()

    def get(self, key, default=None):
        return self._data().get(key, default)

    def __getitem__(self, key):
        return self._data()[key]

    def __setitem__(self, key, value):
        self._data()[key] = value

    def __contains__(self, key):
        return key in self._data()


def use_session_state(values):
    """Bind `values` as st.session_state for the code running in this context."""
    _session_state.set(values)


def _streamlit_stand_in():
    module = types.ModuleType("streamlit")
    module.session_state = _SessionState()
    module.set_page_config = lambda *args, **kwargs: None
    return module


def _load_engine():
    source = APP_PATH.read_text(encoding="utf-8")
    cut = source.index(UI_SECTION_MARKER)

    engine = types.ModuleType("reco_engine")
    engine.__file__ = str(APP_PATH)  # RECO FORMAT.xlsx is resolved beside app.py

    real_streamlit = sys.modules.get("streamlit")
    sys.modules["streamlit"] = _streamlit_stand_in()
    try:
        exec(compile(source[:cut], str(APP_PATH), "exec"), engine.__dict__)
    finally:
        if real_streamlit is None:
            sys.modules.pop("streamlit", None)
        else:
            sys.modules["streamlit"] = real_streamlit

    return engine


engine = _load_engine()
