import {renderToStaticMarkup} from 'react-dom/server';
import {describe,it,expect,vi,afterEach} from 'vitest';
import SchedulesPage,{catalogPath,scopedSchedulePath,type CatalogSchedule} from './SchedulesPage';
import {changeScheduleLifecycle,ScheduleRow,scheduleLifecycleConfirmation} from './ConversationTasks';

const task:CatalogSchedule={id:'series-1',conversation_id:'session-A',conversation_title:'业务会话 A',
  conversation_archived:false,kind:'series',text:'每周查询',scheduled_at:'2026-12-01T01:00:00Z',recurrence:'weekly',execution_mode:'agent',status:'paused'};

afterEach(()=>vi.unstubAllGlobals());
describe('global schedules catalog',()=>{
  it('uses bounded owner catalog with escaped cursor and explicit history filter',()=>{
    expect(catalogPath('active')).toBe('/schedules?scope=active&limit=30');
    expect(new URLSearchParams(catalogPath('history','2026-12-01T00:00:00+00:00|series-1').split('?')[1]).get('before')).toBe('2026-12-01T00:00:00+00:00|series-1');
    expect(scopedSchedulePath({...task,conversation_id:'a/b'})).toBe('/conversations/a%2Fb/schedules');
  });
  it('provides a global navigation surface and reuses row actions',()=>{
    const html=renderToStaticMarkup(<SchedulesPage onOpen={()=>{}}/>);
    expect(html).toContain('定时任务中心');
    expect(html).toContain('活动计划与任务');
    expect(html).toContain('执行历史');
    const row=renderToStaticMarkup(<ScheduleRow task={task} busy={false} onCancel={()=>{}} onLifecycle={()=>{}}/>);
    expect(row).toContain('恢复');
    expect(row).toContain('已暂停');
    expect(scheduleLifecycleConfirmation('resume')).toContain('不补跑、不重试');
  });
  it('uses the same scoped resume API with explicit failure acknowledgment',async()=>{
    const fetch=vi.fn().mockResolvedValue(new Response(JSON.stringify({id:task.id,status:'scheduled',scheduled_at:task.scheduled_at})));
    vi.stubGlobal('fetch',fetch);
    await changeScheduleLifecycle(scopedSchedulePath(task),task.id,'resume');
    expect(fetch.mock.calls[0][0]).toContain('/conversations/session-A/schedules/series-1/resume');
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({acknowledge_failure:true});
  });
});
