import {renderToStaticMarkup} from 'react-dom/server';
import {describe,it,expect} from 'vitest';
import ScheduleRunNow,{runNowPayload} from './ScheduleRunNow';
import {ScheduleRow,type Schedule} from './ConversationTasks';
const task:Schedule={id:'series-1',text:'synthetic task',scheduled_at:'2027-01-01T01:00:00Z',recurrence:'daily',status:'scheduled',edit_token:'a'.repeat(64)};
describe('immediate run',()=>{
  it('uses an explicit stable request key and plan version',()=>{
    expect(runNowPayload(task,'click-one')).toEqual({request_id:'click-one',expected_token:task.edit_token});
    expect(()=>runNowPayload({...task,status:'paused'},'click-one')).toThrow();
    expect(()=>runNowPayload({...task,series_id:'parent'},'click-one')).toThrow();
  });
  it('only offers the action on active editable plans and shows manual receipt time',()=>{
    const props={busy:false,onCancel:()=>{},onLifecycle:()=>{},onRunNow:()=>{}};
    expect(renderToStaticMarkup(<ScheduleRow task={task} {...props}/>)).toContain('>立即执行<');
    expect(renderToStaticMarkup(<ScheduleRow task={{...task,status:'paused'}} {...props}/>)).not.toContain('>立即执行<');
    expect(renderToStaticMarkup(<ScheduleRow task={{...task,status:'succeeded',manual_at:task.scheduled_at}} {...props}/>)).toContain('手动立即执行');
  });
  it('distinguishes early one-shot and extra recurring execution in confirmation',()=>{
    const props={path:'/fixture',onClose:()=>{},onQueued:()=>{}};
    expect(renderToStaticMarkup(<ScheduleRunNow task={task} {...props}/>)).toContain('额外执行一次，原周期时间保持不变');
    expect(renderToStaticMarkup(<ScheduleRunNow task={{...task,recurrence:'once'}} {...props}/>)).toContain('原时间不会再次触发');
  });
});
