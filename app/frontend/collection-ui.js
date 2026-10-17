import {
  collectionErrorMessage,collectionTaskActions,parseCollectionResourceIds,
  renderCollectionManagement,renderCollectionMonitor,renderCollectionAuth,
  renderCollectionPlanForm,renderCollectionPlanDetail,renderCollectionRun,
  renderCollectionConfirmation,renderCollectionReadError,
} from './collection-view.js';
import {collectionPlanPatch,collectionPageCursors,acceptCollectionPage,createCollectionResourcePicker} from './collection-state.js';
import {renderCollectionLaunchForm,renderCollectionLaunchDependencies,renderCollectionResourceSelected,renderCollectionResourceResults,parseCollectionShopUrls} from './collection-editors.js';
import {renderCollectionLaunchDetail} from './collection-management-view.js';
import {renderCollectionAlerts,renderCollectionAlertHistory} from './collection-alerts.js';

const ROOT='/api/v1/collection-management';
const pageModel=()=>({items:[],next:null,cursors:[null],loading:false,loaded:false,error:null,pendingReset:false});
const encode=value=>encodeURIComponent(String(value));
const pageQuery=(path,cursors,filters={})=>{
  const params=new URLSearchParams({limit:'20'});
  if(cursors.at(-1))params.set('before_id',cursors.at(-1));
  for(const [key,value] of Object.entries(filters))if(value!=='')params.set(key,String(value));
  return `${path}?${params}`;
};
function requestId() {
  if(globalThis.crypto.randomUUID)return globalThis.crypto.randomUUID();
  const bytes=globalThis.crypto.getRandomValues(new Uint8Array(16));
  bytes[6]=bytes[6]&15|64;bytes[8]=bytes[8]&63|128;
  const hex=Array.from(bytes,value=>value.toString(16).padStart(2,'0')).join('');
  return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
}

/** Only the visible page owns a polling request. Late responses cannot replace it. */
export function createCollectionMonitorPoller({read,onChange=()=>{},isHidden=()=>false,schedule=setTimeout,cancel=clearTimeout,interval=15000}) {
  const state={data:null,error:null,loading:false,polling:true,windowMinutes:60,taskGroup:'source'};
  let active=false,controller=null,timer=null;
  function stop(){if(timer!==null)cancel(timer);timer=null;controller?.abort();controller=null;state.loading=false;}
  async function refresh(){
    stop();if(!active||isHidden())return;
    const current=new AbortController();controller=current;state.loading=true;onChange();
    try{
      const data=await read(state.windowMinutes,{signal:current.signal,taskGroup:state.taskGroup});
      if(controller!==current||current.signal.aborted)return;
      state.data=data;state.error=null;
    }catch(error){if(controller!==current||current.signal.aborted)return;state.error=collectionErrorMessage(error);}
    finally{if(controller===current&&!current.signal.aborted){state.loading=false;controller=null;onChange();if(active&&state.polling&&!isHidden())timer=schedule(refresh,interval);}}
  }
  return {state,refresh,stop,
    activate(value){active=value;if(!active)stop();},
    visibilityChanged(){if(isHidden())stop();else if(active&&state.polling)refresh();},
    toggle(){state.polling=!state.polling;if(state.polling)refresh();else stop();onChange();},
    setWindow(value){if(![15,60,360,1440].includes(value))return;state.windowMinutes=value;state.data=null;refresh();},
    setGroup(value){if(!['source','local','all'].includes(value))return;state.taskGroup=value;state.data=null;refresh();},
  };
}

