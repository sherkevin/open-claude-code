/**
 * Proof of direct reuse of pinned DeepSeek Harness's pure scheduling domain.
 * No Host, model, shell tool, task dispatch, storage, or customer payload is used.
 * Usage: node --import <pinned-source>/node_modules/tsx/dist/esm/index.mjs
 *        scripts/oss/harness_schedule_probe.mts <pinned-source>
 */
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const source = resolve(process.argv[2] || '.');
const pin = 'fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159';
assert.equal(execFileSync('git', ['-C', source, 'rev-parse', 'HEAD'], { encoding: 'utf8' }).trim(), pin);
execFileSync('git', ['-C', source, 'diff', '--quiet', 'HEAD', '--']);
const schedule = await import(pathToFileURL(resolve(source, 'packages/schedule/schedule/src/domain.ts')).href);
const at = (value: string) => Date.parse(value);
const id = schedule.ScheduleId('oss-probe');
const create = (rule: string, zone = 'Asia/Shanghai', now = '2026-11-20T00:00:00Z') =>
  schedule.createCronScheduleRecord(id, 'Synthetic prompt', { expression: rule, time_zone: zone }, at(now), 'OSS timing probe');

const cases: string[] = [];
const check = (name: string, fn: () => void) => { fn(); cases.push(name); };
check('weekday cron next slot', () => {
  const rec = create('0 9 * * 1-5');
  assert.equal(rec.scheduledAt, '2026-11-20T01:00:00.000Z');
  assert.deepEqual(schedule.resolveRecurringOccurrence(rec, at('2026-11-25T02:00:00Z')), {
    occurrenceAt: '2026-11-25T01:00:00.000Z', nextScheduledAt: '2026-11-26T01:00:00.000Z',
  });
});
check('weekend skip', () => {
  assert.equal(create('0 9 * * 1-5', 'Asia/Shanghai', '2026-11-20T02:00:00Z').scheduledAt, '2026-11-23T01:00:00.000Z');
});
check('invalid cron and zone rejected', () => {
  assert.throws(() => create('0 0 L * *'));
  assert.throws(() => create('0 9 * * *', 'Invalid/Zone'));
});
check('bounded RRULE monthly recurrence', () => {
  const rule = 'DTSTART;TZID=Asia/Shanghai:20261101T090000\nRRULE:FREQ=MONTHLY;BYMONTHDAY=1;COUNT=3';
  const rec = schedule.createRRuleScheduleRecord(id, 'OSS recurrence', 'Synthetic prompt', rule, at('2026-10-31T00:00:00Z'));
  assert.equal(rec.scheduledAt, '2026-11-01T01:00:00.000Z');
  assert.deepEqual(schedule.resolveRecurringOccurrence(rec, at('2026-12-02T00:00:00Z')), {
    occurrenceAt: '2026-12-01T01:00:00.000Z', nextScheduledAt: '2027-01-01T01:00:00.000Z',
  });
  const last = schedule.resolveRecurringOccurrence(rec, at('2027-01-02T00:00:00Z'));
  assert.equal(last.occurrenceAt, '2027-01-01T01:00:00.000Z');
  assert.equal(last.nextScheduledAt, undefined);
});
check('unsafe frequency rejected', () => {
  assert.throws(() => schedule.normalizeRRule('DTSTART:20261101T090000Z\nRRULE:FREQ=SECONDLY'), {code:'frequency_too_high'});
});
check('daylight saving gap and overlap', () => {
  assert.equal(create('30 2 * * *', 'America/New_York', '2026-03-07T08:00:00Z').scheduledAt, '2026-03-09T06:30:00.000Z');
  assert.equal(create('30 1 * * *', 'America/New_York', '2026-10-31T06:00:00Z').scheduledAt, '2026-11-01T05:30:00.000Z');
});
console.log(JSON.stringify({upstream:pin, module:'packages/schedule/schedule/src/domain.ts', passed:cases.length, cases}));
