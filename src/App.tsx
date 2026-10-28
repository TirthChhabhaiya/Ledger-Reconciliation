import { useMemo, useRef, useState } from 'react';
import { ArrowRight, CircleHelp, LoaderCircle, Play, Settings } from 'lucide-react';
import { runPage, UPLOAD_MODE, uploadToBlob } from './api';
import { Hero, Sidebar, Topbar } from './components/Chrome';
import { Banner, ExportCenter, LedgerSummary, MatchingStats, Notes, ResultTabs, Step } from './components/Dashboard';
import { Checkbox, FileUploader, FySelect, NumberInput, type UploadSlot } from './components/Widgets';
import { buildPage } from './layout';
import type { LoadedLedgers, UIElement, UploadedLedger } from './types';

const START_MESSAGE: UIElement[] = [
  { type: 'alert', kind: 'info', body: '👆 Upload both ledgers and click **READ LEDGERS** to start.' },
];

type SlotName = 'our' | 'party';

interface Settings {
  dateTolerance: number;
  amountTolerance: number;
  fromOurBooks: boolean;
  selectedFy: string | null;
}

export default function App() {
  const [slots, setSlots] = useState<Record<SlotName, UploadSlot | null>>({ our: null, party: null });
  const [settings, setSettings] = useState<Settings>({
    dateTolerance: 3,
    amountTolerance: 1.0,
    fromOurBooks: false,
    selectedFy: null,
  });
  const [loaded, setLoaded] = useState<LoadedLedgers | null>(null);
  const [elements, setElements] = useState<UIElement[]>(START_MESSAGE);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [uploadPct, setUploadPct] = useState<number | null>(null);

  const uploads = useRef<Record<SlotName, Promise<string | null> | null>>({ our: null, party: null });
  const runSeq = useRef(0);

  // ---------------- Uploads ----------------
  const startUpload = (name: SlotName, file: File) => {
    const update = (patch: Partial<UploadSlot>) =>
      setSlots((s) => (s[name]?.file === file ? { ...s, [name]: { ...s[name]!, ...patch } } : s));

    const promise = uploadToBlob(file, (progress) => update({ progress }))
      .then((url) => {
        update({ url, progress: 100, error: null });
        return url;
      })
      .catch((e: Error) => {
        update({ error: e.message });
        return null;
      });
    uploads.current[name] = promise;
  };

  const ensureUploaded = async (name: SlotName, slot: UploadSlot | null): Promise<UploadedLedger | null> => {
    if (!slot) return null;
    if (UPLOAD_MODE === 'direct' || slot.url) return { file: slot.file, url: slot.url };
    if (slot.error) startUpload(name, slot.file);
    const url = await uploads.current[name];
    if (!url) throw new Error(`Upload of ${slot.file.name} failed. Please choose the file again.`);
    return { file: slot.file, url };
  };

  // ---------------- Reruns ----------------
  const rerun = async (next: Settings, read = false, clicked: string | null = null) => {
    // Without loaded ledgers a rerun always shows the start message (as in Streamlit).
    if (!read && !loaded) {
      setElements(START_MESSAGE);
      setError(null);
      return;
    }

    const seq = ++runSeq.current;
    setRunning(true);
    setError(null);
    try {
      const our = read ? await ensureUploaded('our', slots.our) : null;
      const party = read ? await ensureUploaded('party', slots.party) : null;

      // Direct mode sends the files with the READ LEDGERS request: show its real upload progress.
      const sendsFiles = read && UPLOAD_MODE === 'direct' && our && party;
      const onUploadProgress = sendsFiles
        ? (percent: number) => {
            if (seq !== runSeq.current) return;
            setUploadPct(percent);
            setSlots((s) => ({
              our: s.our && { ...s.our, progress: percent },
              party: s.party && { ...s.party, progress: percent },
            }));
          }
        : undefined;
      onUploadProgress?.(0);

      const result = await runPage(
        {
          read,
          our,
          party,
          loaded,
          date_tolerance: next.dateTolerance,
          amount_tolerance: next.amountTolerance,
          party_excel_from_our_books: next.fromOurBooks,
          selected_fy: next.selectedFy,
          clicked,
        },
        onUploadProgress,
      );
      if (seq !== runSeq.current) return;

      if (result.read_succeeded === true && our && party) {
        setLoaded({ our, party, party_excel_from_our_books: next.fromOurBooks });
      } else if (result.read_succeeded === false) {
        setLoaded(null);
      }
      setSettings({ ...next, selectedFy: result.selected_fy });
      setElements(result.elements);
    } catch (e) {
      if (seq === runSeq.current) setError((e as Error).message);
    } finally {
      if (seq === runSeq.current) {
        setRunning(false);
        setUploadPct(null);
      }
    }
  };

  const changeSettings = (patch: Partial<Settings>) => {
    const next = { ...settings, ...patch };
    setSettings(next);
    void rerun(next);
  };

  const changeFile = (name: SlotName, file: File | null) => {
    // Direct mode: the file is ready at once (it is sent with READ LEDGERS). Blob mode: uploaded now.
    const progress = UPLOAD_MODE === 'direct' ? 100 : 0;
    setSlots((s) => ({ ...s, [name]: file ? { file, url: null, progress, error: null } : null }));
    uploads.current[name] = null;
    if (file && UPLOAD_MODE === 'blob') startUpload(name, file);
    void rerun(settings);
  };

  const page = useMemo(() => buildPage(elements), [elements]);
  const onClick = (key: string) => void rerun(settings, false, key);
  const loadedLedgers = page.matching !== null || page.fy !== null;

  return (
    <div className="shell" id="top">
      <Sidebar />
      <div className="main">
        <Topbar partyName={page.partyName} />
        {running && (
          <div className="running">
            <LoaderCircle size={16} className="spin" /> Working…
          </div>
        )}

        <div className="content">
          <Hero />

          {/* ---------------- 1. UPLOAD & SETTINGS ---------------- */}
          <Step
            n={1}
            id="step-upload"
            title="Upload Ledgers & Settings"
            subtitle="Upload your Our Ledger and Party Ledger files. Configure matching settings before running reconciliation."
          >
            <div className="upload-grid">
              <FileUploader
                title="Our Ledger"
                subtitle="Upload Excel or PDF file"
                variant="our"
                types={['xlsx', 'xls', 'pdf']}
                slot={slots.our}
                onChange={(file) => changeFile('our', file)}
              />
              <FileUploader
                title="Party / Supplier Ledger"
                subtitle="Upload PDF, Excel or CSV file"
                variant="party"
                types={['pdf', 'xlsx', 'xls', 'csv']}
                slot={slots.party}
                onChange={(file) => changeFile('party', file)}
              />
              <div className="settings-card">
                <div className="settings-head">
                  <Settings size={18} /> Matching Settings
                </div>
                <FySelect
                  options={page.fy?.select.options ?? null}
                  value={page.fy?.select.value ?? null}
                  onChange={(selectedFy) => changeSettings({ selectedFy })}
                />
                <NumberInput
                  label="Date tolerance (days)"
                  value={settings.dateTolerance}
                  min={0}
                  max={365}
                  step={1}
                  decimals={0}
                  onChange={(dateTolerance) => changeSettings({ dateTolerance })}
                />
                <NumberInput
                  label="Amount tolerance (₹)"
                  value={settings.amountTolerance}
                  min={0}
                  step={0.01}
                  decimals={2}
                  help="Transactions can differ by up to ₹1.00 and still match. Example: ₹19,460 can match ₹19,459 or ₹19,461."
                  onChange={(amountTolerance) => changeSettings({ amountTolerance })}
                />
                <Checkbox
                  label="Party Excel is from OUR books (vendor view) - invert sign"
                  checked={settings.fromOurBooks}
                  help="Tick when the Party Excel is a SAP vendor ledger from our own system (invoice = negative). Leave off if it comes from the party's own books."
                  onChange={(fromOurBooks) => changeSettings({ fromOurBooks })}
                />
                <button
                  type="button"
                  className="btn btn-primary btn-block"
                  disabled={running}
                  onClick={() => void rerun(settings, true)}
                >
                  {running ? <LoaderCircle size={18} className="spin" /> : <Play size={18} />}
                  {uploadPct !== null
                    ? uploadPct < 100
                      ? `Uploading ${Math.floor(uploadPct)}%`
                      : 'Reading ledgers…'
                    : running
                      ? 'Working…'
                      : 'Read Ledgers'}
                  {!running && <ArrowRight size={18} />}
                </button>
              </div>
            </div>
            {error && <div className="alert alert-error">❌ Server error: {error}</div>}
            <Notes elements={page.intro} onClick={onClick} />
          </Step>

          <div className={`output${running ? ' stale' : ''}`}>
            {loadedLedgers && (
              <>
                {/* ---------------- 2. LEDGER SUMMARY ---------------- */}
                <Step
                  n={2}
                  id="step-summary"
                  title="Ledger Summary"
                  subtitle={`Summary of both ledgers for ${page.fy?.select.value ?? 'the selected Financial Year'}.`}
                >
                  {page.fy && <Notes elements={page.fy.notes} />}
                  <LedgerSummary our={page.ourSummary} party={page.partySummary} partyName={page.partyName} />
                </Step>

                {/* ---------------- 3. MATCHING ENGINE ---------------- */}
                {page.matching && (
                  <Step
                    n={3}
                    dark
                    id="step-matching"
                    title="Transaction Matching Engine"
                    subtitle="Transactions compared and reconciled between both ledgers."
                  >
                    <MatchingStats matching={page.matching} tabs={page.tabs} />
                  </Step>
                )}

                {/* ---------------- 4. RESULTS ---------------- */}
                {page.tabs.length > 0 && (
                  <Step
                    n={4}
                    id="step-results"
                    title="Transaction Results"
                    subtitle="View matched and unmatched transactions. Search to find specific entries."
                  >
                    <ResultTabs tabs={page.tabs} />
                  </Step>
                )}

                {/* ---------------- 5. EXPORT CENTER ---------------- */}
                {(page.exports.downloads.length > 0 || page.exports.highlightButton) && (
                  <Step
                    n={5}
                    id="step-export"
                    title="Export Center"
                    subtitle="Download highlighted ledgers and the reconciliation statement."
                  >
                    <ExportCenter exports={page.exports} onClick={onClick} running={running} />
                    {page.matching?.banner && <Banner alert={page.matching.banner} />}
                  </Step>
                )}

              </>
            )}
          </div>

          <section className="help" id="help">
            <h3>
              <CircleHelp size={20} /> How it works
            </h3>
            <ol>
              <li>Upload Our Ledger (SAP Excel or PDF) and the Party Ledger (PDF, Excel or CSV).</li>
              <li>Check the date and amount tolerance, then click Read Ledgers.</li>
              <li>Pick the Financial Year at the top. Summary, matching and results update for that year.</li>
              <li>Download the RECO statement, or generate the highlighted ledgers in the Export Center.</li>
            </ol>
          </section>
        </div>
      </div>
    </div>
  );
}
