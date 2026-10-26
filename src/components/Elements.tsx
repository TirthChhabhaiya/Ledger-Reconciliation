import { useState, type ReactNode } from 'react';
import type { UIElement } from '../types';
import { DataTable } from './DataTable';
import { Markdown } from './Markdown';

interface Props {
  elements: UIElement[];
  onSelect: (key: string, value: string) => void;
}

function downloadBase64(data: string, fileName: string, mime: string) {
  const bytes = Uint8Array.from(atob(data), (c) => c.charCodeAt(0));
  const url = URL.createObjectURL(new Blob([bytes], { type: mime }));
  const link = document.createElement('a');
  link.href = url;
  link.download = fileName;
  link.click();
  URL.revokeObjectURL(url);
}

function Expander({ label, children }: { label: string; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="expander">
      <button type="button" className="expander-head" onClick={() => setOpen(!open)}>
        <span>{label}</span>
        <span className="chevron">{open ? '▴' : '▾'}</span>
      </button>
      {open && <div className="expander-body">{children}</div>}
    </div>
  );
}

export function Elements({ elements, onSelect }: Props) {
  return (
    <>
      {elements.map((el, i) => {
        switch (el.type) {
          case 'header':
            return <h2 key={i} className="st-header">{el.body}</h2>;
          case 'subheader':
            return <h3 key={i} className="st-subheader">{el.body}</h3>;
          case 'divider':
            return <hr key={i} className="st-divider" />;
          case 'caption':
            return <div key={i} className="st-caption"><Markdown body={el.body} /></div>;
          case 'markdown':
            return <div key={i} className="st-markdown"><Markdown body={el.body} /></div>;
          case 'text':
            return <pre key={i} className="st-text">{el.body}</pre>;
          case 'alert':
            return <div key={i} className={`alert alert-${el.kind}`}><Markdown body={el.body} /></div>;
          case 'metric':
            return (
              <div key={i} className="metric">
                <div className="metric-label">{el.label}</div>
                <div className="metric-value">{el.value}</div>
              </div>
            );
          case 'dataframe':
            return <DataTable key={i} columns={el.columns} kinds={el.kinds} rows={el.rows} />;
          case 'columns':
            return (
              <div key={i} className="st-columns" style={{ gridTemplateColumns: `repeat(${el.columns.length}, minmax(0, 1fr))` }}>
                {el.columns.map((column, c) => (
                  <div key={c} className="st-column">
                    <Elements elements={column} onSelect={onSelect} />
                  </div>
                ))}
              </div>
            );
          case 'expander':
            return (
              <Expander key={i} label={el.label}>
                <Elements elements={el.children} onSelect={onSelect} />
              </Expander>
            );
          case 'selectbox':
            return (
              <label key={i} className="field">
                <span className="field-label">{el.label}</span>
                <select value={el.value ?? ''} onChange={(e) => onSelect(el.key, e.target.value)}>
                  {el.options.map((option) => (
                    <option key={option} value={option}>{option}</option>
                  ))}
                </select>
              </label>
            );
          case 'download_button':
            return (
              <div key={i} className="download">
                <button
                  type="button"
                  className={el.kind === 'primary' ? 'btn btn-primary' : 'btn'}
                  title={el.help ?? undefined}
                  onClick={() => downloadBase64(el.data, el.file_name, el.mime)}
                >
                  {el.label}
                </button>
              </div>
            );
        }
      })}
    </>
  );
}
