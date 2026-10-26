import { useMemo, useState } from 'react';
import type { CellValue, ColumnKind } from '../types';

interface Props {
  columns: string[];
  kinds: ColumnKind[];
  rows: CellValue[][];
}

function formatCell(value: CellValue): string {
  if (value === null) return 'None';
  if (typeof value === 'number') {
    return Number.isInteger(value) ? String(value) : Number(value.toFixed(4)).toString();
  }
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  return value;
}

function compare(a: CellValue, b: CellValue): number {
  if (a === b) return 0;
  if (a === null) return 1;
  if (b === null) return -1;
  if (typeof a === 'number' && typeof b === 'number') return a - b;
  return String(a).localeCompare(String(b), undefined, { numeric: true });
}

// Equivalent of st.dataframe(..., use_container_width=True, hide_index=True).
export function DataTable({ columns, kinds, rows }: Props) {
  const [sort, setSort] = useState<{ column: number; ascending: boolean } | null>(null);

  const sortedRows = useMemo(() => {
    if (!sort) return rows;
    const sorted = [...rows].sort((a, b) => compare(a[sort.column], b[sort.column]));
    return sort.ascending ? sorted : sorted.reverse();
  }, [rows, sort]);

  const toggleSort = (column: number) =>
    setSort((current) =>
      current?.column !== column
        ? { column, ascending: true }
        : current.ascending
          ? { column, ascending: false }
          : null,
    );

  return (
    <div className="dataframe">
      <table>
        <thead>
          <tr>
            {columns.map((name, i) => (
              <th key={i} className={kinds[i] === 'number' ? 'num' : undefined} onClick={() => toggleSort(i)}>
                {name}
                {sort?.column === i ? (sort.ascending ? ' ↑' : ' ↓') : ''}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sortedRows.map((row, r) => (
            <tr key={r}>
              {row.map((value, c) => (
                <td key={c} className={`${kinds[c] === 'number' ? 'num' : ''}${value === null ? ' null' : ''}`}>
                  {formatCell(value)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
