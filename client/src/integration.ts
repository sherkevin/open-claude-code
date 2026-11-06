import { ApiError } from './api';

export const MCP_CONFIG_MAX_BYTES = 256 * 1024;
class ImportInputError extends Error {}

/** Native JSON parse errors can echo submitted credentials; return only fixed messages. */
export function parseMcpConfig(text: string): Record<string, unknown> | unknown[] {
  if (!text.trim()) throw new ImportInputError('请先粘贴 MCP 配置 JSON。');
  if (new TextEncoder().encode(text).byteLength > MCP_CONFIG_MAX_BYTES) throw new ImportInputError('配置不能超过 256 KiB，请拆分后导入。');
  let value: unknown;
  try { value = JSON.parse(text); } catch { throw new ImportInputError('JSON 格式有误，请检查引号、逗号和括号。'); }
  if (!value || typeof value !== 'object') throw new ImportInputError('请粘贴 MCP 配置对象或数组，不能只填写地址或文本。');
  return value as Record<string, unknown> | unknown[];
}

export function importErrorText(error: unknown): string {
  if (error instanceof ImportInputError) return error.message;
  if (error instanceof ApiError) {
    if (error.status === 409) return '存在同名连接，整批尚未导入。请修改配置名称，或到列表编辑已有连接。';
    if (error.status === 401) return '登录已过期，请重新登录。';
    if (error.status === 403) return '当前账号没有导入连接的权限。';
    if (error.status === 413) return '配置不能超过 256 KiB，请拆分后导入。';
    if (error.status === 400 || error.status === 422) return '配置无法导入。请确认格式、HTTP MCP 地址和主机允许名单；每批最多 16 条，认证仅支持 Bearer Token。';
    if (error.status === 0) return '暂时无法连接服务，请检查网络后重试。';
  }
  return '导入请求未完成，请检查配置后重试。';
}
