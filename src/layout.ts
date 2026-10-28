// Sorts the element list returned by /api/run into the sections of the dashboard.
// The backend output is unchanged: anything not recognised here is still shown
// (under "More details"), so a new backend message never disappears.
import type { UIElement } from './types';

export type El<T extends UIElement['type']> = Extract<UIElement, { type: T }>;

export interface Metric {
  label: string;
  value: string;
}

export interface Balance {
  label: string;
  value: string;
  kind: El<'alert'>['kind'];
}

export interface Summary {
  metrics: Metric[];
  balances: Balance[];
  notes: UIElement[];
}

export interface TableTab {
  id: string;
  label: string;
  count: number | null;
  table: El<'dataframe'> | null;
  notes: UIElement[];
}

export interface Matching {
  title: string;
  metrics: Metric[];
  notes: UIElement[];
  banner: El<'alert'> | null;
}

export interface Exports {
  highlightButton: El<'button'> | null;
  downloads: El<'download_button'>[];
  notes: UIElement[];
}

export interface PageModel {
  intro: UIElement[];
  fy: { select: El<'selectbox'>; notes: UIElement[] } | null;
  partyName: string | null;
  ourSummary: Summary | null;
  partySummary: Summary | null;
  matching: Matching | null;
  tabs: TableTab[];
  exports: Exports;
  other: UIElement[];
}

interface Section {
  title: string;
  elements: UIElement[];
}

const isHeading = (el: UIElement): el is El<'header'> | El<'subheader'> =>
  el.type === 'header' || el.type === 'subheader';

/** Heading text without the leading emoji. */
export function cleanTitle(text: string): string {
  return text.replace(/^[^\p{L}\p{N}]+/u, '').trim();
}

function countIn(text: string): number | null {
  const m = text.match(/\(([\d,]+)\)\s*$/);
  return m ? Number(m[1].replace(/,/g, '')) : null;
}

function withoutCount(text: string): string {
  return cleanTitle(text).replace(/\s*\([\d,]+\)\s*$/, '');
}

/** Split at dividers; a chunk that does not start with a heading belongs to the previous one. */
function splitSections(elements: UIElement[]): { intro: UIElement[]; sections: Section[] } {
  const intro: UIElement[] = [];
  const sections: Section[] = [];
  let current: UIElement[] = [];
  let started = false;

  const flush = () => {
    if (!started) {
      intro.push(...current);
    } else if (current.length) {
      const first = current[0];
      if (isHeading(first) || !sections.length) {
        sections.push({ title: isHeading(first) ? first.body : '', elements: current });
      } else {
        sections[sections.length - 1].elements.push(...current);
      }
    }
    current = [];
  };

  for (const el of elements) {
    if (el.type === 'divider') {
      flush();
      started = true;
    } else {
      current.push(el);
    }
  }
  flush();
  return { intro, sections };
}

/** Columns are layout only: list their contents in order. Expanders are kept as they are. */
function flatten(elements: UIElement[]): UIElement[] {
  return elements.flatMap((el) => (el.type === 'columns' ? flatten(el.columns.flat()) : [el]));
}

