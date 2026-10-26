// Element list returned by POST /api/run (recorded by api/_ui.py).

export type AlertKind = 'success' | 'info' | 'warning' | 'error';
export type CellValue = string | number | boolean | null;
export type ColumnKind = 'number' | 'datetime' | 'bool' | 'text';

export type UIElement =
  | { type: 'header'; body: string }
  | { type: 'subheader'; body: string }
  | { type: 'divider' }
  | { type: 'caption'; body: string }
  | { type: 'markdown'; body: string }
  | { type: 'text'; body: string }
  | { type: 'alert'; kind: AlertKind; body: string }
  | { type: 'metric'; label: string; value: string }
  | { type: 'dataframe'; columns: string[]; kinds: ColumnKind[]; rows: CellValue[][] }
  | { type: 'columns'; columns: UIElement[][] }
  | { type: 'expander'; label: string; children: UIElement[] }
  | { type: 'selectbox'; label: string; options: string[]; value: string | null; key: string }
  | {
      type: 'download_button';
      label: string;
      data: string;
      file_name: string;
      mime: string;
      kind: 'primary' | 'secondary';
      help: string | null;
    };

export interface RunResult {
  elements: UIElement[];
  ledgers_loaded: boolean;
  read_succeeded: boolean | null;
  selected_fy: string | null;
}

/** A file sent to the backend: stored in Vercel Blob (url) or sent inline (part). */
export interface FileRef {
  name: string;
  url?: string;
  part?: string;
}

/** A ledger chosen in an uploader; url is set once it is stored in Vercel Blob. */
export interface UploadedLedger {
  file: File;
  url: string | null;
}

/** Files read by the last successful READ LEDGERS click, with the checkbox value at that click. */
export interface LoadedLedgers {
  our: UploadedLedger;
  party: UploadedLedger;
  party_excel_from_our_books: boolean;
}
