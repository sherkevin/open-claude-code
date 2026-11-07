export interface FeishuH5Config { enabled: boolean; app_id: string; path: string }

interface FeishuCallbackError { errno?: number; errString?: string; message?: string }
interface FeishuApi {
  requestAccess?: (options: {
    appID: string;
    scopeList: string[];
    success: (result: { code?: string }) => void;
    fail: (error: FeishuCallbackError) => void;
  }) => unknown;
  requestAuthCode?: (options: {
    appId: string;
    success: (result: { code?: string }) => void;
    fail: (error: FeishuCallbackError) => void;
  }) => unknown;
}

interface FeishuH5Sdk { ready?: (callback: () => void) => void }

declare global {
  interface Window {
    tt?: FeishuApi;
    h5sdk?: FeishuH5Sdk;
  }
}

const SDK_URLS = [
  'https://lf-scm-cn.feishucdn.com/lark/op/h5-js-sdk-1.5.38.js',
  'https://lf1-cdn-tos.bytegoofy.com/goofy/lark/op/h5-js-sdk-1.5.38.js',
];

let sdkPromise: Promise<void> | undefined;

export function isFeishuEmbed(location: Pick<Location, 'pathname' | 'search'> = window.location): boolean {
  const query = new URLSearchParams(location.search);
  return location.pathname === '/feishu' || location.pathname === '/feishu/' || query.get('embed') === 'feishu';
}

function loadScript(url: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const existing = document.querySelector<HTMLScriptElement>(`script[data-feishu-sdk="${url}"]`);
    if (existing) {
      if (window.tt || window.h5sdk) resolve();
      else {
        existing.addEventListener('load', () => resolve(), { once: true });
        existing.addEventListener('error', () => reject(new Error('飞书网页接口加载失败')), { once: true });
      }
      return;
    }
    const script = document.createElement('script');
    script.src = url;
    script.async = true;
    script.dataset.feishuSdk = url;
    script.onload = () => resolve();
    script.onerror = () => reject(new Error('飞书网页接口加载失败'));
    document.head.appendChild(script);
  });
}

async function ensureSdk(): Promise<void> {
  if (window.tt || window.h5sdk) return;
  if (!sdkPromise) {
    sdkPromise = (async () => {
      let lastError: unknown;
      for (const url of SDK_URLS) {
        try {
          await loadScript(url);
          if (window.tt || window.h5sdk) return;
        } catch (error) {
          lastError = error;
        }
      }
      throw lastError instanceof Error ? lastError : new Error('当前页面没有可用的飞书网页接口');
    })();
  }
  await sdkPromise;
}

function callbackError(error: FeishuCallbackError): Error {
  return new Error(error.errString || error.message || '飞书免登未完成');
}

function callRequestAccess(api: FeishuApi, appId: string): Promise<string> {
  return new Promise((resolve, reject) => {
    if (!api.requestAccess) {
      reject(new Error('当前飞书客户端不支持 requestAccess'));
      return;
    }
    const result = api.requestAccess({
      appID: appId,
      scopeList: [],
      success: response => response.code ? resolve(response.code) : reject(new Error('飞书未返回免登授权码')),
      fail: error => reject(Object.assign(callbackError(error), { errno: error.errno })),
    });
    if (result && typeof (result as Promise<unknown>).then === 'function') {
      void (result as Promise<{ code?: string }>).then(response => response?.code ? resolve(response.code) : reject(new Error('飞书未返回免登授权码')), reject);
    }
  });
}

function callRequestAuthCode(api: FeishuApi, appId: string): Promise<string> {
  return new Promise((resolve, reject) => {
    if (!api.requestAuthCode) {
      reject(new Error('当前页面没有可用的飞书免登接口'));
      return;
    }
    const result = api.requestAuthCode({
      appId,
      success: response => response.code ? resolve(response.code) : reject(new Error('飞书未返回免登授权码')),
      fail: error => reject(callbackError(error)),
    });
    if (result && typeof (result as Promise<unknown>).then === 'function') {
      void (result as Promise<{ code?: string }>).then(response => response?.code ? resolve(response.code) : reject(new Error('飞书未返回免登授权码')), reject);
    }
  });
}

export async function requestFeishuCode(appId: string): Promise<string> {
  if (!appId) throw new Error('飞书应用编号未配置');
  await ensureSdk();
  const run = async () => {
    const api = window.tt;
    if (!api) throw new Error('当前页面没有可用的飞书网页接口');
    try {
      return await callRequestAccess(api, appId);
    } catch (error) {
      // Feishu errno 103 means the client is too old for requestAccess.  The
      // older requestAuthCode API remains supported as a compatibility path.
      if (error && typeof error === 'object' && 'errno' in error && error.errno === 103) return callRequestAuthCode(api, appId);
      throw error;
    }
  };
  if (window.h5sdk?.ready) {
    return new Promise((resolve, reject) => window.h5sdk?.ready?.(() => { void run().then(resolve, reject); }));
  }
  return run();
}