function balanceOf(el: UIElement): Balance | null {
  if (el.type !== 'alert') return null;
  const m = el.body.match(/^(.*?)\n\n###\s*(.+)$/s);
  return m ? { label: cleanTitle(m[1]), value: m[2].trim(), kind: el.kind } : null;
}

function summaryOf(elements: UIElement[]): Summary {
  const summary: Summary = { metrics: [], balances: [], notes: [] };
  for (const el of flatten(elements)) {
    if (isHeading(el)) continue;
    const balance = balanceOf(el);
    if (el.type === 'metric') summary.metrics.push({ label: el.label, value: el.value });
    else if (balance) summary.balances.push(balance);
    else if (!(el.type === 'markdown' && /Financial Year:/.test(el.body))) summary.notes.push(el);
  }
  return summary;
}

function entriesTab(id: string, label: string, elements: UIElement[]): TableTab {
  const tab: TableTab = { id, label, count: null, table: null, notes: [] };
  for (const el of flatten(elements)) {
    if (isHeading(el)) continue;
    if (el.type === 'dataframe') {
      tab.table = el;
      tab.count = el.rows.length;
    } else {
      tab.notes.push(el);
    }
  }
  return tab;
}

// Status messages the backend still sends but the page does not show.
// Delete a line to show that message again. Warnings and errors are never hidden.
const HIDDEN_MESSAGES: RegExp[] = [
  /Our Ledger sheets found/, // 📘 Our Ledger sheets found: SAPUI5 Export, Reco
  /Our Ledger PDF detected/, // 📘 Our Ledger PDF detected: standard Debit / Credit / TDS format
  /Universal Party Ledger normalized successfully/, // ✅ Universal Party Ledger normalized successfully.
  /is available in BOTH Our Ledger and Party Ledger/, // ✅ FY 2025-26 is available in BOTH ...
  /transactions found for/, // ✅ 31 Our Ledger transactions found for FY 2025-26.
  /Universal parser: .* normalized/, // 🔄 Universal parser: 27 transactions normalized ...
  /RECO My Books = Our SAP Ledger/, // 📌 RECO My Books = Our SAP Ledger calculated FY closing: ₹… as at …
  /highlighted PDF\. Excel → highlighted Excel/, // PDF → highlighted PDF. Excel → highlighted Excel (matched amount cells only).
  /^(Our|Party) Ledger: \d+ amounts highlighted$/, // Our Ledger: 7 amounts highlighted
];

const isHidden = (el: UIElement) =>
  ((el.type === 'alert' && (el.kind === 'success' || el.kind === 'info')) || el.type === 'caption') &&
  HIDDEN_MESSAGES.some((re) => re.test(el.body));

export function buildPage(allElements: UIElement[]): PageModel {
  const elements = allElements.filter((el) => !isHidden(el));
  const { intro, sections } = splitSections(elements);
  const page: PageModel = {
    intro,
    fy: null,
    partyName: null,
    ourSummary: null,
    partySummary: null,
    matching: null,
    tabs: [],
    exports: { highlightButton: null, downloads: [], notes: [] },
    other: [],
  };
  const matchTabs: TableTab[] = [];
  const entryTabs: TableTab[] = [];

  for (const section of sections) {
    const title = section.title;
    const select = section.elements.find((el): el is El<'selectbox'> => el.type === 'selectbox');

    if (select && select.key === 'selected_fy') {
      page.fy = { select, notes: section.elements.filter((el) => el !== select && !isHeading(el)) };
    } else if (/Our Ledger Summary/.test(title)) {
      page.ourSummary = summaryOf(section.elements);
    } else if (/Party Ledger Summary/.test(title)) {
      page.partySummary = summaryOf(section.elements);
    } else if (/Our Ledger Entries/.test(title)) {
      entryTabs.push(entriesTab('our-entries', 'Our ledger entries', section.elements));
    } else if (/Party Ledger Entries/.test(title)) {
      const tab = entriesTab('party-entries', 'Party ledger entries', section.elements);
      tab.notes = tab.notes.filter((el) => {
        const m = el.type === 'caption' && el.body.match(/^Party Ledger:\s*\*\*(.+)\*\*$/);
        if (m) page.partyName = m[1];
        return !m;
      });
      entryTabs.push(tab);
    } else if (/Transaction Matching Engine/.test(title)) {
      const matching: Matching = { title: cleanTitle(title), metrics: [], notes: [], banner: null };
      let tab: TableTab | null = null;
      let exporting = false;
      for (const el of flatten(section.elements)) {
        if (el.type === 'header') continue;
        if (el.type === 'subheader') {
          exporting = /Highlighted Ledgers/.test(el.body);
          tab = null;
          if (!exporting) {
            const id = /Unmatched Our/.test(el.body)
              ? 'unmatched-our'
              : /Unmatched Party/.test(el.body)
                ? 'unmatched-party'
                : /Invoice No\. Mismatch/.test(el.body)
                  ? 'doc-mismatch'
                  : 'matched';
            tab = { id, label: withoutCount(el.body), count: countIn(el.body), table: null, notes: [] };
            matchTabs.push(tab);
          }
        } else if (el.type === 'button') {
          page.exports.highlightButton = el;
        } else if (el.type === 'download_button') {
          page.exports.downloads.push(el);
        } else if (exporting) {
          page.exports.notes.push(el);
        } else if (el.type === 'metric') {
          matching.metrics.push({ label: el.label, value: el.value });
        } else if (el.type === 'dataframe' && tab) {
          tab.table = el;
        } else if (el.type === 'alert' && /Matching engine completed/.test(el.body)) {
          matching.banner = el;
        } else if (el.type === 'alert' && /No transactions matched/.test(el.body)) {
          const matched: TableTab = { id: 'matched', label: 'Matched transactions', count: 0, table: null, notes: [el] };
          matchTabs.push(matched);
        } else if (tab) {
          tab.notes.push(el);
        } else {
          matching.notes.push(el);
        }
      }
      page.matching = matching;
    } else if (/Reconciliation Statement/.test(title)) {
      for (const el of section.elements) {
        if (el.type === 'download_button') page.exports.downloads.push(el);
        else if (el.type === 'expander') page.other.push(el);
        else if (el.type === 'alert') page.exports.notes.push(el);
      }
    } else if (/Reconciliation Settings/.test(title)) {
      // Shown in the settings panel already.
    } else {
      page.other.push(...section.elements);
    }
  }

  page.tabs = [...matchTabs, ...entryTabs];
  return page;
}
