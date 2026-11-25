import {renderToStaticMarkup} from 'react-dom/server';
import {describe,expect,it} from 'vitest';
import ConversationFeedback,{feedbackForm,feedbackImageProblem} from './ConversationFeedback';

describe('session-scoped explicit feedback',()=>{
  it('offers a session feedback action without uploading anything on render',()=>{
    const html=renderToStaticMarkup(<ConversationFeedback conversationId="session-1" runId="run-1" messages={[]}/>);
    expect(html).toContain('aria-label="问题反馈"');
    expect(html).not.toContain('上传截图');
  });
  it('posts only explicit content and a selected message id, never the chat text or unrelated run',()=>{
    const file=new File(['fixture'],'shot.png',{type:'image/png'});
    const form=feedbackForm(' display issue ','request-1','message:m-2','run-1',[file]);
    expect([...form.keys()]).toEqual(['description','request_id','message_id','screenshots']);
    expect(form.get('description')).toBe('display issue');
    expect(form.get('message_id')).toBe('m-2');
    expect(form.get('run_id')).toBeNull();
    expect(feedbackForm('issue','request-2','current','run-1',[]).get('run_id')).toBe('run-1');
    expect(feedbackForm('issue','request-3','session','run-1',[]).get('run_id')).toBeNull();
  });
  it('bounds screenshot count and accepts only raster image mime types',()=>{
    const png=new File(['fixture'],'shot.png',{type:'image/png'});
    expect(feedbackImageProblem([png])).toBe('');
    expect(feedbackImageProblem([png,png,png,png])).toContain('3');
    expect(feedbackImageProblem([new File(['bad'],'bad.svg',{type:'image/svg+xml'})])).toContain('PNG');
  });
});
