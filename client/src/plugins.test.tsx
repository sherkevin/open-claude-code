import { describe, expect, it } from 'vitest';
import { isDigest, isSecureSource, permissionLabels, permissionOptions, pluginStatus } from './PluginsPage';
import type { PluginPermission } from './types';

describe('plugin governance UI contract', () => {
  it('labels every permission the server can grant', () => {
    const expected: PluginPermission[] = [
      'workspace.files.read', 'workspace.files.write', 'knowledge.read', 'web.read',
      'mcp.connection.read', 'artifact.write', 'memory.read',
    ];
    expect(permissionOptions).toEqual(expected);
    expect(permissionOptions.every(permission => permissionLabels[permission].length > 0)).toBe(true);
  });

  it('shows pending, approved and revoked states distinctly', () => {
    expect(pluginStatus('pending').label).toBe('待审核');
    expect(pluginStatus('pending').tone).toBe('amber');
    expect(pluginStatus('approved').label).toBe('已批准');
    expect(pluginStatus('approved').tone).toBe('green');
    expect(pluginStatus('revoked').label).toBe('已撤销');
  });

  it('requires a fixed lowercase digest and a credential-free HTTPS source', () => {
    expect(isDigest('a'.repeat(64))).toBe(true);
    expect(isDigest('A'.repeat(64))).toBe(false);
    expect(isDigest('a'.repeat(63))).toBe(false);
    expect(isSecureSource('https://github.com/example/plugin')).toBe(true);
    expect(isSecureSource('http://github.com/example/plugin')).toBe(false);
    expect(isSecureSource('https://user:secret@example.com/plugin')).toBe(false);
    expect(isSecureSource('https://example.com/plugin#readme')).toBe(false);
  });
});
