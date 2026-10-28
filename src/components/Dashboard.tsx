import { useMemo, useState, type ReactNode } from 'react';
import {
  CircleCheck,
  CircleX,
  Download,
  FileSpreadsheet,
  FileText,
  Highlighter,
  LoaderCircle,
  Percent,
  ScrollText,
  Search,
  TriangleAlert,
} from 'lucide-react';
import type { Balance, El, Exports, Matching, Metric, Summary, TableTab } from '../layout';
import type { UIElement } from '../types';
import { DataTable } from './DataTable';
import { downloadBase64, Elements } from './Elements';
import { Markdown } from './Markdown';
import { formatSize } from './Widgets';

type OnClick = (key: string) => void;

const noSelect = () => undefined;

export function Notes({ elements, onClick }: { elements: UIElement[]; onClick?: OnClick }) {
  if (!elements.length) return null;
  return <Elements elements={elements} onSelect={noSelect} onClick={onClick ?? noSelect} />;
}

export function Step({
  n,
  id,
  title,
  subtitle,
  dark,
  children,
}: {
  n: number;
  id?: string;
  title: string;
  subtitle: string;
  dark?: boolean;
  children: ReactNode;
}) {
  return (
    <section className={`step${dark ? ' dark' : ''}`} id={id}>
      <header className="step-head">
        <span className="step-num">{n}</span>
        <div>
          <h3>{title}</h3>
          <p>{subtitle}</p>
        </div>
      </header>
      <div className="step-body">{children}</div>
    </section>
  );
}

// ---------------- 2. Ledger summary ----------------
function SummaryCard({ variant, title, name, summary }: { variant: 'our' | 'party'; title: string; name?: string | null; summary: Summary | null }) {
  return (
    <div className={`summary-card ${variant}`}>
      <div className="summary-head">
        <span className="summary-icon">{variant === 'our' ? <FileText size={22} /> : <ScrollText size={22} />}</span>
        <div>
          <h4>{title}</h4>
          {name && <small>{name}</small>}
        </div>
      </div>
      {summary ? (
        <>
          <div className="summary-metrics">
            {summary.metrics.map((m) => (
              <div key={m.label}>
                <span>{m.label === 'Transactions' ? 'Total transactions' : m.label}</span>
                <strong>{m.value}</strong>
              </div>
            ))}
          </div>
          {summary.balances.length > 0 && (
            <div className="summary-balances">
              {summary.balances.map((b: Balance) => (
                <div key={b.label} className={`balance ${b.kind}`}>
                  <span>{b.label}</span>
                  <strong>{b.value}</strong>
                </div>
              ))}
            </div>
          )}
          <Notes elements={summary.notes} />
        </>
      ) : (
        <p className="muted">No summary for this Financial Year.</p>
      )}
    </div>
  );
}

export function LedgerSummary({ our, party, partyName }: { our: Summary | null; party: Summary | null; partyName: string | null }) {
  return (
    <div className="summary-grid">
      <SummaryCard variant="our" title="Our Ledger" name="Durga Infra Mining Pvt Ltd" summary={our} />
      <SummaryCard variant="party" title="Party Ledger" name={partyName} summary={party} />
    </div>
  );
}

// ---------------- 3. Matching engine ----------------
const metricValue = (metrics: Metric[], label: string) => metrics.find((m) => m.label === label)?.value;
const toNumber = (value: string | undefined) => (value ? Number(value.replace(/[^\d.]/g, '')) : NaN);

export function MatchingStats({ matching, tabs }: { matching: Matching; tabs: TableTab[] }) {
  const our = toNumber(metricValue(matching.metrics, 'Our Transactions'));
  const party = toNumber(metricValue(matching.metrics, 'Party Transactions'));
  const matched = metricValue(matching.metrics, 'Matched');
  const rate = metricValue(matching.metrics, 'Match Rate');
  const unmatchedOur = tabs.find((t) => t.id === 'unmatched-our')?.count;
  const unmatchedParty = tabs.find((t) => t.id === 'unmatched-party')?.count;
  const share = (n: number | null | undefined, total: number) =>
    n != null && total > 0 ? `${((n / total) * 100).toFixed(1)}% of ${total.toLocaleString('en-IN')}` : '';

  return (
    <>
      {matching.metrics.length > 0 && (
        <div className="stat-grid">
          <div className="stat green">
            <CircleCheck size={30} />
            <div>
              <span>Matched transactions</span>
              <strong>{matched}</strong>
              <small>{share(toNumber(matched), our)}</small>
            </div>
          </div>
          <div className="stat red">
            <CircleX size={30} />
            <div>
              <span>Unmatched (Our Ledger)</span>
              <strong>{unmatchedOur?.toLocaleString('en-IN') ?? '—'}</strong>
              <small>{share(unmatchedOur, our)}</small>
            </div>
          </div>
          <div className="stat amber">
            <TriangleAlert size={30} />
            <div>
              <span>Unmatched (Party Ledger)</span>
              <strong>{unmatchedParty?.toLocaleString('en-IN') ?? '—'}</strong>
              <small>{share(unmatchedParty, party)}</small>
            </div>
          </div>
          <div className="stat blue">
            <Percent size={30} />
            <div>
              <span>Match rate</span>
              <strong>{rate}</strong>
              <div className="progress">
                <div style={{ width: `${Math.min(100, toNumber(rate) || 0)}%` }} />
              </div>
            </div>
          </div>
        </div>
      )}
      <Notes elements={matching.notes} />
    </>
  );
}

