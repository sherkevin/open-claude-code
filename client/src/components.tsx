import { useEffect, useRef, useState } from 'react';
import type { ButtonHTMLAttributes, ReactNode } from 'react';
import { AlertCircle, Check, Download, FileText, LoaderCircle, Minus, Plus, RotateCcw, X } from 'lucide-react';
import { api, errorText, fileURL, sizeText } from './api';
import type { StoredFile } from './types';
import XlsxPreview from './XlsxPreview';

export function Button({ children, className = '', busy, variant = 'primary', type = 'button', ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { busy?: boolean; variant?: 'primary' | 'secondary' | 'ghost' | 'danger' }) {
  return <button {...props} type={type} disabled={props.disabled || busy} className={`button ${variant} ${className}`}>{busy && <LoaderCircle size={16} className="spin" />}{children}</button>;
}
export function ErrorNotice({ message, onDismiss }: { message: string; onDismiss?: () => void }) {
  if (!message) return null;
  return <div className="notice error" role="alert"><AlertCircle size={17} /><span>{message}</span>{onDismiss && <button type="button" className="icon-button" onClick={onDismiss} aria-label="关闭提示"><X size={16}/></button>}</div>;
}
export function SuccessNotice({ message }: { message: string }) { return message ? <div className="notice success" role="status"><Check size={17}/>{message}</div> : null; }
export function Loader({ text = '正在加载…' }: { text?: string }) { return <div className="loading"><LoaderCircle className="spin" size={22}/><span>{text}</span></div>; }
export function PageHeader({ eyebrow, title, description, actions }: { eyebrow: string; title: string; description: string; actions?: ReactNode }) {
  return <header className="page-header"><div><span className="eyebrow">{eyebrow}</span><h1>{title}</h1><p>{description}</p></div>{actions && <div className="page-actions">{actions}</div>}</header>;
}
export function Field({ label, hint, children, required }: { label: string; hint?: string; children: ReactNode; required?: boolean }) {
  return <label className="field"><span className="field-label">{label}{required && <span className="required"> *</span>}</span>{children}{hint && <small>{hint}</small>}</label>;
}
export function EmptyState({ icon, title, description, action }: { icon: ReactNode; title: string; description: string; action?: ReactNode }) {
  return <div className="empty-state"><div className="empty-icon">{icon}</div><h2>{title}</h2><p>{description}</p>{action}</div>;
}
export function Modal({ title, description, children, onClose, wide = false }: { title: string; description?: string; children: ReactNode; onClose: () => void; wide?: boolean }) {
  const dialog = useRef<HTMLDivElement>(null);
  const closeRef = useRef(onClose); closeRef.current = onClose;
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const oldOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const timer = window.setTimeout(() => dialog.current?.querySelector<HTMLElement>('input, select, textarea, button')?.focus(), 0);
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') closeRef.current();
      if (event.key !== 'Tab') return;
      const focusable = [...(dialog.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), a[href], [tabindex="0"]') || [])].filter(el => el.offsetParent !== null);
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    };
    document.addEventListener('keydown', onKey);
    return () => { window.clearTimeout(timer); document.removeEventListener('keydown', onKey); document.body.style.overflow = oldOverflow; previous?.focus(); };
  }, []);
  return <div className="modal-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}><div ref={dialog} role="dialog" aria-modal="true" aria-label={title} className={`modal ${wide ? 'wide' : ''}`}><header className="modal-header"><div><h2>{title}</h2>{description && <p>{description}</p>}</div><button type="button" className="icon-button" aria-label="关闭窗口" onClick={onClose}><X size={20}/></button></header>{children}</div></div>;
}
export function FileChip({ file, onRemove, onPreview }: { file: StoredFile; onRemove?: () => void; onPreview?: () => void }) {
  return <div className="file-chip"><button type="button" onClick={onPreview} className="file-chip-main" title={file.name}><FileText size={17}/><span>{file.name}<small>{sizeText(file.size)}</small></span></button>{onRemove ? <button type="button" onClick={onRemove} className="icon-button" aria-label={`移除 ${file.name}`}><X size={14}/></button> : <a className="icon-button" aria-label={`下载 ${file.name}`} href={fileURL(file.id)} download={file.name}><Download size={14}/></a>}</div>;
}
export function FilePreview({ file, onClose }: { file: StoredFile; onClose: () => void }) {
  const [text, setText] = useState(''); const [error, setError] = useState(''); const [url, setURL] = useState(''); const [zoom, setZoom] = useState(1);
  const image = ['image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/avif'].includes(file.mime_type);
  const xlsx = file.name.toLowerCase().endsWith('.xlsx') || file.mime_type === 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';
  const plain = file.mime_type.startsWith('text/plain') || ['text/csv', 'application/json', 'text/markdown'].includes(file.mime_type);
  const pdf = file.mime_type === 'application/pdf';
  useEffect(() => { setZoom(1); }, [file.id]);
  useEffect(() => {
    const controller = new AbortController(); let objectURL = '';
    if (!(image || plain || pdf)) return;
    if (plain && file.size > 1048576) { setError('文本较大，请下载后查看。'); return; }
    fetch(fileURL(file.id), { credentials: 'include', signal: controller.signal }).then(async response => {
      if (!response.ok) throw new Error('附件暂时无法预览，请重试或下载。');
      const blob = await response.blob();
      if (controller.signal.aborted) return;
      if (plain) setText(await blob.text()); else { objectURL = URL.createObjectURL(blob); setURL(objectURL); }
    }).catch(e => { if (!controller.signal.aborted) setError(errorText(e)); });
    return () => { controller.abort(); if (objectURL) URL.revokeObjectURL(objectURL); };
  }, [file.id, file.size, image, plain, pdf]);
  const changeZoom = (factor: number) => setZoom(value => Math.min(4, Math.max(0.25, Math.round(value * factor * 100) / 100)));
  return <Modal title={file.name} description={`${file.mime_type} · ${sizeText(file.size)}`} wide onClose={onClose}><div className="modal-body"><ErrorNotice message={error}/>{xlsx ? <XlsxPreview file={file}/> : image && url ? <div className="image-preview-wrap"><div className="image-zoom-controls" role="group" aria-label="图片缩放"><button type="button" className="icon-button" title="缩小" aria-label="缩小" disabled={zoom <= 0.25} onClick={() => changeZoom(0.5)}><Minus size={15}/></button><span>{Math.round(zoom * 100)}%</span><button type="button" className="icon-button" title="放大" aria-label="放大" disabled={zoom >= 4} onClick={() => changeZoom(2)}><Plus size={15}/></button><button type="button" className="icon-button" title="恢复原始大小" aria-label="恢复原始大小" onClick={() => setZoom(1)}><RotateCcw size={15}/></button></div><div className="image-preview-scroll"><img src={url} alt={file.name} className="image-preview" style={{ transform: `scale(${zoom})` }}/></div></div> : pdf && url ? <iframe title={file.name} src={url} className="pdf-preview" sandbox="allow-same-origin"/> : plain && !error ? <pre className="text-preview">{text || '正在读取…'}</pre> : !(image || plain || pdf) ? <EmptyState icon={<FileText/>} title="下载后查看此附件" description="这个文件类型暂不提供在线预览。"/> : !error ? <Loader/> : null}</div><footer className="modal-footer"><a className="button primary" href={fileURL(file.id)} download={file.name}><Download size={16}/>下载原文件</a></footer></Modal>;
}
export function Attachment({ id, onPreview }: { id: string; onPreview: (file: StoredFile) => void }) {
  const [file, setFile] = useState<StoredFile | null>(null); const [error, setError] = useState(false);
  useEffect(() => { let current = true; api<StoredFile>(`/files/${id}`).then(value => { if (current) setFile(value); }).catch(() => { if (current) setError(true); }); return () => { current = false; }; }, [id]);
  return file ? <FileChip file={file} onPreview={() => onPreview(file)}/> : <span className="file-loading"><FileText size={15}/>{error ? '附件不可访问' : '读取附件…'}</span>;
}
