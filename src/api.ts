import { upload } from '@vercel/blob/client';
import type { FileRef, LoadedLedgers, RunResult, UploadedLedger } from './types';

// 'blob' (Vercel): files go to Vercel Blob first, so any size works.
// 'direct' (local dev): files are sent with every request to the local FastAPI server.
export const UPLOAD_MODE: 'blob' | 'direct' =
  import.meta.env.VITE_UPLOAD_MODE === 'direct' ? 'direct' : 'blob';

const CONTENT_TYPES: Record<string, string> = {
  pdf: 'application/pdf',
  xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  xls: 'application/vnd.ms-excel',
  csv: 'text/csv',
};

export function fileExtension(name: string): string {
  const dot = name.lastIndexOf('.');
  return dot >= 0 ? name.slice(dot + 1).toLowerCase() : '';
}

export async function uploadToBlob(file: File, onProgress: (percent: number) => void): Promise<string> {
  const blob = await upload(`ledgers/${file.name}`, file, {
    access: 'public',
    handleUploadUrl: '/api/blob-upload',
    contentType: CONTENT_TYPES[fileExtension(file.name)] ?? 'application/octet-stream',
    multipart: file.size > 8 * 1024 * 1024,
    onUploadProgress: ({ percentage }) => onProgress(percentage),
  });
  return blob.url;
}

export interface RunRequest {
  read: boolean;
  our: UploadedLedger | null;
  party: UploadedLedger | null;
  loaded: LoadedLedgers | null;
  date_tolerance: number;
  amount_tolerance: number;
  party_excel_from_our_books: boolean;
  selected_fy: string | null;
  clicked: string | null;
}

export async function runPage(request: RunRequest): Promise<RunResult> {
  const form = new FormData();

  const ref = ({ file, url }: UploadedLedger, part: string): FileRef => {
    if (UPLOAD_MODE === 'blob' && url) return { name: file.name, url };
    form.append(part, file, file.name);
    return { name: file.name, part };
  };

  const readingNewFiles = request.read && request.our && request.party;

  const params = {
    read: request.read,
    our: request.read && request.our ? ref(request.our, 'our') : null,
    party: request.read && request.party ? ref(request.party, 'party') : null,
    loaded:
      request.loaded && !readingNewFiles
        ? {
            our: ref(request.loaded.our, 'loaded_our'),
            party: ref(request.loaded.party, 'loaded_party'),
            party_excel_from_our_books: request.loaded.party_excel_from_our_books,
          }
        : null,
    date_tolerance: request.date_tolerance,
    amount_tolerance: request.amount_tolerance,
    party_excel_from_our_books: request.party_excel_from_our_books,
    selected_fy: request.selected_fy,
    clicked: request.clicked,
  };
  form.append('params', JSON.stringify(params));

  const response = await fetch('/api/run', { method: 'POST', body: form });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      detail = (await response.json()).detail ?? detail;
    } catch {
      // keep the status text
    }
    throw new Error(detail);
  }
  return response.json();
}
