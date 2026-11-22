const imageExtensions = new Map([
  ['image/png', '.png'], ['image/jpeg', '.jpg'], ['image/webp', '.webp'], ['image/gif', '.gif'],
]);

export function clipboardFiles(clipboard: Pick<DataTransfer, 'items' | 'files'> | null): File[] {
  if (!clipboard) return [];
  const items = Array.from(clipboard.items || []).flatMap(item => {
    if (item.kind !== 'file') return [];
    const file = item.getAsFile();
    return file ? [file] : [];
  });
  return (items.length ? items : Array.from(clipboard.files || [])).map(file => {
    const suffix = imageExtensions.get(file.type.toLowerCase());
    const name = file.name.toLowerCase();
    if (!suffix || name.endsWith(suffix) || (suffix === '.jpg' && name.endsWith('.jpeg'))) return file;
    // Screenshot pastes on some browsers expose an image MIME type but no filename extension.
    const stem = file.name.replace(/\.[^.]+$/, '').trim() || '粘贴图片';
    return new File([file], `${stem}${suffix}`, { type: file.type, lastModified: file.lastModified });
  });
}
