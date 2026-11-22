export type StoredUpload = {
  owner: string; scope: string; id: string; file: File; binary: boolean; receivedAt: number; order?: number; error?: string;
};

const storeName = 'uploads';
const retention = 7 * 24 * 60 * 60 * 1000;
let opening: Promise<IDBDatabase> | undefined;

function open() {
  if (!opening) {
    opening = new Promise((resolve, reject) => {
      const request = indexedDB.open('kaiwu-composer-uploads', 1);
      request.onupgradeneeded = () => {
        const store = request.result.createObjectStore(storeName, { keyPath: ['scope', 'id'] });
        store.createIndex('scope', 'scope');
        store.createIndex('owner', 'owner');
        store.createIndex('receivedAt', 'receivedAt');
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
      request.onblocked = () => reject(new Error('Browser upload storage is busy'));
    });
    void opening.catch(() => { opening = undefined; });
  }
  return opening;
}

async function transact<T>(mode: IDBTransactionMode, read: (store: IDBObjectStore) => IDBRequest<T>) {
  const database = await open();
  return new Promise<T>((resolve, reject) => {
    const transaction = database.transaction(storeName, mode);
    const request = read(transaction.objectStore(storeName));
    transaction.oncomplete = () => resolve(request.result);
    transaction.onabort = () => reject(transaction.error || request.error);
    transaction.onerror = () => reject(transaction.error || request.error);
  });
}

async function removeIndexed(index: 'owner' | 'receivedAt', range: IDBKeyRange) {
  await transact('readwrite', store => {
    const request = store.index(index).openKeyCursor(range);
    request.onsuccess = () => {
      const cursor = request.result;
      if (cursor) { store.delete(cursor.primaryKey); cursor.continue(); }
    };
    return request;
  });
}

export async function readUploads(scope: string): Promise<StoredUpload[]> {
  await removeIndexed('receivedAt', IDBKeyRange.upperBound(Date.now() - retention));
  return transact('readonly', store => store.index('scope').getAll(scope));
}

export const putUpload = (entry: StoredUpload) => transact('readwrite', store => store.put(entry));
export const deleteUpload = (scope: string, id: string) => transact('readwrite', store => store.delete([scope, id]));
export const deleteOwnerUploads = (owner: string) => removeIndexed('owner', IDBKeyRange.only(owner));