export function createCollectionWorkspace({request,write,getPage,render,toast,openDialog,closeDialog,openTask,icon}) {
  const state={control:null,plans:pageModel(),launches:pageModel(),audit:pageModel(),alerts:{...pageModel(),status:'active',evaluation:null},tab:'plans',filters:{q:'',task_type:'',enabled:'',archived:'false'},launchType:'',tokenConnected:false,busy:false,error:null,loaded:false,loading:false};
  const drafts=new Map(),commands=new Map(),planLocks=new Set();
  let token='',controller=null,alertController=null,dialogState=null,mutationDialog=null,filterTimer=null;
  const $=id=>document.getElementById(id);
  const canWrite=()=>Boolean(token&&state.control?.writes_enabled);
  function draw(){
    const active=document.activeElement,focusId=active?.id==='collection-plan-query'?active.id:null;
    const selection=focusId?[active.selectionStart,active.selectionEnd]:null;
    render();
    if(focusId&&$(focusId)){ $(focusId).focus({preventScroll:true});try{$(focusId).setSelectionRange(...selection);}catch{} }
  }
  const monitor=createCollectionMonitorPoller({
    read:(minutes,{taskGroup,...options})=>request(`/api/v1/collections/monitor?window_minutes=${minutes}&task_group=${taskGroup}`,options),
    isHidden:()=>document.hidden,onChange:()=>{if(getPage()==='collection-monitor'){draw();if(!monitor.state.loading&&!state.alerts.loading)refreshAlerts();}},
  });

  async function refreshAlerts({direction=null,reset=false}={}) {
    if(getPage()!=='collection-monitor'||document.hidden)return;
    alertController?.abort();const current=new AbortController();alertController=current;
    const model=state.alerts,cursors=reset||model.pendingReset?[null]:collectionPageCursors(model,direction);
    model.loading=true;model.error=null;draw();
    try{
      const data=await request(pageQuery('/api/v1/collections/alerts',cursors,{status:model.status}),{signal:current.signal});
      if(current.signal.aborted||alertController!==current)return;
      acceptCollectionPage(model,data,cursors);model.evaluation=data.evaluation;model.pendingReset=false;
    }catch(error){if(!current.signal.aborted)model.error=collectionErrorMessage(error);}
    finally{if(alertController===current&&!current.signal.aborted){model.loading=false;if(getPage()==='collection-monitor')draw();}}
  }

  async function refreshManagement({direction=null,reset=false}={}) {
    controller?.abort();const current=new AbortController();controller=current;
    const tab=state.tab,model=state[tab],cursors=reset||model.pendingReset?[null]:collectionPageCursors(model,direction);
    const filters=tab==='plans'?{...state.filters}:tab==='launches'?{task_type:state.launchType}:{};
    state.loading=true;model.loading=true;model.error=null;state.error=null;draw();
    const results=await Promise.allSettled([
      request(`${ROOT}/control`,{signal:current.signal}),
      request(pageQuery(`${ROOT}/${tab}`,cursors,filters),{signal:current.signal}),
    ]);
    if(current.signal.aborted||controller!==current)return;
    if(results[0].status==='fulfilled'){state.control=results[0].value;state.loaded=true;}
    else state.error=collectionErrorMessage(results[0].reason);
    if(results[1].status==='fulfilled'){acceptCollectionPage(model,results[1].value,cursors);model.pendingReset=false;}
    else{model.error=collectionErrorMessage(results[1].reason);state.error=model.error;}
    model.loading=false;state.loading=false;draw();
  }

  function saveDraft(detail=dialogState){
    if(!detail?.draftKey||!$('collection-name'))return;
    const type=$('collection-type').value;
    const draft={name:$('collection-name').value,task_type:type,resource_ids:[...(detail.picker?.state.selected.keys()||[])]};
    if(detail.kind==='form')Object.assign(draft,{schedule_mode:$('collection-schedule').value,interval_seconds:Number($('collection-interval').value)*60,enabled:$('collection-enabled').checked});
    else Object.assign(draft,{shop_urls:$('collection-shop-urls').value.split('\n'),max_pages:Number($('collection-max-pages').value)});
    detail.draft=draft;
    drafts.set(detail.draftKey,{draft,original:detail.plan,requestId:detail.requestId,submittedBody:detail.submittedBody,uncertain:detail.uncertain});
  }
  function releaseDialog(){saveDraft();clearTimeout(dialogState?.searchTimer);dialogState?.picker?.stop();dialogState?.readController?.abort();}
  function show(html,kind){
    if(state.busy&&mutationDialog)return null;
    releaseDialog();
    const signal=openDialog('COLLECTION / 采集管理');
    $('detail-dialog').classList.add('collection-dialog');$('dialog-content').innerHTML=html;
    dialogState={kind,signal};return dialogState;
  }
  function formError(message){
    const element=$('collection-form-error');
    if(element){
      element.textContent=message;element.hidden=false;
      if(dialogState?.uncertain&&dialogState.submittedBody){const retry=document.createElement('button');retry.type='button';retry.className='button';retry.dataset.collectionAction='retry-submission';retry.textContent='重试原提交';element.append(' ',retry);}
    }else toast(message);
  }
  function showReadError(detail,title,action,id,error){
    if(dialogState!==detail||detail.signal.aborted)return;
    $('dialog-content').innerHTML=renderCollectionReadError(title,action,id,collectionErrorMessage(error));
  }

  function drawPlan(detail){if(dialogState===detail&&!detail.signal.aborted)$('dialog-content').innerHTML=renderCollectionPlanDetail(detail.plan,detail.runs,canWrite());}
  async function loadRuns(detail,direction=null){
    const cursors=collectionPageCursors(detail.runs,direction);
    detail.readController?.abort();const current=new AbortController();detail.readController=current;
    const signal=AbortSignal.any([detail.signal,current.signal]);
    detail.runs.loading=true;detail.runs.error=null;drawPlan(detail);
    try{const data=await request(pageQuery(`${ROOT}/plans/${encode(detail.plan.id)}/runs`,cursors),{signal});if(signal.aborted||dialogState!==detail)return;acceptCollectionPage(detail.runs,data,cursors);}
    catch(error){if(!signal.aborted)detail.runs.error=collectionErrorMessage(error);}
    finally{if(!signal.aborted){detail.runs.loading=false;drawPlan(detail);}}
  }
  async function showPlan(id){
    const detail=show('<h2 id="dialog-title">采集计划</h2><div class="loading-state compact"><span class="loader"></span><p>正在读取计划…</p></div>','plan');if(!detail)return;
    try{detail.plan=await request(`${ROOT}/plans/${encode(id)}`,{signal:detail.signal});if(detail.signal.aborted||dialogState!==detail)return;detail.runs=pageModel();await loadRuns(detail);}
    catch(error){showReadError(detail,'采集计划暂不可用','plan',id,error);}
  }
  async function showRun(id){
    const detail=show('<h2 id="dialog-title">执行记录</h2><div class="loading-state compact"><span class="loader"></span></div>','run');if(!detail)return;
    try{detail.run=await request(`${ROOT}/runs/${encode(id)}`,{signal:detail.signal});if(detail.signal.aborted||dialogState!==detail)return;$('dialog-content').innerHTML=renderCollectionRun(detail.run,canWrite());}
    catch(error){showReadError(detail,'执行记录暂不可用','run-detail',id,error);}
  }
  async function loadLaunch(detail,direction=null){
    const cursors=collectionPageCursors(detail.tasks,direction);
    detail.readController?.abort();const current=new AbortController();detail.readController=current;
    const signal=AbortSignal.any([detail.signal,current.signal]);detail.tasks.loading=true;detail.tasks.error=null;
    if(detail.launch)$('dialog-content').innerHTML=renderCollectionLaunchDetail(detail.launch,detail.tasks,(task,options)=>collectionTaskActions(task,canWrite(),options),canWrite());
    try{
      const data=await request(pageQuery(`${ROOT}/launches/${encode(detail.id)}`,cursors),{signal});
      if(signal.aborted||dialogState!==detail)return;
      detail.launch=data.launch||data;
      acceptCollectionPage(detail.tasks,{items:Array.isArray(data.tasks)?data.tasks:data.tasks?.items||data.items||[],next_before_id:data.next_before_id??data.tasks?.next_before_id},cursors);
    }catch(error){if(!signal.aborted){detail.tasks.error=collectionErrorMessage(error);if(!detail.launch)showReadError(detail,'单次采集暂不可用','launch-detail',detail.id,error);}}
    finally{if(!signal.aborted&&dialogState===detail){detail.tasks.loading=false;if(detail.launch)$('dialog-content').innerHTML=renderCollectionLaunchDetail(detail.launch,detail.tasks,(task,options)=>collectionTaskActions(task,canWrite(),options),canWrite());}}
  }
  async function showLaunch(id){
    const detail=show('<h2 id="dialog-title">单次采集</h2><div class="loading-state compact"><span class="loader"></span></div>','launch');if(!detail)return;
    detail.id=id;detail.tasks=pageModel();await loadLaunch(detail);
  }
  async function loadAlertEvents(detail,direction=null){
    detail.readController?.abort();const current=new AbortController();detail.readController=current;
    const signal=AbortSignal.any([detail.signal,current.signal]),cursors=collectionPageCursors(detail.events,direction);
    detail.events.loading=true;detail.events.error=null;
    if(detail.alert)$('dialog-content').innerHTML=renderCollectionAlertHistory(detail.alert,detail.events,canWrite());
    try{
      const data=await request(pageQuery(`/api/v1/collections/alerts/${encode(detail.id)}/events`,cursors),{signal});
      if(signal.aborted||dialogState!==detail)return;
      detail.alert=data.alert;acceptCollectionPage(detail.events,data,cursors);detail.events.evaluation=data.evaluation;
    }catch(error){if(!signal.aborted){detail.events.error=collectionErrorMessage(error);if(!detail.alert)showReadError(detail,'告警历史暂不可用','alert-history',detail.id,error);}}
    finally{if(!signal.aborted&&dialogState===detail){detail.events.loading=false;if(detail.alert)$('dialog-content').innerHTML=renderCollectionAlertHistory(detail.alert,detail.events,canWrite());}}
  }
  async function showAlert(id){
    const detail=show('<h2 id="dialog-title">告警事件历史</h2><div class="loading-state compact"><span class="loader"></span></div>','alert');if(!detail)return;
    detail.id=id;detail.events=pageModel();await loadAlertEvents(detail);
  }
  function findPlan(id){return String(dialogState?.plan?.id)===String(id)?dialogState.plan:state.plans.items.find(plan=>String(plan.id)===String(id));}
  function updatePlan(plan){const index=state.plans.items.findIndex(item=>String(item.id)===String(plan.id));if(index>=0)state.plans.items[index]=plan;if(String(dialogState?.plan?.id)===String(plan.id))dialogState.plan=plan;}

  function bindPicker(detail){
    detail.picker=createCollectionResourcePicker({
      taskType:$('collection-type').value,selectedIds:detail.draft?.resource_ids||[],maximum:state.control?.limits?.max_resources||100,
      read:({taskType,query:search,ids,beforeId,signal})=>{
        const params=new URLSearchParams({task_type:taskType});
        if(ids)for(const id of ids)params.append('ids',id);
        else{params.set('limit','20');if(search)params.set('q',search);if(beforeId)params.set('before_id',beforeId);}
        return request(`${ROOT}/resources?${params}`,{signal});
      },
      onChange:picker=>{
        if(dialogState!==detail||detail.signal.aborted||state.busy)return;
        const focusedResource=document.activeElement?.dataset.collectionResource;
        $('collection-resource-selected').innerHTML=renderCollectionResourceSelected(picker);
        $('collection-resource-results').innerHTML=renderCollectionResourceResults(picker);
        if(!detail.advancedDirty)$('collection-resources').value=[...picker.selected.keys()].join(', ');
        if(focusedResource)document.querySelector(`[data-collection-resource="${CSS.escape(focusedResource)}"]`)?.focus({preventScroll:true});
        saveDraft(detail);
      },
    });
    if($('collection-type').value!=='shop_discovery'){detail.picker.load();detail.picker.resolveSelected();}
  }
  function showForm(plan=null,launch=false){
    if(plan?.archived_at){toast('已归档计划需先恢复。');return;}
    const key=launch?'launch':plan?`plan:${plan.id}`:'plan:new',saved=drafts.get(key);
    const original=saved?.original||plan;
    const detail=show(launch?renderCollectionLaunchForm(state.control,saved?.draft):renderCollectionPlanForm(original,state.control,saved?.draft||original),launch?'launch-form':'form');if(!detail)return;
    Object.assign(detail,{plan:original,draftKey:key,draft:saved?.draft||original,requestId:saved?.requestId||requestId(),submittedBody:saved?.submittedBody,uncertain:saved?.uncertain||false});
    bindPicker(detail);saveDraft(detail);$('collection-name')?.focus();
    if(detail.uncertain)formError('上次提交结果尚未确认，已保留原请求。请保持内容不变重试，或先到记录列表核对。');
  }
  function confirm(kind,item=null,id=''){
    const key=`${kind}:${item?.id||id}`,existing=kind==='run'?commands.get(key):null;
    const detail=show(renderCollectionConfirmation(kind,item,state.control?.paused),'confirm');if(!detail)return;
    detail.command=existing||{kind,item,id,key,requestId:requestId(),paused:!state.control?.paused};commands.set(key,detail.command);
  }

  async function mutate(path,body,{method='POST',detail=dialogState,root=ROOT}={}){
    if(!token){formError('请先加载已配置的 API 写入令牌。');return null;}
    if(state.busy)return null;
    state.busy=true;mutationDialog=detail;
    const controls=detail?Array.from($('detail-dialog').querySelectorAll('button,input,select,textarea')).map(element=>[element,element.disabled]):[];
    for(const [element] of controls)element.disabled=true;
    if(detail)$('dialog-content').setAttribute('aria-busy','true');
    draw();const timeout=AbortSignal.timeout(20000);
    try{const result=await write(`${root}${path}`,body,{method,token,signal:timeout});if(detail)detail.uncertain=false;return result;}
    catch(error){
      if(detail)detail.uncertain=timeout.aborted||!error?.status||error.status>=500||error.code==='idempotency_conflict';
      formError(timeout.aborted?'提交超时，服务可能已接受请求。已保留本次请求，请重试原操作或刷新记录核对。':collectionErrorMessage(error));return null;
    }finally{
      state.busy=false;mutationDialog=null;saveDraft(detail);
      for(const [element,wasDisabled] of controls)if(element.isConnected)element.disabled=wasDisabled;
      $('dialog-content').removeAttribute('aria-busy');if(['collections','collection-monitor'].includes(getPage()))draw();
    }
  }
  function preparedBody(detail,body){
    const comparable={...body};delete comparable.request_id;
    const previous=detail.submittedBody?{...detail.submittedBody}:null;if(previous)delete previous.request_id;
    if(previous&&JSON.stringify(previous)!==JSON.stringify(comparable)){
      if(detail.uncertain){formError('上次提交结果尚未确认。请保持原内容重试，或先刷新记录核对，避免重复采集。');return null;}
      detail.requestId=requestId();
    }
    body.request_id=detail.requestId;detail.submittedBody=body;saveDraft(detail);return body;
  }
  function selectionValid(detail,resources){
    if(!resources.length){formError('请至少选择一个采集资源。');return false;}
    if(detail.picker.state.resolving||resources.some(id=>detail.picker.state.selected.get(id)?.eligible!==true)){
      formError('部分已选资源尚未核对或当前不可采集，请移除后重试，或重新核对资源。');return false;
    }
    return true;
  }
  async function submitPlan(){
    const detail=dialogState;if(detail?.kind!=='form'||state.busy)return;saveDraft(detail);
    const draft=detail.draft,name=draft.name.trim(),mode=draft.schedule_mode,type=draft.task_type;
    if(!name){formError('请填写计划名称。');return;}
    const minimum=state.control?.limits?.min_interval_seconds?.[type]||(type==='shop_profile'?3600:60),maximum=state.control?.limits?.max_interval_seconds||604800;
    if(mode==='interval'&&(!Number.isInteger(draft.interval_seconds)||draft.interval_seconds<minimum||draft.interval_seconds>maximum)){
      formError(`执行间隔需在 ${minimum/60} 至 ${maximum/60} 分钟之间。`);return;
    }
    const values={name,resource_ids:draft.resource_ids,interval_seconds:mode==='interval'?draft.interval_seconds:null,enabled:draft.enabled};
    let body=detail.plan?collectionPlanPatch(detail.plan,values):{...values,task_type:type,schedule_mode:mode};
    if(!detail.plan||body.resource_ids){if(!selectionValid(detail,values.resource_ids))return;}
    if(detail.plan&&!Object.keys(body).length){toast('没有需要保存的更改。');closeDialog();return;}
    if(!detail.plan){body=preparedBody(detail,body);if(!body)return;}
    const result=await mutate(detail.plan?`/plans/${encode(detail.plan.id)}`:'/plans',body,{method:detail.plan?'PATCH':'POST',detail});if(!result)return;
    await finishPlan(detail);
  }
  async function finishPlan(detail){
    const key=detail.draftKey;detail.draftKey=null;drafts.delete(key);closeDialog();toast(detail.plan?'采集计划已更新。':'采集计划已保存。');
    state.tab='plans';state.plans.pendingReset=true;await refreshManagement();
  }
  async function submitLaunch(){
    const detail=dialogState;if(detail?.kind!=='launch-form'||state.busy)return;saveDraft(detail);
    const draft=detail.draft,type=draft.task_type,name=draft.name.trim();if(!name){formError('请填写采集名称。');return;}
    let body={name,task_type:type};
    if(type==='shop_discovery'){
      try{body.shop_urls=parseCollectionShopUrls($('collection-shop-urls').value,state.control?.limits?.max_resources||100);}catch(error){formError(error.message);return;}
    }else{if(!selectionValid(detail,draft.resource_ids))return;body.resource_ids=draft.resource_ids;}
    if(['product_discovery','product_reviews'].includes(type)){
      const maximum=state.control?.limits?.[type==='product_reviews'?'max_review_pages':'max_discovery_pages']||1;
      if(!Number.isInteger(draft.max_pages)||draft.max_pages<1||draft.max_pages>maximum){formError(`采集页数需为 1 至 ${maximum} 的整数。`);return;}body.max_pages=draft.max_pages;
    }
    body=preparedBody(detail,body);if(!body)return;const result=await mutate('/launches',body,{detail});if(!result)return;
    await finishLaunch(detail,result);
  }
  async function finishLaunch(detail,result){
    drafts.delete(detail.draftKey);detail.draftKey=null;
    toast(result.created?`采集已提交：新建 ${result.launch.created_task_count} 个任务，复用 ${result.launch.reused_task_count} 个任务。`:'已读取先前提交的采集，未重复创建。');
    state.tab='launches';state.launches.pendingReset=true;await showLaunch(result.launch.id);refreshManagement();
  }
  async function retrySubmission(){
    const detail=dialogState;if(!detail?.submittedBody||!detail.uncertain||state.busy)return;
    const launch=detail.kind==='launch-form';
    const result=await mutate(launch?'/launches':'/plans',detail.submittedBody,{detail});
    if(result){if(launch)await finishLaunch(detail,result);else await finishPlan(detail);}
  }
  async function executeConfirmation(){
    const detail=dialogState,command=detail?.command;if(!command||state.busy)return;let result;
    if(command.kind==='control')result=await mutate('/control',{paused:command.paused},{method:'PUT',detail});
    if(command.kind==='run')result=await mutate(`/plans/${encode(command.item.id)}/run`,{request_id:command.requestId},{detail});
    if(['archive','restore'].includes(command.kind))result=await mutate(`/plans/${encode(command.item.id)}/${command.kind}`,{},{detail});
    if(command.kind==='cancel-task')result=await mutate(`/tasks/${encode(command.id)}/cancel`,{},{detail});
    if(command.kind==='retry-task')result=await mutate(`/tasks/${encode(command.id)}/retry`,{},{detail});
    if(command.kind==='launch-cancel')result=await mutate(`/launches/${encode(command.item.id)}/cancel`,{},{detail});
    if(command.kind==='alert-ack')result=await mutate(`/alerts/${encode(command.item.id)}/ack`,{},{detail,root:'/api/v1/collections'});
    if(!result)return;commands.delete(command.key);
    if(command.kind==='control'){state.control=result;closeDialog();toast(result.paused?'已暂停新的来源采集。':'来源采集已恢复。');}
    if(command.kind==='run'){toast(result.run?.error_code?'本次运行未能派发任务，请查看执行记录中的原因。':result.created?'采集任务已派发。':'本次执行已存在，已读取原记录。');if(result.run?.id)await showRun(result.run.id);}
    if(['archive','restore'].includes(command.kind)){updatePlan(result.plan);closeDialog();toast(command.kind==='archive'?'计划已归档，任务和历史已保留。':'计划已恢复并保持停用，可按需启用或手动运行。');state.plans.pendingReset=true;}
    if(command.kind==='cancel-task'){toast(result.changed?'任务已取消，历史记录已保留。':'任务已处于取消状态。');openTask(command.id);}
    if(command.kind==='retry-task'){toast(result.created?'已创建重试任务，原历史记录保留。':'已存在重试任务，已打开原重试记录。');openTask(result.task?.task_id||command.id);}
    if(command.kind==='launch-cancel'){toast(result.changed?`已请求取消：本次取消 ${result.cancelled_task_count} 个，剩余 ${result.pending_remaining_count} 个由调度器处理；运行中 ${result.still_running_count} 个、共享 ${result.shared_task_count} 个继续执行，批次会在关联任务结束后完成取消。`:'批次已经结束或已取消，历史状态保持不变。');await showLaunch(command.item.id);}
    if(command.kind==='alert-ack'){toast(result.changed?'已确认告警，条件恢复后自动关闭。':'告警已确认或已恢复，当前无需重复操作。');await showAlert(command.item.id);}
    if(getPage()==='collections')refreshManagement();if(getPage()==='collection-monitor')monitor.refresh();
  }

  async function handleAction(element){
    if(element.disabled||state.busy)return;const {collectionAction:action,id,value}=element.dataset;
    if(action==='close'){closeDialog();return;}
    if(action==='manage'){closeDialog();location.hash='collections';return;}
    if(action==='auth'){show(renderCollectionAuth(Boolean(token)),'auth');$('collection-token')?.focus();return;}
    if(action==='disconnect'){token='';state.tokenConnected=false;closeDialog();draw();toast('采集写入令牌已清除。');return;}
    if(action==='tab'){clearTimeout(filterTimer);state.tab=['audit','launches'].includes(value)?value:'plans';refreshManagement();return;}
    if(action==='create'||action==='launch-create'){if(canWrite())showForm(null,action==='launch-create');return;}
    if(action==='edit'){const plan=findPlan(id);if(plan&&canWrite())showForm(plan);return;}
    if(action==='plan'){showPlan(id);return;}
    if(action==='run-detail'){showRun(id);return;}
    if(action==='launch-detail'){showLaunch(id);return;}
    if(action==='launch-cancel'){const launch=dialogState?.launch||state.launches.items.find(item=>String(item.id)===String(id));if(launch&&canWrite())confirm(action,launch);return;}
    if(action==='alert-history'){showAlert(id);return;}
    if(action==='alert-ack'){const alert=dialogState?.alert||state.alerts.items.find(item=>String(item.id)===String(id));if(alert&&canWrite())confirm(action,alert);return;}
    if(action==='alerts-refresh'){refreshAlerts();return;}
    if(action==='retry-submission'){retrySubmission();return;}
    if(action==='launch-list'){closeDialog();state.tab='launches';if(getPage()!=='collections')location.hash='collections';else refreshManagement();return;}
    if(action==='control'){if(canWrite())confirm('control');return;}
    if(['run','archive','restore'].includes(action)){
      const plan=findPlan(id);if(!plan||!canWrite())return;
      if(action==='run'&&state.control?.paused){toast('采集已暂停，请先恢复调度。');return;}
      if(action==='run'&&!state.control?.enabled_task_types?.includes(plan.task_type)){toast('该采集类型在服务中未启用。');return;}
      confirm(action,plan);return;
    }
    if(action==='toggle'){
      const plan=findPlan(id);if(!plan||!canWrite()||planLocks.has(String(id)))return;planLocks.add(String(id));
      try{const result=await mutate(`/plans/${encode(id)}`,{enabled:!plan.enabled},{method:'PATCH',detail:null});if(result){updatePlan(result);toast(result.enabled?'计划已启用。':'计划已停用，已派发任务继续执行。');await refreshManagement();}}
      finally{planLocks.delete(String(id));}return;
    }
    if(action==='confirm'){executeConfirmation();return;}
    if(action==='cancel-task'||action==='retry-task'){if(canWrite())confirm(action,null,id);return;}
    if(action==='poll'){monitor.toggle();return;}
    if(action.startsWith('resource-')){
      const picker=dialogState?.picker;if(!picker)return;
      if(action==='resource-remove')picker.remove(id);
      if(action==='resource-replace'){try{const ids=parseCollectionResourceIds($('collection-resources').value,picker.state.maximum);dialogState.advancedDirty=false;picker.replace(ids);}catch(error){formError(error.message);}}
      if(action==='resource-resolve')picker.resolveSelected();if(action==='resource-refresh')picker.load();
      if(action==='resource-prev'||action==='resource-next')picker.load({direction:action.endsWith('next')?'next':'prev'});return;
    }
    if(action==='runs-refresh'||/^runs-(prev|next)$/.test(action)){const detail=dialogState;if(detail?.runs&&!detail.runs.loading)loadRuns(detail,action==='runs-refresh'?null:action.split('-')[1]);return;}
    if(/^launch-tasks-(prev|next)$/.test(action)){const detail=dialogState;if(detail?.tasks&&!detail.tasks.loading)loadLaunch(detail,action.endsWith('next')?'next':'prev');return;}
    if(/^alert-events-(prev|next)$/.test(action)){const detail=dialogState;if(detail?.events&&!detail.events.loading)loadAlertEvents(detail,action.endsWith('next')?'next':'prev');return;}
    if(/^alerts-(prev|next)$/.test(action)){if(!state.alerts.loading&&!state.alerts.error)refreshAlerts({direction:action.endsWith('next')?'next':'prev'});return;}
    if(/^(plans|audit|launches)-(prev|next)$/.test(action)){const [kind,direction]=action.split('-');if(!state[kind].loading&&!state[kind].error)refreshManagement({direction});}
  }

  function updateFormType(){
    const detail=dialogState;if(!detail?.picker)return;const type=$('collection-type').value;
    if(detail.picker.state.taskType!==type){clearTimeout(detail.searchTimer);$('collection-resource-query').value='';detail.advancedDirty=false;detail.picker.setType(type,{fetch:type!=='shop_discovery'});}
    $('collection-resource-label').textContent=`选择${['shop_profile','product_discovery'].includes(type)?'店铺':'商品'}`;
    if(detail.kind==='form'){
      const minimum=(state.control?.limits?.min_interval_seconds?.[type]||(type==='shop_profile'?3600:60))/60,mode=$('collection-schedule').value;
      $('collection-interval-field').hidden=mode!=='interval';$('collection-interval').disabled=mode!=='interval';$('collection-interval').min=String(minimum);
      if(Number($('collection-interval').value)<minimum)$('collection-interval').value=String(minimum);
      $('collection-interval-hint').textContent=`最短 ${minimum} 分钟，最长 7 天。上一批仍在执行时不重复派发。`;
      $('collection-schedule-hint').textContent=mode==='once'?'启用后自动派发一次并停用；停用时仅保存计划。':'启用后按间隔自动运行；更改名称和范围不会推迟既定执行时间。';
    }else{
      const budget=['product_discovery','product_reviews'].includes(type),maximum=state.control?.limits?.[type==='product_reviews'?'max_review_pages':'max_discovery_pages']||1;
      $('collection-seeds').hidden=type!=='shop_discovery';$('collection-launch-picker').hidden=type==='shop_discovery';
      $('collection-budget-field').hidden=!budget;$('collection-max-pages').disabled=!budget;$('collection-max-pages').max=String(maximum);
      if(Number($('collection-max-pages').value)>maximum||Number($('collection-max-pages').value)<1)$('collection-max-pages').value=String(maximum);
      $('collection-budget-hint').textContent=`每个资源最多 ${maximum} 页，任务遇到末页时会提前完成。`;
      $('collection-launch-dependencies').innerHTML=renderCollectionLaunchDependencies(type,state.control);
    }
    saveDraft(detail);
  }
  function planFilterChanged(){
    if(!$('collection-plan-query'))return;
    state.filters={q:$('collection-plan-query').value.trim(),task_type:$('collection-plan-type').value,enabled:$('collection-plan-enabled').value,archived:$('collection-plan-archived').value};
    state.plans.pendingReset=true;refreshManagement({reset:true});
  }
  document.addEventListener('click',event=>{const element=event.target.closest('[data-collection-action]');if(element){event.preventDefault();handleAction(element);}});
  document.addEventListener('submit',event=>{
    if(event.target.id==='collection-plan-form'){event.preventDefault();submitPlan();}
    if(event.target.id==='collection-launch-form'){event.preventDefault();submitLaunch();}
    if(event.target.id==='collection-auth-form'){
      event.preventDefault();const value=$('collection-token').value.trim();if(value.length<24){formError('请输入服务中配置的完整写入令牌（至少 24 个字符）。');return;}
      token=value;state.tokenConnected=true;$('collection-token').value='';closeDialog();draw();toast('写入令牌已加载到当前页面，提交时验证权限。');
    }
  });
  function handleInput(event){
    if(event.isComposing)return;
    if(event.target.id==='collection-resource-query'){
      const detail=dialogState;if(!detail?.picker)return;const search=event.target.value;
      clearTimeout(detail.searchTimer);detail.searchTimer=setTimeout(()=>{if(dialogState===detail)detail.picker.load({query:search});},250);return;
    }
    if(event.target.id==='collection-plan-query'){controller?.abort();clearTimeout(filterTimer);filterTimer=setTimeout(planFilterChanged,250);return;}
    if(event.target.id==='collection-resources'&&dialogState)dialogState.advancedDirty=true;
    if(event.target.closest('#collection-plan-form,#collection-launch-form'))saveDraft();
  }
  document.addEventListener('input',handleInput);document.addEventListener('compositionend',handleInput);
  document.addEventListener('change',event=>{
    if(event.target.id==='collection-monitor-window'){monitor.setWindow(Number(event.target.value));return;}
    if(event.target.id==='collection-monitor-group'){monitor.setGroup(event.target.value);return;}
    if(event.target.id==='collection-alert-status'){state.alerts.status=event.target.value;state.alerts.pendingReset=true;refreshAlerts({reset:true});return;}
    if(['collection-plan-type','collection-plan-enabled','collection-plan-archived'].includes(event.target.id)){clearTimeout(filterTimer);planFilterChanged();return;}
    if(event.target.id==='collection-launch-type'){state.launchType=event.target.value;state.launches.pendingReset=true;refreshManagement({reset:true});return;}
    if(event.target.dataset.collectionResource){const picker=dialogState?.picker,row=picker?.state.items.find(item=>String(item.id)===event.target.dataset.collectionResource);if(row)picker.select(row,event.target.checked);return;}
    if(['collection-type','collection-schedule'].includes(event.target.id)){updateFormType();return;}
    if(event.target.closest('#collection-plan-form,#collection-launch-form'))saveDraft();
  });
  document.addEventListener('visibilitychange',()=>{if(document.hidden){alertController?.abort();state.alerts.loading=false;}monitor.visibilityChanged();});
  globalThis.addEventListener?.('beforeunload',event=>{if(state.busy){event.preventDefault();event.returnValue='';}});
  return {
    activate(page){clearTimeout(filterTimer);controller?.abort();alertController?.abort();state.loading=false;for(const name of ['plans','audit','launches','alerts'])state[name].loading=false;monitor.activate(page==='collection-monitor');},
    refresh(){return getPage()==='collection-monitor'?monitor.refresh():refreshManagement();},
    render(page){return page==='collection-monitor'?`${renderCollectionMonitor(monitor.state,icon)}${renderCollectionAlerts(state.alerts,canWrite()&&!state.busy,icon)}`:renderCollectionManagement(state,icon);},
    taskActions(task){const actions=collectionTaskActions(task,canWrite());return actions?`${actions}${canWrite()?'':' <button class="button" data-collection-action="manage">打开采集管理</button>'}`:'';},
    canCloseDialog(){return !(state.busy&&mutationDialog);},
    dialogClosed(){releaseDialog();dialogState=null;$('detail-dialog').classList.remove('collection-dialog');},
    connection(){return getPage()==='collection-monitor'?{loading:monitor.state.loading,loaded:Boolean(monitor.state.data),stale:Boolean(monitor.state.error),updatedAt:monitor.state.data?.generated_at}:{loading:state.loading,loaded:state.loaded,stale:Boolean(state.error),updatedAt:state.control?.updated_at};},
  };
}
