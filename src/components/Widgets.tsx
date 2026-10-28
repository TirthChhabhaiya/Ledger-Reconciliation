import { useEffect, useRef, useState } from 'react';
import {
  CircleCheck,
  CircleX,
  CloudUpload,
  FileSpreadsheet,
  FileText,
  LoaderCircle,
  ScrollText,
  X,
} from 'lucide-react';
import { fileExtension } from '../api';

export interface UploadSlot {
  file: File;
  url: string | null;
  progress: number;
  error: string | null;
}

function Help({ text }: { text: string }) {
  return (
    <span className="help" title={text} aria-label={text}>
      ?
    </span>
  );
}

// st.file_uploader equivalent: drag & drop or browse, one file, extension filter.
export function FileUploader({
  title,
  subtitle,
  variant,
  types,
  slot,
  onChange,
}: {
  title: string;
  subtitle: string;
  variant: 'our' | 'party';
  types: string[];
  slot: UploadSlot | null;
  onChange: (file: File | null) => void;
}) {
  const input = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const [rejected, setRejected] = useState<string | null>(null);

  const choose = (file: File | undefined) => {
    if (!file) return;
    if (!types.includes(fileExtension(file.name))) {
      setRejected(`${file.name}: file type .${fileExtension(file.name)} is not allowed.`);
      return;
    }
    setRejected(null);
    onChange(file);
  };

  const uploading = slot && !slot.error && !slot.url && slot.progress < 100;
  const ext = slot ? fileExtension(slot.file.name) : '';

  return (
    <div className={`upload-card ${variant}`}>
      <div className="upload-head">
        <span className="upload-icon">{variant === 'our' ? <FileText size={22} /> : <ScrollText size={22} />}</span>
        <div>
          <div className="upload-title">{title}</div>
          <div className="upload-sub">{subtitle}</div>
        </div>
      </div>
      <div
        className={`dropzone${dragging ? ' dragging' : ''}`}
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          choose(e.dataTransfer.files[0]);
        }}
      >
        <CloudUpload size={26} />
        <div>Drag &amp; drop file here</div>
        <div className="dropzone-or">or</div>
        <button type="button" className="btn btn-choose" onClick={() => input.current?.click()}>
          Choose File
        </button>
        <small>{types.map((t) => t.toUpperCase()).join(', ')}</small>
        <input
          ref={input}
          type="file"
          hidden
          accept={types.map((t) => `.${t}`).join(',')}
          onChange={(e) => {
            choose(e.target.files?.[0]);
            e.target.value = '';
          }}
        />
      </div>
      {rejected && <div className="alert alert-error">{rejected}</div>}
      {slot && (
        <div className="file-chip">
          <span className={`file-icon ${ext === 'pdf' ? 'pdf' : 'sheet'}`}>
            {ext === 'pdf' ? <FileText size={20} /> : <FileSpreadsheet size={20} />}
          </span>
          <span className="file-meta">
            <span className="file-name" title={slot.file.name}>{slot.file.name}</span>
            <span className="file-size">
              {slot.error
                ? 'upload failed'
                : uploading
                  ? `uploading ${Math.floor(slot.progress)}%`
                  : formatSize(slot.file.size)}
            </span>
          </span>
          {uploading ? (
            <LoaderCircle size={20} className="spin file-state" />
          ) : slot.error ? (
            <CircleX size={20} className="file-state bad" />
          ) : (
            <CircleCheck size={20} className="file-state ok" />
          )}
          <button type="button" className="file-remove" aria-label="Remove file" onClick={() => onChange(null)}>
            <X size={18} />
          </button>
        </div>
      )}
      {slot?.error && <div className="alert alert-error">❌ Upload failed: {slot.error}</div>}
    </div>
  );
}

export function formatSize(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / 1024).toFixed(1)} KB`;
}

// st.number_input equivalent: commits on Enter / blur, with − / + step buttons.
export function NumberInput({
  label,
  value,
  min,
  max,
  step,
  decimals,
  help,
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max?: number;
  step: number;
  decimals: number;
  help?: string;
  onChange: (value: number) => void;
}) {
  const [draft, setDraft] = useState(value.toFixed(decimals));
  useEffect(() => setDraft(value.toFixed(decimals)), [value, decimals]);

  const clamp = (n: number) => Math.min(max ?? Infinity, Math.max(min, n));
  const commit = (n: number) => {
    const next = Number(clamp(n).toFixed(decimals));
    setDraft(next.toFixed(decimals));
    if (next !== value) onChange(next);
  };

  return (
    <label className="field">
      <span className="field-label">
        {label} {help && <Help text={help} />}
      </span>
      <div className="number-input">
        <input
          type="number"
          value={draft}
          min={min}
          max={max}
          step={step}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={() => (draft.trim() === '' || Number.isNaN(Number(draft)) ? setDraft(value.toFixed(decimals)) : commit(Number(draft)))}
          onKeyDown={(e) => e.key === 'Enter' && (e.target as HTMLInputElement).blur()}
        />
        <button type="button" onClick={() => commit(value - step)} disabled={value - step < min}>
          −
        </button>
        <button type="button" onClick={() => commit(value + step)} disabled={max !== undefined && value + step > max}>
          +
        </button>
      </div>
    </label>
  );
}

// Financial Year picker (the st.selectbox sent by the backend); disabled until ledgers are read.
export function FySelect({
  options,
  value,
  onChange,
}: {
  options: string[] | null;
  value: string | null;
  onChange: (value: string) => void;
}) {
  return (
    <label className="field">
      <span className="field-label">Financial Year</span>
      <select value={value ?? ''} disabled={!options} onChange={(e) => onChange(e.target.value)}>
        {options ? (
          options.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))
        ) : (
          <option value="">Read ledgers to choose</option>
        )}
      </select>
    </label>
  );
}

export function Checkbox({
  label,
  checked,
  help,
  onChange,
}: {
  label: string;
  checked: boolean;
  help?: string;
  onChange: (checked: boolean) => void;
}) {
  return (
    <label className="checkbox">
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      <span>{label}</span>
      {help && <Help text={help} />}
    </label>
  );
}
