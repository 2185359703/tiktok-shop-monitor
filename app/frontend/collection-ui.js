import {
  collectionErrorMessage, collectionTaskActions, parseCollectionResourceIds,
  renderCollectionManagement, renderCollectionMonitor, renderCollectionAuth,
  renderCollectionPlanForm, renderCollectionPlanDetail, renderCollectionRun,
  renderCollectionConfirmation,
} from './collection-view.js';

const ROOT = '/api/v1/collection-management';
const pageModel = () => ({items:[],next:null,cursors:[null],loading:false,loaded:false,error:null});
const encode = value => encodeURIComponent(String(value));
const query = (path, model) => `${path}?limit=20${model.cursors.at(-1) ? `&before_id=${encode(model.cursors.at(-1))}` : ''}`;
function requestId() {
  if (globalThis.crypto.randomUUID) return globalThis.crypto.randomUUID();
  const bytes = globalThis.crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = bytes[6] & 15 | 64; bytes[8] = bytes[8] & 63 | 128;
  const hex = Array.from(bytes, value => value.toString(16).padStart(2,'0')).join('');
  return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
}

/** One visible-page request at a time; navigation and tab hiding cancel ownership. */
export function createCollectionMonitorPoller({read,onChange=()=>{},isHidden=()=>false,schedule=setTimeout,cancel=clearTimeout,interval=15000}) {
  const state = {data:null,error:null,loading:false,polling:true,windowMinutes:60};
  let active = false, controller = null, timer = null;
  function stop() {
    if (timer !== null) cancel(timer);
    timer = null;
    controller?.abort(); controller = null; state.loading = false;
  }
  async function refresh() {
    stop();
    if (!active || isHidden()) return;
    const current = new AbortController(); controller = current;
    state.loading = true; onChange();
    try {
      const data = await read(state.windowMinutes, {signal:current.signal});
      if (controller !== current || current.signal.aborted) return;
      state.data = data; state.error = null;
    } catch (error) {
      if (controller !== current || current.signal.aborted) return;
      state.error = collectionErrorMessage(error);
    } finally {
      if (controller === current && !current.signal.aborted) {
        state.loading = false; controller = null; onChange();
        if (active && state.polling && !isHidden()) timer = schedule(refresh, interval);
      }
    }
  }
  return {
    state, refresh,
    activate(value) {active = value; if (!active) stop();},
    visibilityChanged() {if (isHidden()) stop(); else if (active && state.polling) refresh();},
    toggle() {state.polling = !state.polling; if (state.polling) refresh(); else stop(); onChange();},
    setWindow(value) {if (![15,60,360,1440].includes(value)) return; state.windowMinutes = value; refresh();},
    stop,
  };
}

