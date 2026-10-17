import test from 'node:test';
import assert from 'node:assert/strict';
import {createCollectionMonitorPoller} from '../app/frontend/collection-ui.js';
import {
  parseCollectionResourceIds,collectionErrorMessage,renderCollectionPlanForm,
  renderCollectionMonitor,renderCollectionManagement,renderCollectionRun,
} from '../app/frontend/collection-view.js';

test('collection resources preserve full unsigned bigint IDs and deduplicate before submission',()=>{
  assert.deepEqual(parseCollectionResourceIds('9007199254740993, 18446744073709551615\n12，12'),[
    '9007199254740993','18446744073709551615','12',
  ]);
  for(const invalid of ['', '0', '-1', '1e3', '12.1', '01', '18446744073709551616', 'https://example.test/shop/12']) {
    assert.throws(()=>parseCollectionResourceIds(invalid));
  }
  assert.throws(()=>parseCollectionResourceIds('1,2,3',2),/最多选择 2/);
});

test('server write errors cannot expose arbitrary server details',()=>{
  assert.equal(collectionErrorMessage({status:422,code:'invalid_plan_resources',message:'private-payload'}),'部分资源不存在或不属于当前市场，请核对内部 ID。');
  assert.doesNotMatch(collectionErrorMessage({status:500,message:'private-payload'}),/private-payload/);
});

test('new plans prevent selecting a collector disabled by the running service',()=>{
  const control={enabled_task_types:['shop_profile'],limits:{max_resources:100,min_interval_seconds:{shop_profile:3600},max_interval_seconds:604800}};
  const html=renderCollectionPlanForm(null,control);
  assert.match(html,/value="product_snapshot"[^>]*disabled/);
  assert.match(html,/商品快照（未启用）/);
  assert.match(html,/name="name" maxlength="80"/);
  assert.match(html,/name="interval_minutes" type="number" disabled/);
  const plan={id:'1',name:'Paused collector',task_type:'product_snapshot',resource_ids:['3'],enabled:true,schedule_mode:'once'};
  const page=renderCollectionManagement({control:{...control,writes_enabled:true,paused:false},tokenConnected:true,tab:'plans',plans:{items:[plan],cursors:[null],loaded:true}},()=> '');
  assert.match(page,/data-collection-action="run" data-id="1" disabled/);
  assert.match(page,/采集器未启用/);
});

test('monitor ignores a late response after navigation and aborts its request',async()=>{
  let resolve,signal,scheduled=0;
  const poller=createCollectionMonitorPoller({
    read:(_minutes,options)=>{signal=options.signal;return new Promise(done=>resolve=done);},
    schedule:()=>++scheduled,cancel:()=>{},
  });
  poller.activate(true);
  const pending=poller.refresh();
  poller.activate(false);
  assert.equal(signal.aborted,true);
  resolve({revision:1});await pending;
  assert.equal(poller.state.data,null);
  assert.equal(poller.state.loading,false);
  assert.equal(scheduled,0);
});

test('run detail keeps failed root history visible beside derived retries and safe dispatch failures',()=>{
  const html=renderCollectionRun({id:'1',plan_id:'2',trigger:'manual',status:'failed',requested_count:1,task_count:1,
    tasks:[{task_id:'original-task',task_type:'shop_profile',status:'dead',resource_id:'7',attempt_count:1}],
    retry_tasks:[{task_id:'retry-task',task_type:'shop_profile',status:'running',resource_id:'7',attempt_count:1}],
    retry_task_count:1,active_retry_count:1,status_counts:{dead:1}},true);
  assert.match(html,/派生重试任务/);assert.match(html,/data-id="original-task"/);assert.match(html,/data-id="retry-task"/);
  assert.match(html,/重试成功不改写最初派发任务的历史状态/);
  const failed=renderCollectionRun({id:'3',plan_id:'2',status:'failed',task_count:0,error_code:'invalid_plan_resources'},true);
  assert.match(failed,/部分资源不存在/);assert.match(failed,/本次运行未能派发任务/);
});

test('monitor retains last good data when a refresh fails and labels it stale',async()=>{
  let fail=false;
  const poller=createCollectionMonitorPoller({
    read:async()=>{if(fail)throw Error('private diagnostic');return {generated_at:'2026-10-17T00:00:00Z',queue:{status:'pending'},runtime:{status:'unavailable'},attempts:{}};},
    schedule:()=>1,cancel:()=>{},
  });
  poller.activate(true);await poller.refresh();fail=true;await poller.refresh();
  assert.ok(poller.state.data);
  assert.ok(poller.state.error);
  const html=renderCollectionMonitor(poller.state,()=> '');
  assert.match(html,/当前显示上次结果/);
  assert.match(html,/无法判断进程在线状态/);
  assert.doesNotMatch(html,/private diagnostic|心跳超时/);
  poller.stop();
});

test('hidden pages do not read or retain polling timers, paused polling stays paused on visibility return',async()=>{
  let hidden=true,calls=0,scheduled=0,cancelled=0;
  const poller=createCollectionMonitorPoller({
    read:async()=>{calls++;return {revision:calls};},isHidden:()=>hidden,
    schedule:()=>++scheduled,cancel:()=>cancelled++,
  });
  poller.activate(true);await poller.refresh();assert.equal(calls,0);
  hidden=false;await poller.refresh();assert.equal(calls,1);assert.equal(scheduled,1);
  hidden=true;poller.visibilityChanged();assert.equal(cancelled,1);
  poller.toggle();assert.equal(poller.state.polling,false);
  hidden=false;poller.visibilityChanged();assert.equal(calls,1);
  poller.stop();
});

test('switching monitor windows cancels the old response and keeps the new window authoritative',async()=>{
  const requests=[];
  const poller=createCollectionMonitorPoller({
    read:(minutes,{signal})=>new Promise(resolve=>requests.push({minutes,signal,resolve})),
    schedule:()=>1,cancel:()=>{},
  });
  poller.activate(true);
  const first=poller.refresh();poller.setWindow(15);
  assert.equal(requests[0].signal.aborted,true);
  requests[1].resolve({window:{minutes:15}});
  await new Promise(resolve=>setImmediate(resolve));
  requests[0].resolve({window:{minutes:60}});await first;
  assert.equal(poller.state.windowMinutes,15);
  assert.equal(poller.state.data.window.minutes,15);
  poller.stop();
});