// ---------------- 4. Results ----------------
export function ResultTabs({ tabs }: { tabs: TableTab[] }) {
  const [active, setActive] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const tab = tabs.find((t) => t.id === active) ?? tabs[0];

  const rows = useMemo(() => {
    if (!tab?.table) return [];
    const q = query.trim().toLowerCase();
    if (!q) return tab.table.rows;
    return tab.table.rows.filter((row) => row.some((v) => v !== null && String(v).toLowerCase().includes(q)));
  }, [tab, query]);

  if (!tab) return null;

  return (
    <div className="results">
      <div className="results-bar">
        <div className="tabs" role="tablist">
          {tabs.map((t) => (
            <button
              key={t.id}
              type="button"
              role="tab"
              aria-selected={t.id === tab.id}
              className={`tab${t.id === tab.id ? ' active' : ''}`}
              onClick={() => setActive(t.id)}
            >
              {t.label}
              {t.count != null && <span className="tab-count">{t.count.toLocaleString('en-IN')}</span>}
            </button>
          ))}
        </div>
        <label className="search">
          <Search size={16} />
          <input
            type="search"
            placeholder="Search by voucher no, date, amount…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </label>
      </div>
      <Notes elements={tab.notes} />
      {tab.table && (
        <>
          <DataTable key={tab.id} columns={tab.table.columns} kinds={tab.table.kinds} rows={rows} />
          <div className="results-foot">
            Showing {rows.length.toLocaleString('en-IN')} of {tab.table.rows.length.toLocaleString('en-IN')} rows
          </div>
        </>
      )}
    </div>
  );
}

// ---------------- 5. Export center ----------------
function base64Size(data: string) {
  return Math.floor((data.length * 3) / 4) - (data.endsWith('==') ? 2 : data.endsWith('=') ? 1 : 0);
}

function ExportCard({
  title,
  subtitle,
  file,
  generate,
  generating,
}: {
  title: string;
  subtitle: string;
  file: El<'download_button'> | undefined;
  generate: (() => void) | null;
  generating: boolean;
}) {
  const isPdf = file ? file.mime === 'application/pdf' : /PDF/.test(title);
  return (
    <div className="export-card">
      <span className={`export-icon ${isPdf ? 'pdf' : 'sheet'}`}>{isPdf ? <FileText size={24} /> : <FileSpreadsheet size={24} />}</span>
      <div className="export-main">
        <strong>{title}</strong>
        <span>{subtitle}</span>
        {file ? (
          <>
            <button type="button" className="btn btn-outline" onClick={() => downloadBase64(file.data, file.file_name, file.mime)}>
              <Download size={16} /> Download {isPdf ? 'PDF' : 'Excel'}
            </button>
            <small>
              {file.file_name} · {formatSize(base64Size(file.data))}
            </small>
          </>
        ) : generate ? (
          <button type="button" className="btn btn-outline" disabled={generating} onClick={generate}>
            {generating ? <LoaderCircle size={16} className="spin" /> : <Highlighter size={16} />} Generate highlighted ledgers
          </button>
        ) : (
          <small>Not available for this run.</small>
        )}
      </div>
    </div>
  );
}

export function ExportCenter({ exports, onClick, running }: { exports: Exports; onClick: OnClick; running: boolean }) {
  const find = (prefix: string) => exports.downloads.find((d) => d.file_name.startsWith(prefix));
  const generate = exports.highlightButton ? () => onClick(exports.highlightButton!.key) : null;
  const known = ['Our_Ledger_Highlighted', 'Party_Ledger_Highlighted', 'RECO_'];
  const extra = exports.downloads.filter((d) => !known.some((p) => d.file_name.startsWith(p)));

  return (
    <>
      <div className="export-grid">
        <ExportCard
          title="Our Ledger (highlighted)"
          subtitle="Matched amounts highlighted in yellow, same format as uploaded"
          file={find('Our_Ledger_Highlighted')}
          generate={generate}
          generating={running}
        />
        <ExportCard
          title="Party Ledger (highlighted)"
          subtitle="Matched amounts highlighted in yellow, same format as uploaded"
          file={find('Party_Ledger_Highlighted')}
          generate={generate}
          generating={running}
        />
        <ExportCard
          title="RECO Statement"
          subtitle="Unmatched entries in the RECO FORMAT.xlsx layout"
          file={find('RECO_')}
          generate={null}
          generating={false}
        />
      </div>
      <Notes elements={[...extra, ...exports.notes]} onClick={onClick} />
    </>
  );
}

export function Banner({ alert }: { alert: El<'alert'> }) {
  return (
    <div className="banner">
      <CircleCheck size={26} />
      <div>
        <strong>Reconciliation completed</strong>
        <Markdown body={alert.body.replace(/^✅\s*/, '')} />
      </div>
    </div>
  );
}
