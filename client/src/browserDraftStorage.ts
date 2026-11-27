export const composerOwnerPrefix = (owner: string) => 'kaiwu:composer:' + encodeURIComponent(owner) + ':';
export const composerScope = (owner: string, conversation: string) => composerOwnerPrefix(owner) + encodeURIComponent(conversation);

export function browserDraftDatabase(name: string, upgrade: (database: IDBDatabase) => void, blocked: string) {
  let opening: Promise<IDBDatabase> | undefined;
  return () => {
    if (!opening) {
      opening = new Promise<IDBDatabase>((resolve, reject) => {
        const request = indexedDB.open(name, 1);
        request.onupgradeneeded = () => upgrade(request.result);
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error);
        request.onblocked = () => reject(new Error(blocked));
      });
      void opening.catch(() => { opening = undefined; });
    }
    return opening;
  };
}
