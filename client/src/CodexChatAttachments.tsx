import { FileChip } from './components';
import type { StoredFile } from './types';

export default function CodexChatAttachments({ files, onPreview }: {
  files: StoredFile[];
  onPreview: (file: StoredFile) => void;
}) {
  if (files.length === 0) return null;
  return <section aria-label="当前聊天草稿附件">
    <p className="section-help">当前聊天草稿中的附件仅供你核对原件。工程任务只发送你输入的文本。</p>
    <div className="attachment-row">
      {files.map(file => <FileChip key={file.id} file={file} onPreview={() => onPreview(file)}/>)}
    </div>
  </section>;
}
