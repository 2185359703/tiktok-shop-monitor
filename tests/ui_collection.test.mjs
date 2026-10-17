import test from 'node:test';
import assert from 'node:assert/strict';
import {createCollectionMonitorPoller} from '../app/frontend/collection-ui.js';
import {
  parseCollectionResourceIds,collectionErrorMessage,renderCollectionPlanForm,
  renderCollectionMonitor,renderCollectionManagement,renderCollectionRun,
  renderCollectionPlanDetail,renderCollectionConfirmation,collectionTaskActions,
} from '../app/frontend/collection-view.js';
import {collectionPlanPatch,collectionPageCursors,acceptCollectionPage,createCollectionResourcePicker} from '../app/frontend/collection-state.js';
import {parseCollectionShopUrls,renderCollectionResourceResults,renderCollectionResourceSelected,renderCollectionLaunchForm,renderCollectionLaunchDependencies} from '../app/frontend/collection-editors.js';
import {renderCollectionLaunchDetail} from '../app/frontend/collection-management-view.js';
import {renderCollectionAlerts,renderCollectionAlertHistory} from '../app/frontend/collection-alerts.js';

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

test('metadata edits do not resubmit a one-time schedule, resource order, or stale enabled state',()=>{
  const original={name:'Original',resource_ids:['9007199254740993','2'],schedule_mode:'once',interval_seconds:null,enabled:true};
  const draft={...original,name:' Renamed ',resource_ids:['2','9007199254740993']};
  assert.deepEqual(collectionPlanPatch(original,draft),{name:'Renamed'});
  assert.deepEqual(collectionPlanPatch(original,{...original}),{});
  assert.deepEqual(collectionPlanPatch(original,{...original,enabled:false}),{enabled:false});
  const interval={...original,schedule_mode:'interval',interval_seconds:3600};
  assert.deepEqual(collectionPlanPatch(interval,{...interval,name:'Only title'}),{name:'Only title'});
  assert.deepEqual(collectionPlanPatch(interval,{...interval,interval_seconds:7200}),{interval_seconds:7200});
});

test('page cursors commit only after a successful response',()=>{
  const model={items:[{id:'20'}],next:'20',cursors:[null],loaded:true,error:null};
  const proposed=collectionPageCursors(model,'next');
  assert.deepEqual(proposed,[null,'20']);assert.deepEqual(model.cursors,[null]);
  assert.deepEqual(collectionPageCursors(model,'next'),proposed);
  acceptCollectionPage(model,{items:[{id:'19'}],next_before_id:'19'},proposed);
  assert.deepEqual(model.cursors,[null,'20']);assert.equal(model.items[0].id,'19');
  assert.deepEqual(collectionPageCursors(model,'prev'),[null]);assert.equal(model.cursors.length,2);
});

test('resource selection survives search and pagination without selecting ineligible candidates',async()=>{
  const rows={a:{id:'1',name:'Shop A',eligible:true},b:{id:'2',name:'Shop B',eligible:true},bad:{id:'3',eligible:false,eligibility_reason:'invalid_source_url'}};
  const calls=[];
  const picker=createCollectionResourcePicker({taskType:'shop_profile',maximum:2,read:async input=>{
    calls.push(input);
    return input.query?{items:[rows.b,rows.bad],next_before_id:null}:input.beforeId?{items:[rows.b],next_before_id:null}:{items:[rows.a],next_before_id:'1'};
  }});
  await picker.load();picker.select(rows.a,true);
  await picker.load({direction:'next'});assert.equal(calls.at(-1).beforeId,'1');
  await picker.load({query:'Shop B'});assert.equal(calls.at(-1).beforeId,null);
  picker.select(rows.b,true);assert.equal(picker.select(rows.bad,true),false);
  assert.deepEqual([...picker.state.selected.keys()],['1','2']);
  assert.equal(picker.select({id:'4',eligible:true},true),false);
  const html=renderCollectionResourceResults(picker.state);
  assert.match(html,/data-collection-resource="3"[^>]*disabled/);assert.match(html,/缺少有效来源链接/);
  picker.remove('1');assert.deepEqual([...picker.state.selected.keys()],['2']);picker.stop();
});

test('newer resource searches own their response and leaving the editor cancels reads',async()=>{
  const calls=[];
  const picker=createCollectionResourcePicker({taskType:'product_detail',read:input=>new Promise(resolve=>calls.push({...input,resolve}))});
  const old=picker.load({query:'old'}),recent=picker.load({query:'new'});
  assert.equal(calls[0].signal.aborted,true);
  calls[1].resolve({items:[{id:'2',eligible:true}],next_before_id:null});await recent;
  calls[0].resolve({items:[{id:'1',eligible:true}],next_before_id:null});await old;
  assert.equal(picker.state.items[0].id,'2');assert.equal(picker.state.resultQuery,'new');
  const leaving=picker.load({query:'late'});picker.stop();assert.equal(calls[2].signal.aborted,true);
  calls[2].resolve({items:[{id:'3',eligible:true}]});await leaving;assert.equal(picker.state.items[0].id,'2');
});

