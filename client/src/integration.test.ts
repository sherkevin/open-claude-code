import { describe, expect, it } from 'vitest';
import { ApiError } from './api';
import { MCP_CONFIG_MAX_BYTES, importErrorText, parseMcpConfig } from './integration';

describe('MCP import input and safe errors', () => {
  it('preserves both supported JSON containers for authoritative server validation', () => {
    const tool = { type: 'mcp', server_label: 'knowledge', server_url: 'https://mcp.example.com/mcp', authorization: 'synthetic-test-credential' };
    expect(parseMcpConfig(JSON.stringify(tool))).toEqual(tool);
    expect(parseMcpConfig(JSON.stringify([tool]))).toEqual([tool]);
    const standard = { mcpServers: { knowledge: { type: 'http', url: tool.server_url } } };
    expect(parseMcpConfig(JSON.stringify(standard))).toEqual(standard);
  });
  it('does not echo a credential from invalid JSON or a server error body', () => {
    const secret = 'synthetic-private-marker-123';
    let parsing: unknown;
    try { parseMcpConfig(`{"authorization":"${secret}" invalid`); } catch (error) { parsing = error; }
    expect(importErrorText(parsing)).toContain('JSON');
    expect(importErrorText(parsing)).not.toContain(secret);
    expect(importErrorText(new ApiError(`Authorization: ${secret}`, 422, 'INVALID_CONFIG'))).not.toContain(secret);
    expect(importErrorText(new Error(secret))).not.toContain(secret);
  });
  it('limits UTF-8 bytes and rejects scalar or empty input', () => {
    const oversized = JSON.stringify({ value: '中'.repeat(MCP_CONFIG_MAX_BYTES / 2) });
    expect(() => parseMcpConfig(oversized)).toThrow('256 KiB');
    for (const input of ['', 'null', '"https://mcp.example.com/mcp"', 'true', '42']) expect(() => parseMcpConfig(input)).toThrow();
  });
  it('tells the user conflicts do not update existing connections', () => {
    expect(importErrorText(new ApiError('private conflict data', 409, 'CONFLICT'))).toContain('整批尚未导入');
  });
  it('separates an unapproved service host from a malformed configuration without echoing the response', () => {
    const secret = 'synthetic-authorization-do-not-display';
    const denied = importErrorText(new ApiError(secret, 422, 'MCP_HOST_NOT_ALLOWED'));
    expect(denied).toContain('工作台管理员核准');
    expect(denied).toContain('配置尚未导入');
    expect(denied).not.toContain(secret);
    const invalid = importErrorText(new ApiError(secret, 422, 'INVALID_MCP_URL'));
    expect(invalid).toContain('地址格式不支持');
    expect(invalid).not.toContain('核准');
    expect(invalid).not.toContain(secret);
  });
  it('keeps permission and login errors ahead of lower-level configuration errors', () => {
    expect(importErrorText(new ApiError('hidden', 403, 'MCP_HOST_NOT_ALLOWED'))).toContain('没有导入连接的权限');
    expect(importErrorText(new ApiError('hidden', 401, 'INVALID_MCP_URL'))).toContain('登录已过期');
  });
});

