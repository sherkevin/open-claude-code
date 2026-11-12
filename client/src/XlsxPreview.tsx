import { useEffect, useMemo, useState } from 'react';
import { ArrowDown, ArrowUp, Download, FileSpreadsheet, LoaderCircle, RefreshCw, ShieldCheck } from 'lucide-react';
import { api, errorText, fileURL } from './api';
import type { StoredFile, XlsxPreview as XlsxPreviewData } from './types';

export type XlsxSort = { column: number; direction: 'asc' | 'desc' } | null;

const numeric = (value: string) => value.trim() !== '' && Number.isFinite(Number(value));

export function xlsxFilterRows(rows: string[][], query: string): string[][] {
  const needle = query.trim().toLocaleLowerCase();
  if (!needle) return rows;
  return rows.filter(row => row.some(value => String(value ?? '').toLocaleLowerCase().includes(needle)));
}

export function xlsxSortRows(rows: string[][], sort: XlsxSort): string[][] {
  if (!sort) return rows;
  const direction = sort.direction === 'asc' ? 1 : -1;
  return [...rows].sort((left, right) => {
    const a = String(left[sort.column] ?? '');
    const b = String(right[sort.column] ?? '');
    if (numeric(a) && numeric(b)) return (Number(a) - Number(b)) * direction;
    return a.localeCompare(b, 'zh-CN', { numeric: true, sensitivity: 'base' }) * direction;
  });
}

export function xlsxPage(rows: string[][], page: number, pageSize = 50): { rows: string[][]; page: number; pageCount: number } {
  const pageCount = Math.max(1, Math.ceil(rows.length / pageSize));
  const current = Math.min(Math.max(1, page), pageCount);
  return { rows: rows.slice((current - 1) * pageSize, current * pageSize), page: current, pageCount };
}

export function xlsxFormulaNote(preview: XlsxPreviewData | null): string {
  if (!preview) return '';
  if (preview.formula_cells > 0) return `工作簿含 ${preview.formula_cells} 个公式单元格，预览只显示缓存值，绝不执行公式。`;
  if (preview.escaped_text_cells > 0) return `${preview.escaped_text_cells} 个以 = + - @ 开头的文本已按文本保存，不会被当作公式执行。`;
  return '预览只读取单元格文本，不执行公式或宏。';
}

export default function XlsxPreview({ file }: { file: StoredFile }) {
  const [data, setData] = useState<XlsxPreviewData | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [reload, setReload] = useState(0);
  const [sheetIndex, setSheetIndex] = useState(0);
  const [filter, setFilter] = useState('');
  const [sort, setSort] = useState<XlsxSort>(null);
  const [page, setPage] = useState(1);

  useEffect(() => {
    let current = true;
    setLoading(true);
    setError('');
    api<XlsxPreviewData>(`/files/${encodeURIComponent(file.id)}/xlsx-preview`)
      .then(value => {
        if (!current) return;
        setData(value);
        setSheetIndex(index => Math.min(index, Math.max(0, value.sheets.length - 1)));
        setSort(null);
        setPage(1);
      })
      .catch(cause => { if (current) setError(errorText(cause)); })
      .finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, [file.id, reload]);

  const sheet = data?.sheets[sheetIndex];
  const rows = useMemo(() => xlsxSortRows(xlsxFilterRows(sheet?.rows || [], filter), sort), [sheet, filter, sort]);
  const visible = useMemo(() => xlsxPage(rows, page), [rows, page]);

  const toggleSort = (column: number) => {
    setSort(current => current?.column === column ? { column, direction: current.direction === 'asc' ? 'desc' : 'asc' } : { column, direction: 'asc' });
    setPage(1);
  };
  const reloadPreview = () => { setFilter(''); setSort(null); setPage(1); setReload(value => value + 1); };

  return <div className="xlsx-preview" data-testid="xlsx-preview">
    {loading ? <div className="loading" role="status"><LoaderCircle className="spin" size={22}/><span>正在读取 Excel 预览…</span></div> : null}
    {error ? <div className="notice error" role="alert"><span>{error}</span></div> : null}
    {!loading && data ? <>
      <div className="xlsx-toolbar">
        <div className="xlsx-sheets" role="tablist" aria-label="工作表">
          {data.sheets.map((item, index) => <button key={`${item.name}-${index}`} type="button" role="tab" aria-selected={index === sheetIndex} className={index === sheetIndex ? 'selected' : ''} onClick={() => { setSheetIndex(index); setFilter(''); setSort(null); setPage(1); }}><FileSpreadsheet size={14}/>{item.name}</button>)}
        </div>
        <div className="xlsx-actions">
          <input aria-label="筛选 Excel 单元格" placeholder="筛选当前工作表…" value={filter} onChange={event => { setFilter(event.target.value); setPage(1); }}/>
          <button type="button" className="icon-button" title="重新读取预览" aria-label="重新读取预览" onClick={reloadPreview}><RefreshCw size={15}/></button>
          <a className="button secondary" href={fileURL(file.id)} download={file.name}><Download size={15}/>下载原文件</a>
        </div>
      </div>
      <p className="xlsx-safety"><ShieldCheck size={14}/>{xlsxFormulaNote(data)}</p>
      {sheet && sheet.columns.length > 0 ? <div className="xlsx-table-wrap">
        <table className="xlsx-table">
          <thead><tr>{sheet.columns.map((column, index) => <th key={`${column}-${index}`}><button type="button" onClick={() => toggleSort(index)} title={`按 ${column || `第 ${index + 1} 列`} 排序`}>{column || `第 ${index + 1} 列`}{sort?.column === index ? (sort.direction === 'asc' ? <ArrowUp size={12}/> : <ArrowDown size={12}/>) : null}</button></th>)}</tr></thead>
          <tbody>{visible.rows.map((row, rowIndex) => <tr key={rowIndex}>{sheet.columns.map((_, columnIndex) => <td key={columnIndex}>{String(row[columnIndex] ?? '')}</td>)}</tr>)}</tbody>
        </table>
        {!visible.rows.length && <p className="section-help">没有符合筛选条件的数据。</p>}
      </div> : <p className="section-help">这个工作表没有可预览的单元格。</p>}
      <div className="xlsx-footer">
        <span>共 {rows.length} 行{filter ? `（已筛选，原 ${sheet?.rows.length || 0} 行）` : ''}{data.truncated ? ' · 预览已截断' : ''}</span>
        {visible.pageCount > 1 && <span className="xlsx-pager"><button type="button" className="icon-button" aria-label="上一页" disabled={visible.page <= 1} onClick={() => setPage(visible.page - 1)}><ArrowUp size={14}/></button>第 {visible.page} / {visible.pageCount} 页<button type="button" className="icon-button" aria-label="下一页" disabled={visible.page >= visible.pageCount} onClick={() => setPage(visible.page + 1)}><ArrowDown size={14}/></button></span>}
      </div>
    </> : null}
  </div>;
}