test('failed resource search retains its page and retry starts at the first page of the new search',async()=>{
  let fail=false;const calls=[];
  const picker=createCollectionResourcePicker({taskType:'shop_profile',read:async input=>{
    calls.push(input);if(fail)throw Error('private detail');
    return {items:[{id:input.beforeId?'19':'20',eligible:true}],next_before_id:input.beforeId?null:'20'};
  }});
  await picker.load();await picker.load({direction:'next'});fail=true;
  await picker.load({query:'new'});assert.deepEqual(picker.state.cursors,[null,'20']);assert.equal(picker.state.items[0].id,'19');
  assert.match(renderCollectionResourceResults(picker.state),/data-collection-action="resource-prev" disabled/);
  fail=false;await picker.load();assert.equal(calls.at(-1).beforeId,null);assert.deepEqual(picker.state.cursors,[null]);picker.stop();
});

test('selected IDs resolve independently and missing market resources remain visible for correction',async()=>{
  const picker=createCollectionResourcePicker({taskType:'product_snapshot',selectedIds:['1','2'],read:async input=>input.ids?{items:[{id:'1',name:'Known',eligible:true}]}:{items:[]}});
  await picker.resolveSelected();assert.equal(picker.state.selected.get('1').name,'Known');
  assert.equal(picker.state.selected.get('2').eligibility_reason,'resource_missing');
  assert.match(renderCollectionResourceSelected(picker.state),/资源不存在或不属于当前市场/);
  await picker.replace(['1']);assert.equal(picker.state.selected.size,1);
  picker.setType('product_detail');await new Promise(resolve=>setImmediate(resolve));
  assert.equal(picker.state.selected.size,0);picker.setType('product_snapshot');assert.equal(picker.state.selected.size,1);picker.stop();
});

test('archive remains reversible and manual run explains its scheduling effect',()=>{
  const plan={id:'2',name:'Plan',resource_ids:['1'],task_type:'shop_profile',schedule_mode:'once',enabled:false,archived_at:'2026-10-17T00:00:00Z'};
  const html=renderCollectionPlanDetail(plan,{items:[],cursors:[null]},true);
  assert.match(html,/已归档/);assert.match(html,/data-collection-action="restore"/);assert.doesNotMatch(html,/data-collection-action="run"/);
  assert.match(renderCollectionConfirmation('run',{...plan,archived_at:null},false),/一次性计划将自动停用/);
  assert.match(renderCollectionConfirmation('run',{...plan,schedule_mode:'interval',enabled:true},false),/重新计算一个间隔/);
  assert.match(renderCollectionConfirmation('restore',plan,false),/保持停用/);
});

test('shop entry input accepts only supported public HTTPS stores and removes tracking data',()=>{
  const base='https://shop.tiktok.com/us/store/store/123';
  assert.deepEqual(parseCollectionShopUrls(`${base}?tracking=value#fragment\n${base}`),[base]);
  assert.deepEqual(parseCollectionShopUrls('https://www.tiktok.com/shop/store/store/123'),['https://www.tiktok.com/shop/store/store/123']);
  for(const value of ['', 'http://shop.tiktok.com/us/store/store/123','https://localhost/us/store/a/1','https://user:pass@shop.tiktok.com/us/store/a/1','https://shop.tiktok.com/us/k/category','https://shop.tiktok.com/gb/store/a/1'])assert.throws(()=>parseCollectionShopUrls(value));
  assert.throws(()=>parseCollectionShopUrls(`${base}\nhttps://shop.tiktok.com/us/store/a/456`,1),/最多导入/);
});

test('single collection shows reused roots, new descendants, pagination, and enabled launch choices',()=>{
  const form=renderCollectionLaunchForm({launch_task_types:['product_detail'],limits:{max_resources:20}},{});
  assert.match(form,/value="shop_discovery"[^>]*disabled/);assert.match(form,/value="product_detail" selected/);
  const html=renderCollectionLaunchDetail({id:'1',name:'<Store>',task_type:'product_detail',status:'running',requested_count:1,created_task_count:0,reused_task_count:1,root_task_count:1,descendant_task_count:1},{items:[{task_id:'root',member_role:'root',task_type:'product_detail',status:'succeeded'},{task_id:'child',member_role:'descendant',task_type:'change_detect',status:'running'}],cursors:[null],next:'7'},()=> '');
  assert.match(html,/&lt;Store&gt;/);assert.match(html,/直接任务/);assert.match(html,/后续任务/);assert.match(html,/不回溯其已有后续任务/);assert.match(html,/data-collection-action="launch-tasks-next"/);
});

