import {renderToStaticMarkup} from 'react-dom/server';
import {describe,it,expect} from 'vitest';
import ConversationGoal,{goalChange,goalExecutionAuthorization,goalRoundStart,GoalExecutionView,GoalStateView,type GoalExecution,type NativeGoal} from './ConversationGoal';

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
  it('builds an explicit approval with separate budgets and an exact round start',()=>{
    expect(goalExecutionAuthorization(goal,'workspace-session-fixture',{maxRounds:2,tokenBudget:10000,costBudgetMicros:25,timeBudgetSeconds:60}))
      .toEqual({goal_id:goal.id,goal_revision:3,harness_session_id:'workspace-session-fixture',max_rounds:2,token_budget:10000,cost_budget_micros:25,time_budget_seconds:60,confirm:true});
    expect(goalRoundStart(goal)).toEqual({goal_id:goal.id,goal_revision:3,confirm:true});
    expect(()=>goalExecutionAuthorization(goal,null,{maxRounds:1,tokenBudget:1,costBudgetMicros:1,timeBudgetSeconds:60})).toThrow();
    expect(()=>goalExecutionAuthorization(goal,'workspace-session-fixture',{maxRounds:1,tokenBudget:0,costBudgetMicros:1,timeBudgetSeconds:60})).toThrow();
    expect(()=>goalExecutionAuthorization(null,'workspace-session-fixture',{maxRounds:1,tokenBudget:1,costBudgetMicros:1,timeBudgetSeconds:60})).toThrow();
  });
  it('shows synthetic and unknown billing without claiming production cost',()=>{
    const execution:GoalExecution={harness_session_id:'workspace-session-fixture',authorization:{id:'auth-1',goal_id:goal.id,goal_revision:3,harness_session_id:'workspace-session-fixture',max_rounds:2,token_budget:10000,cost_budget_micros:10000,time_budget_seconds:3600,status:'active',expires_at:null},budget:{rounds_used:1,rounds_executed:1,usage_complete:false,cost_authoritative:false,tokens_used:null,cost_micros_used:null,synthetic_rounds:1,unknown_rounds:[1],rounds_remaining:1,tokens_remaining:null,cost_micros_remaining:null,time_remaining_seconds:3600},active_lease:null,rounds:[{id:'lease-1',round_index:1,state:'settled',armed:true,usage_status:'unknown',usage_source:'synthetic_fixture',cost_authoritative:false,total_tokens:null,cost_micros:null,run_id:'run-1',assistant_message_id:'message-1',error_code:'USAGE_INCOMPLETE'}],can_start:false,driver_integrated:true,single_round_per_run:true,unattended_execution:false,continuation_driver:false};
    const html=renderToStaticMarkup(<GoalExecutionView execution={execution} goal={goal}/>);
    expect(html).toContain('用量未完整确认');
    expect(html).toContain('回环合成计费');
    expect(html).toContain('第 1 轮：settled');
    expect(html).not.toContain('费用来自权威回执');
  });
});
