import {renderToStaticMarkup} from 'react-dom/server';
import {describe,it,expect} from 'vitest';
import {scheduleEditPayload,localDateTime} from './ScheduleEditor';
import {ScheduleRow,type Schedule} from './ConversationTasks';

const task:Schedule={id:'series-1',text:'old',status:'paused',scheduled_at:'2027-01-01T01:00:00Z',recurrence:'cron',rule_expression:'0 9 * * 1-5',timezone:'Asia/Shanghai',edit_token:'a'.repeat(64)};
describe('schedule editor shared contract',()=>{
  it('does not reset rule or time for a text-only edit',()=>{
    expect(scheduleEditPayload(task,' new ',false,'','0 9 * * 1-5','Asia/Shanghai')).toEqual({text:'new',expected_token:task.edit_token});
  });
  it('sends a full official rule and timezone only when user selects retiming',()=>{
    expect(scheduleEditPayload(task,'new',true,'','15 10 * * 1-5','Asia/Shanghai')).toEqual({text:'new',expected_token:task.edit_token,rule_expression:'15 10 * * 1-5',timezone:'Asia/Shanghai'});
    expect(()=>scheduleEditPayload(task,'new',true,'','','Asia/Shanghai')).toThrow();
  });
  it('uses browser local time but sends an absolute future instant',()=>{
    const later=new Date(Date.now()+86400000);
    later.setSeconds(0,0);
    const value=localDateTime(later.toISOString());
    const once={...task,recurrence:'once'};
    expect(scheduleEditPayload(once,'new',true,value,'','').scheduled_at).toBe(later.toISOString());
    expect(()=>scheduleEditPayload({...once,edit_token:undefined},'new',false,'','','')).toThrow();
  });
  it('shows edit only for rows with authoritative edit tokens',()=>{
    const props={busy:false,onCancel:()=>{},onLifecycle:()=>{},onEdit:()=>{}};
    expect(renderToStaticMarkup(<ScheduleRow task={task} {...props}/>)).toContain('>编辑<');
    expect(renderToStaticMarkup(<ScheduleRow task={{...task,edit_token:undefined}} {...props}/>)).not.toContain('>编辑<');
  });
});