test('minute rollup warming and process connectivity do not claim zero throughput or ready workers',()=>{
  const html=renderCollectionMonitor({windowMinutes:60,taskGroup:'source',polling:true,data:{statistics:{ready:false,status:'warming'},attempts:{total:null,failed:null,success_rate:null,throughput_per_minute:null},queue:{status:'pending'},runtime:{status:'available',components:[{id:'worker',role:'worker',status:'online',state:'connecting',ready:false}],ready_counts:{worker:0,scheduler:0}},trend:[]}},()=> '');
  assert.match(html,/分钟统计正在准备/);assert.match(html,/来源采集吞吐/);assert.match(html,/未就绪/);assert.doesNotMatch(html,/>运行就绪</);assert.match(html,/class="kpi-value">—<small>\/ 分钟/);assert.doesNotMatch(html,/窗口内无失败记录/);
});

test('monitor counts abandoned tasks once in waiting and retry, while only dead tasks are terminal failures',()=>{
  const counts={product_detail:{pending:2,retry_waiting:3,abandoned:5,running:7,succeeded:11,dead:13},shop_profile:{abandoned:17,dead:19}};
  const byType=Object.entries(counts).flatMap(([task_type,statuses])=>Object.entries(statuses).map(([status,count])=>({task_type,status,count})));
  const html=renderCollectionMonitor({windowMinutes:60,taskGroup:'source',polling:true,data:{
    queue:{status:'ready',total:77,by_status:{pending:2,retry_waiting:3,abandoned:22,running:7,succeeded:11,dead:32},by_type:byType},
    runtime:{status:'unavailable'},leases:{expired:4,items:[{task_id:'expired-task',task_type:'product_detail',expired:true}]},
  }},()=> '');
  assert.match(html,/kpi-label">等待执行 \/ 重试<\/div><div class="kpi-value">27<\/div>/);
  const table=html.slice(html.indexOf('<h2 class="panel-title">任务队列</h2>')).split('</section>')[0];
  const values=[...table.matchAll(/<td class="num">(\d+)<\/td>/g)].map(match=>Number(match[1]));
  assert.deepEqual(values,[10,7,11,13,17,0,0,19]);
  assert.equal(values[0]+values[4],27);
  assert.equal(values[3]+values[7],32);
  assert.equal(values.reduce((sum,count)=>sum+count,0),77);
  assert.match(table,/<th>等待 \/ 重试<\/th>/);assert.match(table,/<th>失败终止<\/th>/);
  assert.doesNotMatch(table,/失败 \/ 失效/);
  assert.match(html,/租约过期 4 个/);assert.match(html,/租约已过期的任务/);
});

test('source and local monitor groups cancel stale responses and explain approximate percentiles',async()=>{
  const calls=[];const poller=createCollectionMonitorPoller({read:(minutes,options)=>new Promise(resolve=>calls.push({minutes,...options,resolve})),schedule:()=>1,cancel:()=>{}});
  poller.activate(true);const first=poller.refresh();poller.setGroup('local');
  assert.equal(calls[0].signal.aborted,true);assert.equal(calls[1].taskGroup,'local');
  calls[1].resolve({statistics:{ready:true,p95_approximate:true,p95_max_relative_error:.1},attempts:{p95_duration_ms:1100},runtime:{status:'unavailable'}});await new Promise(resolve=>setImmediate(resolve));
  calls[0].resolve({attempts:{total:9999}});await first;
  const html=renderCollectionMonitor(poller.state,()=> '');assert.match(html,/本地处理吞吐/);assert.match(html,/P95近似上界/);assert.match(html,/最大相对误差约 10%/);assert.doesNotMatch(html,/9,999/);poller.stop();
});

test('supported launch types remain disabled when their collector is not enabled',()=>{
  const form=renderCollectionLaunchForm({launch_task_types:['shop_discovery','product_detail'],enabled_task_types:['product_detail'],limits:{max_resources:20}});
  assert.match(form,/value="shop_discovery"[^>]*disabled/);assert.match(form,/value="product_detail" selected/);
});

test('shop intake warns about disabled downstream collectors while keeping the root submission available',()=>{
  const form=renderCollectionLaunchForm({launch_task_types:['shop_discovery'],enabled_task_types:['shop_discovery'],limits:{max_resources:20}});
  assert.match(form,/后续商品发现、商品详情、店铺档案未启用/);
  assert.match(form,/相关任务将等待服务器启用/);
  assert.match(form,/<button type="submit" class="button primary">提交采集任务<\/button>/);
  assert.doesNotMatch(form,/value="shop_discovery"[^>]*disabled/);
});

test('downstream hints follow the chosen root type and omit collectors already enabled',()=>{
  const control={enabled_task_types:['shop_discovery','product_discovery','shop_profile']};
  assert.match(renderCollectionLaunchDependencies('shop_discovery',control),/后续商品详情未启用/);
  assert.doesNotMatch(renderCollectionLaunchDependencies('shop_discovery',control),/商品发现、|店铺档案/);
  assert.match(renderCollectionLaunchDependencies('product_discovery',control),/后续商品详情未启用/);
  assert.equal(renderCollectionLaunchDependencies('product_detail',control),'');
  assert.equal(renderCollectionLaunchDependencies('product_reviews',control),'');
  assert.equal(renderCollectionLaunchDependencies('shop_discovery',{enabled_task_types:[...control.enabled_task_types,'product_detail']}),'');
});

test('batch cancellation explains running and shared work without erasing completion history',()=>{
  const launch={id:'1',name:'Example',status:'cancelling',cancel_requested_at:'2026-10-17T00:00:00Z'};
  const html=renderCollectionLaunchDetail(launch,{items:[],cursors:[null]},()=>'',true);
  assert.match(html,/取消中/);assert.match(html,/继续取消待执行任务/);assert.match(html,/其它有效批次共享的任务继续执行/);assert.match(html,/批次会在关联任务结束后完成取消/);
  const confirmation=renderCollectionConfirmation('launch-cancel',launch,false);
  assert.match(confirmation,/正在运行的请求允许完成/);assert.match(confirmation,/其它有效批次共享的任务继续执行/);assert.match(confirmation,/任务记录保留/);
  const completed=renderCollectionLaunchDetail({...launch,status:'succeeded',cancel_requested_at:null},{items:[],cursors:[null]},()=>'',true);
  assert.doesNotMatch(completed,/data-collection-action="launch-cancel"/);assert.match(completed,/已完成/);
});

test('a cancelled batch disables retries without disabling the same shared task in another batch or global details',()=>{
  const task={task_id:'shared-task',task_type:'product_detail',status:'dead',member_role:'root',resource_type:'product',resource_id:'9007199254740993'};
  const model={items:[task],cursors:[null]};
  const actions=(item,options)=>collectionTaskActions(item,true,options);
  const cancelled=renderCollectionLaunchDetail({id:'1',status:'cancelled',cancel_requested_at:'2026-10-17T00:00:00Z'},model,actions,true);
  assert.match(cancelled,/data-collection-action="retry-task" data-id="shared-task" disabled/);
  assert.match(cancelled,/本批次不再新增采集；共享任务若需补采请从仍有效批次操作/);
  assert.match(cancelled,/商品 · 内部 ID 9007199254740993/);
  const active=renderCollectionLaunchDetail({id:'2',status:'failed',cancel_requested_at:null},model,actions,true);
  assert.match(active,/data-collection-action="retry-task" data-id="shared-task"/);
  assert.doesNotMatch(active,/data-collection-action="retry-task" data-id="shared-task" disabled/);
  assert.doesNotMatch(collectionTaskActions(task,true),/disabled/);
});

test('single collection task resource context is escaped before rendering',()=>{
  const html=renderCollectionLaunchDetail({id:'1',status:'queued'},{items:[{task_id:'task',task_type:'product_detail',status:'pending',resource_type:'<source>',resource_id:'<img src=x onerror=alert(1)>'}],cursors:[null]},()=>'',true);
  assert.match(html,/&lt;source&gt; · 内部 ID &lt;img src=x onerror=alert\(1\)&gt;/);
  assert.doesNotMatch(html,/<img|<source>/);
});

test('persistent alerts distinguish stale evaluation, unknown conditions, acknowledgement, and recovery history',()=>{
  const alert={id:'1',message:'<Worker unavailable>',code:'worker_unavailable',status:'open',condition_known:false,occurrences:2};
  const model={items:[alert],cursors:[null],status:'active',evaluation:{status:'stale',last_evaluated_at:'2026-10-17T00:00:00Z'}};
  const readOnly=renderCollectionAlerts(model,false,()=> '');
  assert.match(readOnly,/&lt;Worker unavailable&gt;/);assert.match(readOnly,/告警评估更新延迟/);assert.match(readOnly,/待确认/);assert.match(readOnly,/data-collection-action="alert-ack" data-id="1" disabled/);
  assert.match(readOnly,/固定使用来源采集最近 60 分钟评估/);assert.match(readOnly,/连接写入令牌/);
  const history=renderCollectionAlertHistory({...alert,status:'acknowledged'},{items:[{event_type:'reopened',status:'open',occurrence:2}],cursors:[null],evaluation:{status:'fresh'}},true);
  assert.match(history,/再次发生/);assert.match(history,/保留原状态等待下一次有效评估/);assert.doesNotMatch(history,/data-collection-action="alert-ack"/);
  assert.match(renderCollectionConfirmation('alert-ack',alert,false),/确认不代表故障已解决/);
});
