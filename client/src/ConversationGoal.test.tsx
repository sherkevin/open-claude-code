import {renderToStaticMarkup} from 'react-dom/server';
import {describe,it,expect} from 'vitest';
import ConversationGoal,{goalChange,GoalStateView,type NativeGoal} from './ConversationGoal';

const goal:NativeGoal={id:'goal-00000000-0000-4000-8000-000000000001',revision:3,objective:'synthetic goal',phase:'paused',maxGoalRounds:8,roundsStarted:2,activation:'disarmed',createdAt:1,updatedAt:2};
describe('native goal state UI',()=>{
  it('sends only exact version and valid explicit fields',()=>{
    expect(goalChange('create',null,' fixture ',4)).toEqual({action:'create',objective:'fixture',maxGoalRounds:4});
    expect(goalChange('edit',goal,'new fixture',5)).toEqual({action:'edit',ref:{id:goal.id,revision:3},objective:'new fixture',maxGoalRounds:5});
    expect(goalChange('resume',goal,'ignored',8)).toEqual({action:'resume',ref:{id:goal.id,revision:3}});
    expect(()=>goalChange('create',null,'x',33)).toThrow();
    expect(()=>goalChange('create',null,' ',2)).toThrow();
  });
  it('does not claim automatic work or use round count as money/token budget',()=>{
    const html=renderToStaticMarkup(<GoalStateView goal={goal}/>);
    expect(html).toContain('已暂停');
    expect(html).toContain('2 / 8');
    expect(html).toContain('自动执行驱动尚未启用');
    expect(renderToStaticMarkup(<ConversationGoal conversationId="fixture" unavailable/>)).toContain('disabled');
  });
});
