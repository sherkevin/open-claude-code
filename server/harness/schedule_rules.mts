/** Thin, side-effect-free adapter to the pinned MIT upstream rule engine.
 * The host owns identity, authorization, durable state and business dispatch.
 * No model prompt, credentials, or customer data enter this process.
 */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const input = JSON.parse(readFileSync(0, 'utf8'));
const source = resolve(process.argv[2]);
const domain = await import(pathToFileURL(resolve(source, 'packages/schedule/schedule/src/domain.ts')).href);
const at = Date.parse(input.now);
const placeholder = domain.ScheduleId('kaiwu-timing');
let output: Record<string, unknown>;
try {
  if (!Number.isFinite(at)) throw new Error('invalid reference');
  const {kind, expression, timezone} = input;
  if (kind !== 'cron' && kind !== 'rrule') throw new Error('invalid kind');
  if (input.operation === 'create') {
    const record = kind === 'cron'
      ? domain.createCronScheduleRecord(placeholder, 'Timing only', {expression, time_zone: timezone}, at, 'Timing rule')
      : domain.createRRuleScheduleRecord(placeholder, 'Timing rule', 'Timing only', expression, at);
    output = {ok:true, next:record.scheduledAt, expression:kind === 'cron' ? record.expression : record.rrule,
              timezone:kind === 'cron' ? record.timeZone : null};
  } else if (input.operation === 'resolve') {
    const record = kind === 'cron'
      ? {kind,id:placeholder,title:'Timing rule',prompt:'Timing only',expression,timeZone:timezone,scheduledAt:input.scheduled_at}
      : {kind,id:placeholder,title:'Timing rule',prompt:'Timing only',rrule:expression,scheduledAt:input.scheduled_at};
    const receipt = domain.resolveRecurringOccurrence(record,at);
    output = {ok:true, occurrence:receipt.occurrenceAt, next:receipt.nextScheduledAt ?? null};
  } else throw new Error('invalid operation');
} catch (error) {
  // Never return a stack, local paths, or raw operator input.
  output = {ok:false, code:typeof (error as any)?.code === 'string' ? (error as any).code : 'invalid_rule'};
}
process.stdout.write(JSON.stringify(output));
