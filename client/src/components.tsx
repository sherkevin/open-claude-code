import { useEffect, useRef, useState } from 'react';
import type { ButtonHTMLAttributes, ReactNode } from 'react';
import { AlertCircle, Check, Download, FileText, LoaderCircle, X } from 'lucide-react';
import { api, errorText, fileURL, sizeText } from './api';
import type { StoredFile } from './types';

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
type ModalLayer = { node: HTMLElement; previous: HTMLElement | null };
type ModalStack = { layers: ModalLayer[]; overflow: string; previous: HTMLElement | null };
const modalStacks = new WeakMap<Document, ModalStack>();
const modalFocusable = (node: HTMLElement) => [...node.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), a[href], [tabindex="0"]')].filter(el => el.offsetParent !== null);

/** Share keyboard ownership and the body lock across nested dialogs. */
export function bindModalLayer(node: HTMLElement, onClose: () => void) {
  const doc = node.ownerDocument;
  let stack = modalStacks.get(doc);
  if (!stack) {
    stack = { layers: [], overflow: doc.body.style.overflow, previous: doc.activeElement as HTMLElement | null };
    modalStacks.set(doc, stack);
  }
  const layer = { node, previous: doc.activeElement as HTMLElement | null };
  // Child effects may mount before their parent; keep the deeper dialog on top.
  const descendant = stack.layers.findIndex(item => node.contains(item.node));
  stack.layers.splice(descendant < 0 ? stack.layers.length : descendant, 0, layer);
  doc.body.style.overflow = 'hidden';
  const top = () => stack.layers.at(-1);
  const focusFirst = (target: HTMLElement) => (modalFocusable(target)[0] || target).focus();
  const timer = window.setTimeout(() => { if (top() === layer) focusFirst(node); }, 0);
  const onKey = (event: KeyboardEvent) => {
    if (top() !== layer) return;
    if (event.key === 'Escape') {
      event.preventDefault(); event.stopImmediatePropagation(); onClose(); return;
    }
    if (event.key !== 'Tab') return;
    event.stopImmediatePropagation();
    const focusable = modalFocusable(node);
    const first = focusable[0], last = focusable.at(-1);
    if (!first) { event.preventDefault(); node.focus(); return; }
    if (!node.contains(doc.activeElement)) { event.preventDefault(); (event.shiftKey ? last : first)?.focus(); return; }
    if (event.shiftKey && (doc.activeElement === first || doc.activeElement === node)) { event.preventDefault(); last?.focus(); }
    if (!event.shiftKey && (doc.activeElement === last || doc.activeElement === node)) { event.preventDefault(); first.focus(); }
  };
  doc.addEventListener('keydown', onKey);
  let disposed = false;
  return () => {
    if (disposed) return;
    disposed = true;
    window.clearTimeout(timer); doc.removeEventListener('keydown', onKey);
    const wasTop = top() === layer;
    stack.layers.splice(stack.layers.indexOf(layer), 1);
    const remaining = top();
    if (!remaining) { doc.body.style.overflow = stack.overflow; modalStacks.delete(doc); }
    if (!wasTop) return;
    const previous = layer.previous?.isConnected && (!remaining || remaining.node.contains(layer.previous)) ? layer.previous : null;
    if (previous) previous.focus();
    else if (remaining) focusFirst(remaining.node);
    else if (stack.previous?.isConnected) stack.previous.focus();
  };
}
export function Modal({ title, description, children, onClose, wide = false }: { title: string; description?: string; children: ReactNode; onClose: () => void; wide?: boolean }) {
  const dialog = useRef<HTMLDivElement>(null);
  const closeRef = useRef(onClose); closeRef.current = onClose;
  useEffect(() => {
    if (dialog.current) return bindModalLayer(dialog.current, () => closeRef.current());
  }, []);
  return <div className="modal-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}><div ref={dialog} tabIndex={-1} role="dialog" aria-modal="true" aria-label={title} className={`modal ${wide ? 'wide' : ''}`}><header className="modal-header"><div><h2>{title}</h2>{description && <p>{description}</p>}</div><button type="button" className="icon-button" aria-label="关闭窗口" onClick={onClose}><X size={20}/></button></header>{children}</div></div>;
}
export function FileChip({ file, onRemove, onPreview }: { file: StoredFile; onRemove?: () => void; onPreview?: () => void }) {
  return <div className="file-chip"><button type="button" onClick={onPreview} className="file-chip-main" title={file.name}><FileText size={17}/><span>{file.name}<small>{sizeText(file.size)}</small></span></button>{onRemove ? <button type="button" onClick={onRemove} className="icon-button" aria-label={`移除 ${file.name}`}><X size={14}/></button> : <a className="icon-button" aria-label={`下载 ${file.name}`} href={fileURL(file.id)} download={file.name}><Download size={14}/></a>}</div>;
}
export function FilePreview({ file, onClose }: { file: StoredFile; onClose: () => void }) {
  const [text, setText] = useState(''); const [error, setError] = useState(''); const [url, setURL] = useState('');
  const image = ['image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/avif'].includes(file.mime_type);
  const plain = file.mime_type.startsWith('text/plain') || ['text/csv', 'application/json', 'text/markdown'].includes(file.mime_type);
  const pdf = file.mime_type === 'application/pdf';
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
  return <Modal title={file.name} description={`${file.mime_type} · ${sizeText(file.size)}`} wide onClose={onClose}><div className="modal-body"><ErrorNotice message={error}/>{image && url ? <img src={url} alt={file.name} className="image-preview"/> : pdf && url ? <iframe title={file.name} src={url} className="pdf-preview" sandbox="allow-same-origin"/> : plain && !error ? <pre className="text-preview">{text || '正在读取…'}</pre> : !(image || plain || pdf) ? <EmptyState icon={<FileText/>} title="下载后查看此附件" description="这个文件类型暂不提供在线预览。"/> : !error ? <Loader/> : null}</div><footer className="modal-footer"><a className="button primary" href={fileURL(file.id)} download={file.name}><Download size={16}/>下载原文件</a></footer></Modal>;
}
export function Attachment({ id, onPreview }: { id: string; onPreview: (file: StoredFile) => void }) {
  const [file, setFile] = useState<StoredFile | null>(null); const [error, setError] = useState(false);
  useEffect(() => { let current = true; api<StoredFile>(`/files/${id}`).then(value => { if (current) setFile(value); }).catch(() => { if (current) setError(true); }); return () => { current = false; }; }, [id]);
  return file ? <FileChip file={file} onPreview={() => onPreview(file)}/> : <span className="file-loading"><FileText size={15}/>{error ? '附件不可访问' : '读取附件…'}</span>;
}