export function createCollectionWorkspace({request,write,getPage,render,toast,openDialog,closeDialog,openTask,icon}) {
  const state = {control:null,plans:pageModel(),audit:pageModel(),tab:'plans',tokenConnected:false,busy:false,error:null,loaded:false,loading:false};
  const monitor = createCollectionMonitorPoller({
    read:(minutes, options) => request(`/api/v1/collections/monitor?window_minutes=${minutes}`, options),
    isHidden:() => document.hidden,
    onChange:() => {if (getPage() === 'collection-monitor') render();},
  });
  let token = '', controller = null, dialogState = null;
  const canWrite = () => Boolean(token && state.control?.writes_enabled);
  const $ = id => document.getElementById(id);

  async function refreshManagement() {
    controller?.abort(); const current = new AbortController(); controller = current;
    const model = state.tab === 'audit' ? state.audit : state.plans;
    state.loading = true; model.loading = true; model.error = null; state.error = null; render();
    const results = await Promise.allSettled([
      request(`${ROOT}/control`, {signal:current.signal}),
      request(query(`${ROOT}/${state.tab === 'audit' ? 'audit' : 'plans'}`,model), {signal:current.signal}),
    ]);
    if (current.signal.aborted || controller !== current) return;
    if (results[0].status === 'fulfilled') {state.control = results[0].value; state.loaded = true;}
    else state.error = collectionErrorMessage(results[0].reason);
    if (results[1].status === 'fulfilled') {
      model.items = results[1].value.items || []; model.next = results[1].value.next_before_id; model.loaded = true;
    } else {model.error = collectionErrorMessage(results[1].reason); state.error = model.error;}
    model.loading = false; state.loading = false; render();
  }

  function show(html, kind) {
    const signal = openDialog('COLLECTION / 采集管理');
    $('detail-dialog').classList.add('collection-dialog');
    $('dialog-content').innerHTML = html;
    dialogState = {kind,signal};
    return dialogState;
  }
  function formError(message) {
    const element = $('collection-form-error');
    if (element) {element.textContent = message; element.hidden = false;}
    else toast(message);
  }
  function drawPlan(detail) {
    if (dialogState !== detail || detail.signal.aborted) return;
    $('dialog-content').innerHTML = renderCollectionPlanDetail(detail.plan, detail.runs, canWrite());
  }
  async function loadRuns(detail) {
    detail.runs.loading = true; detail.runs.error = null; drawPlan(detail);
    try {
      const data = await request(query(`${ROOT}/plans/${encode(detail.plan.id)}/runs`,detail.runs),{signal:detail.signal});
      if (dialogState !== detail || detail.signal.aborted) return;
      detail.runs.items = data.items || []; detail.runs.next = data.next_before_id; detail.runs.loaded = true;
    } catch (error) {if (!detail.signal.aborted) detail.runs.error = collectionErrorMessage(error);}
    finally {if (!detail.signal.aborted) {detail.runs.loading = false; drawPlan(detail);}}
  }
  async function showPlan(id) {
    const detail = show('<h2 id="dialog-title">采集计划</h2><div class="loading-state compact"><span class="loader"></span><p>正在读取计划与执行记录…</p></div>','plan');
    try {
      detail.plan = await request(`${ROOT}/plans/${encode(id)}`,{signal:detail.signal});
      if (detail.signal.aborted) return;
      detail.runs = pageModel(); await loadRuns(detail);
    } catch (error) {if (!detail.signal.aborted) {show('<h2 id="dialog-title">采集计划暂不可用</h2><div id="collection-form-error" class="notice warning" role="alert"></div>','error'); formError(collectionErrorMessage(error));}}
  }
  async function showRun(id) {
    const detail = show('<h2 id="dialog-title">执行记录</h2><div class="loading-state compact"><span class="loader"></span></div>','run');
    try {
      detail.run = await request(`${ROOT}/runs/${encode(id)}`,{signal:detail.signal});
      if (detail.signal.aborted) return;
      $('dialog-content').innerHTML = renderCollectionRun(detail.run,canWrite());
    } catch (error) {if (!detail.signal.aborted) {show('<h2 id="dialog-title">执行记录暂不可用</h2><div id="collection-form-error" class="notice warning" role="alert"></div>','error'); formError(collectionErrorMessage(error));}}
  }
  function findPlan(id) {
    return String(dialogState?.plan?.id) === String(id) ? dialogState.plan : state.plans.items.find(plan => String(plan.id) === String(id));
  }
  function showForm(plan = null) {
    const detail = show(renderCollectionPlanForm(plan,state.control),'form');
    detail.plan = plan; detail.requestId = requestId();
    $('collection-name')?.focus();
  }
  function confirm(kind,item=null,id='') {
    const detail = show(renderCollectionConfirmation(kind,item,state.control?.paused),'confirm');
    detail.command = {kind,item,id,requestId:requestId(),paused:!state.control?.paused};
  }

  async function mutate(path,body,{method='POST',detail=dialogState}={}) {
    if (!token) {formError('请先加载已配置的 API 写入令牌。'); return null;}
    if (state.busy) return null;
    state.busy = true;
    const submit = $('dialog-content').querySelector('[type="submit"],[data-collection-action="confirm"]');
    if (submit) {submit.disabled = true; submit.setAttribute('aria-busy','true');}
    const timeout = AbortSignal.timeout(15000);
    const signal = detail?.signal ? AbortSignal.any([detail.signal,timeout]) : timeout;
    try {return await write(`${ROOT}${path}`,body,{method,token,signal});}
    catch (error) {
      if (!detail?.signal?.aborted) formError(timeout.aborted ? '提交超时，服务可能已接受请求。请重试当前操作或刷新状态核对。' : collectionErrorMessage(error));
      return null;
    } finally {
      state.busy = false;
      if (submit?.isConnected) {submit.disabled = false; submit.removeAttribute('aria-busy');}
      if (getPage() === 'collections') render();
    }
  }

  async function submitPlan(form) {
    const detail = dialogState;
    if (detail?.kind !== 'form') return;
    let resources;
    try {resources = parseCollectionResourceIds($('collection-resources').value,state.control?.limits?.max_resources || 100);}
    catch (error) {formError(error.message); return;}
    const name = $('collection-name').value.trim();
    if (!name) {formError('请填写计划名称。'); return;}
    const type = $('collection-type').value, mode = $('collection-schedule').value;
    const minimum = state.control?.limits?.min_interval_seconds?.[type] || (type === 'shop_profile' ? 3600 : 60);
    const seconds = mode === 'interval' ? Number($('collection-interval').value) * 60 : null;
    if (mode === 'interval' && (!Number.isInteger(seconds) || seconds < minimum || seconds > (state.control?.limits?.max_interval_seconds || 604800))) {
      formError(`执行间隔需在 ${minimum/60} 至 10080 分钟之间。`); return;
    }
    const body = {name,resource_ids:resources,interval_seconds:seconds,enabled:$('collection-enabled').checked};
    if (!detail.plan) Object.assign(body,{task_type:type,schedule_mode:mode,request_id:detail.requestId});
    const result = await mutate(detail.plan ? `/plans/${encode(detail.plan.id)}` : '/plans',body,{method:detail.plan ? 'PATCH' : 'POST',detail});
    if (!result || detail.signal.aborted) return;
    closeDialog(); toast(detail.plan ? '采集计划已更新。' : '采集计划已保存。');
    state.plans.cursors = [null]; await refreshManagement();
  }

  async function executeConfirmation() {
    const detail = dialogState, command = detail?.command;
    if (!command) return;
    let result;
    if (command.kind === 'control') result = await mutate('/control',{paused:command.paused},{method:'PUT',detail});
    if (command.kind === 'run') result = await mutate(`/plans/${encode(command.item.id)}/run`,{request_id:command.requestId},{detail});
    if (command.kind === 'cancel-task') result = await mutate(`/tasks/${encode(command.id)}/cancel`,{},{detail});
    if (command.kind === 'retry-task') result = await mutate(`/tasks/${encode(command.id)}/retry`,{},{detail});
    if (!result || detail.signal.aborted) return;
    if (command.kind === 'control') {closeDialog(); state.control = result; toast(result.paused ? '已暂停新的来源采集。' : '来源采集已恢复。');}
    if (command.kind === 'run') {toast(result.created ? '采集任务已派发。' : '该次执行已存在，已读取原执行记录。'); if (result.run?.id) showRun(result.run.id);}
    if (command.kind === 'cancel-task') {toast(result.changed ? '任务已取消，历史记录已保留。' : '任务已处于取消状态。'); openTask(command.id);}
    if (command.kind === 'retry-task') {toast(result.created ? '重试任务已创建，原历史记录保留。' : '已存在重试任务，已打开原重试记录。'); openTask(result.task?.task_id || command.id);}
    if (getPage() === 'collections') refreshManagement();
    if (getPage() === 'collection-monitor') monitor.refresh();
  }

  async function handleAction(element) {
    if (element.disabled || state.busy) return;
    const {collectionAction:action,id,value} = element.dataset;
    if (action === 'close') {closeDialog(); return;}
    if (action === 'manage') {closeDialog(); location.hash = 'collections'; return;}
    if (action === 'auth') {show(renderCollectionAuth(Boolean(token)),'auth'); $('collection-token')?.focus(); return;}
    if (action === 'disconnect') {token = ''; state.tokenConnected = false; closeDialog(); render(); toast('采集写入令牌已清除。'); return;}
    if (action === 'tab') {state.tab = value === 'audit' ? 'audit' : 'plans'; refreshManagement(); return;}
    if (action === 'create') {if (canWrite()) showForm(); return;}
    if (action === 'edit') {const plan = findPlan(id); if (plan && canWrite()) showForm(plan); return;}
    if (action === 'plan') {showPlan(id); return;}
    if (action === 'run-detail') {showRun(id); return;}
    if (action === 'control') {if (canWrite()) confirm('control'); return;}
    if (action === 'run') {const plan = findPlan(id); if (state.control?.paused) {toast('采集已全局暂停，请先恢复调度。'); return;} if (plan && !state.control?.enabled_task_types?.includes(plan.task_type)) {toast('该采集类型在服务中未启用。'); return;} if (plan && canWrite()) confirm('run',plan); return;}
    if (action === 'toggle') {
      const plan = findPlan(id); if (!plan || !canWrite()) return;
      const result = await mutate(`/plans/${encode(id)}`,{enabled:!plan.enabled},{method:'PATCH',detail:null});
      if (result) {toast(result.enabled ? '计划已启用。' : '计划已停用，已派发任务继续执行。'); refreshManagement();}
      return;
    }
    if (action === 'confirm') {executeConfirmation(); return;}
    if (action === 'cancel-task' || action === 'retry-task') {if (canWrite()) confirm(action,null,id); return;}
    if (action === 'poll') {monitor.toggle(); return;}
    if (action === 'runs-prev' || action === 'runs-next') {
      const detail = dialogState; if (!detail?.runs || detail.runs.loading) return;
      if (action.endsWith('next') && detail.runs.next) detail.runs.cursors.push(detail.runs.next);
      else if (action.endsWith('prev') && detail.runs.cursors.length > 1) detail.runs.cursors.pop();
      await loadRuns(detail); return;
    }
    if (/^(plans|audit)-(prev|next)$/.test(action)) {
      const [kind,direction] = action.split('-'), model = state[kind];
      if (model.loading) return;
      if (direction === 'next' && model.next) model.cursors.push(model.next);
      else if (direction === 'prev' && model.cursors.length > 1) model.cursors.pop();
      refreshManagement();
    }
  }

  document.addEventListener('click',event => {
    const element = event.target.closest('[data-collection-action]');
    if (element) {event.preventDefault(); handleAction(element);}
  });
  document.addEventListener('submit',event => {
    if (event.target.id === 'collection-plan-form') {event.preventDefault(); submitPlan(event.target);}
    if (event.target.id === 'collection-auth-form') {
      event.preventDefault(); const value = $('collection-token').value.trim();
      if (value.length < 24) {formError('请输入服务中配置的完整写入令牌（至少 24 个字符）。'); return;}
      token = value; state.tokenConnected = true; $('collection-token').value = ''; closeDialog(); render();
      toast('写入令牌已加载到当前页面，提交操作时验证权限。');
    }
  });
  document.addEventListener('change',event => {
    if (event.target.id === 'collection-monitor-window') {monitor.setWindow(Number(event.target.value)); return;}
    if (['collection-type','collection-schedule'].includes(event.target.id)) {
      const type = $('collection-type').value;
      const minimum = (state.control?.limits?.min_interval_seconds?.[type] || (type === 'shop_profile' ? 3600 : 60)) / 60;
      $('collection-resources-label').textContent = `${type === 'shop_profile' ? '店铺' : '商品'}内部 ID`;
      $('collection-interval-field').hidden = $('collection-schedule').value !== 'interval';
      $('collection-interval').disabled = $('collection-schedule').value !== 'interval';
      $('collection-interval').min = String(minimum);
      if (Number($('collection-interval').value) < minimum) $('collection-interval').value = String(minimum);
      $('collection-interval-hint').textContent = `最短 ${minimum} 分钟，最长 7 天。上次仍在执行时不会重复派发。`;
    }
  });
  document.addEventListener('visibilitychange',() => monitor.visibilityChanged());

  return {
    activate(page) {controller?.abort(); state.loading = false; state.plans.loading = false; state.audit.loading = false; monitor.activate(page === 'collection-monitor');},
    refresh() {return getPage() === 'collection-monitor' ? monitor.refresh() : refreshManagement();},
    render(page) {return page === 'collection-monitor' ? renderCollectionMonitor(monitor.state,icon) : renderCollectionManagement(state,icon);},
    taskActions(task) {const actions=collectionTaskActions(task,canWrite()); return actions ? `${actions}${canWrite() ? '' : ' <button class="button" data-collection-action="manage">打开采集管理</button>'}` : '';},
    dialogClosed() {dialogState = null; $('detail-dialog').classList.remove('collection-dialog');},
    connection() {return getPage() === 'collection-monitor' ? {loading:monitor.state.loading,loaded:Boolean(monitor.state.data),stale:Boolean(monitor.state.error),updatedAt:monitor.state.data?.generated_at} : {loading:state.loading,loaded:state.loaded,stale:Boolean(state.error),updatedAt:state.control?.updated_at};},
  };
}
