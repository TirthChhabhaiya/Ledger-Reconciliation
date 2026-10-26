import { useEffect, useRef, useState } from 'react';
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
  label,
  types,
  slot,
  onChange,
}: {
  label: string;
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

  return (
    <div className="field">
      <span className="field-label">{label}</span>
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
        <div className="dropzone-text">
          <div>Drag and drop file here</div>
          <small>Limit 1GB per file • {types.map((t) => t.toUpperCase()).join(', ')}</small>
        </div>
        <button type="button" className="btn" onClick={() => input.current?.click()}>
          Browse files
        </button>
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
          <span className="file-icon">📄</span>
          <span className="file-name">{slot.file.name}</span>
          <span className="file-size">
            {slot.error
              ? 'upload failed'
              : slot.url || slot.progress >= 100
                ? formatSize(slot.file.size)
                : `uploading ${Math.floor(slot.progress)}%`}
          </span>
          <button type="button" className="file-remove" aria-label="Remove file" onClick={() => onChange(null)}>
            ✕
          </button>
        </div>
      )}
      {slot?.error && <div className="alert alert-error">❌ Upload failed: {slot.error}</div>}
    </div>
  );
}

function formatSize(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)}MB`;
  return `${(bytes / 1024).toFixed(1)}KB`;
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
