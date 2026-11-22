import { mkdir, open } from 'node:fs/promises'
import { basename, dirname, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'

/** Fork only an exact completed boundary of the runtime's bound conversation. */
export async function forkWorkspaceSession(ctx: any, config: any, params: any) {
  if (params === null || typeof params !== 'object' || Object.keys(params).sort().join(',') !== 'atSeq,childSessionId,sessionId'
      || params.sessionId !== config.sessionId || typeof params.childSessionId !== 'string'
      || !/^workspace-[0-9a-f]{32}$/u.test(params.childSessionId) || params.childSessionId === config.sessionId
      || !Number.isSafeInteger(params.atSeq) || params.atSeq < 0 || params.atSeq >= 100_000) {
    throw new Error('Unauthorized workspace fork request')
  }
  const source = ctx.agents.get(config.sessionId)?.session
  if (source?.id !== config.sessionId) throw new Error('Fork session unavailable')
  const events = source.snapshotEvents().slice(0, params.atSeq + 1)
  if (events.at(-1)?.type !== 'turn/end' || events.at(-1)?.seq !== params.atSeq) {
    throw new Error('Fork boundary is not a completed turn')
  }
  const childId = params.childSessionId.slice('workspace-'.length)
  const childCwd = resolve(dirname(config.cwd), childId)
  const sourceHome = resolve(config.profileRoot)
  if (basename(config.cwd) !== basename(sourceHome) || basename(sourceHome) === childId) {
    throw new Error('Fork does not have an isolated destination')
  }
  const childHome = resolve(dirname(sourceHome), childId)
  const sourceImport = (path: string) => import(pathToFileURL(resolve(config.sourceDir, path)).href)
  const [{ buildForkSeed }, { logPath, toHeaderLine, eventLines }, { commitPreparedImageFile }] = await Promise.all([
    sourceImport('packages/core/session/src/fork.ts'),
    sourceImport('packages/session/session-persistence-jsonl/src/format.ts'),
    sourceImport('packages/attachment/attachment-local/src/store.ts'),
  ])
  const childLog = logPath(resolve(childHome, 'sessions'), childCwd, params.childSessionId, 'none')
  const inheritedImages = new Map<string, any>()
  const remaining = [...events]
  for (let value = remaining.pop(); value !== undefined; value = remaining.pop()) {
    if (!value || typeof value !== 'object') continue
    if (value.type === 'image' && /^sha256:[0-9a-f]{64}$/u.test(value.attachment?.attachmentId ?? '')) {
      inheritedImages.set(value.attachment.attachmentId, value.attachment)
    }
    remaining.push(...Object.values(value))
  }
  const child = ctx.sessions.prepare(params.childSessionId, {
    seed: buildForkSeed(events, params.atSeq), inheritedEventCount: events.length,
    meta: { cwd: childCwd, isSeeded: true },
  })
  for (const ref of inheritedImages.values()) {
    const stored = await ctx.attachments.readImage(ref)
    await commitPreparedImageFile(resolve(childHome, 'attachments', 'v1'), stored)
  }
  await mkdir(dirname(childLog), { recursive: true, mode: 0o700 })
  const childEvents = child.snapshotEvents()
  const handle = await open(childLog, 'wx', 0o600)
  try {
    await handle.writeFile(JSON.stringify(toHeaderLine(child.header, events.length)) + '\n')
    for (let offset = 0; offset < childEvents.length; offset += 128) {
      await handle.writeFile(eventLines(childEvents.slice(offset, offset + 128)) + '\n')
    }
    await handle.sync()
  } finally { await handle.close() }
  const logDirectory = await open(dirname(childLog), 'r')
  try { await logDirectory.sync() } finally { await logDirectory.close() }
  return { sessionId: params.childSessionId, atSeq: params.atSeq }
}
