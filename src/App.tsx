import { useRef, useState } from 'react';
import { runPage, UPLOAD_MODE, uploadToBlob } from './api';
import { Elements } from './components/Elements';
import { Checkbox, FileUploader, NumberInput, type UploadSlot } from './components/Widgets';
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

      const result = await runPage({
        read,
        our,
        party,
        loaded,
        date_tolerance: next.dateTolerance,
        amount_tolerance: next.amountTolerance,
        party_excel_from_our_books: next.fromOurBooks,
        selected_fy: next.selectedFy,
        clicked,
      });
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
      if (seq === runSeq.current) setRunning(false);
    }
  };

  const changeSettings = (patch: Partial<Settings>) => {
    const next = { ...settings, ...patch };
    setSettings(next);
    void rerun(next);
  };

  const changeFile = (name: SlotName, file: File | null) => {
    setSlots((s) => ({ ...s, [name]: file ? { file, url: null, progress: 0, error: null } : null }));
    uploads.current[name] = null;
    if (file && UPLOAD_MODE === 'blob') startUpload(name, file);
    void rerun(settings);
  };

  return (
    <main className="page">
      {running && <div className="running">Running…</div>}

      {/* ---------------- FILE UPLOAD ---------------- */}
      <div className="st-columns two">
        <div className="st-column">
          <h3 className="st-subheader">📘 Our Ledger</h3>
          <FileUploader
            label="Upload Our Ledger Excel / PDF File"
            types={['xlsx', 'xls', 'pdf']}
            slot={slots.our}
            onChange={(file) => changeFile('our', file)}
          />
          {slots.our && (
            <>
              <div className="alert alert-success">✅ Uploaded: {slots.our.file.name}</div>
              <div className="st-caption">File size: {(slots.our.file.size / (1024 * 1024)).toFixed(2)} MB</div>
            </>
          )}
        </div>
        <div className="st-column">
          <h3 className="st-subheader">📕 Party / Supplier Ledger</h3>
          <FileUploader
            label="Upload Party Ledger"
            types={['pdf', 'xlsx', 'xls', 'csv']}
            slot={slots.party}
            onChange={(file) => changeFile('party', file)}
          />
          {slots.party && (
            <>
              <div className="alert alert-success">✅ Uploaded: {slots.party.file.name}</div>
              <div className="st-caption">
                File size: {(slots.party.file.size / (1024 * 1024)).toFixed(2)} MB • Universal reader: PDF / XLSX / XLS / CSV
              </div>
            </>
          )}
        </div>
      </div>

      <hr className="st-divider" />

      {/* ---------------- MATCHING SETTINGS ---------------- */}
      <h3 className="st-subheader">⚙️ Matching Settings</h3>
      <div className="st-columns two">
        <div className="st-column">
          <NumberInput
            label="Date tolerance (days)"
            value={settings.dateTolerance}
            min={0}
            max={365}
            step={1}
            decimals={0}
            onChange={(dateTolerance) => changeSettings({ dateTolerance })}
          />
        </div>
        <div className="st-column">
          <NumberInput
            label="Amount tolerance (₹)"
            value={settings.amountTolerance}
            min={0}
            step={0.01}
            decimals={2}
            help="Transactions can differ by up to ₹1.00 and still match. Example: ₹19,460 can match ₹19,459 or ₹19,461."
            onChange={(amountTolerance) => changeSettings({ amountTolerance })}
          />
        </div>
      </div>

      <Checkbox
        label="Party Excel is exported from OUR books (vendor view) - invert sign"
        checked={settings.fromOurBooks}
        help="Tick when the Party Excel is a SAP vendor ledger from our own system (invoice = negative). Leave off if it comes from the party's own books."
        onChange={(fromOurBooks) => changeSettings({ fromOurBooks })}
      />

      <hr className="st-divider" />

      {/* ---------------- READ LEDGERS ---------------- */}
      <button type="button" className="btn btn-primary" disabled={running} onClick={() => void rerun(settings, true)}>
        ▶️ READ LEDGERS
      </button>

      {/* ---------------- DISPLAY ---------------- */}
      <section className={`output${running ? ' stale' : ''}`}>
        {error && <div className="alert alert-error">❌ Server error: {error}</div>}
        <Elements
          elements={elements}
          onSelect={(key, value) => key === 'selected_fy' && changeSettings({ selectedFy: value })}
          onClick={(key) => void rerun(settings, false, key)}
        />
      </section>
    </main>
  );
}
