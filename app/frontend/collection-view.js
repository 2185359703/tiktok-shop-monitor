import { formatDate, labels } from './api.js';

const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num = value => value == null ? '—' : new Intl.NumberFormat('zh-CN', {maximumFractionDigits: 1}).format(value);
const button = (action, text, id = '', extra = '') => `<button type="button" class="button" data-collection-action="${action}"${id ? ` data-id="${esc(id)}"` : ''} ${extra}>${text}</button>`;
const badge = (text, tone = '') => `<span class="badge ${tone}">${esc(text)}</span>`;
const taskBadge = status => badge(labels.taskStatuses[status] || status, {succeeded:'green',running:'blue',retry_waiting:'amber',dead:'red',abandoned:'amber'}[status]);
const runLabels = {queued:'待执行',running:'执行中',succeeded:'已成功',partial:'部分失败',failed:'已失败'};
const runBadge = status => badge(runLabels[status] || status, {succeeded:'green',running:'blue',partial:'amber',failed:'red'}[status]);
const empty = (title, description) => `<div class="empty-state compact"><h2>${esc(title)}</h2><p>${esc(description)}</p></div>`;
const notice = (text, warning = false) => `<div class="notice${warning ? ' warning' : ''}" role="status">${esc(text)}</div>`;
const loading = () => '<div class="loading-state compact"><span class="loader"></span><h2>正在读取采集数据</h2></div>';
const field = (label, value) => `<div class="detail-field"><small>${esc(label)}</small><span>${esc(value ?? '—')}</span></div>`;
const time = value => esc(formatDate(value));
const taskLink = task => `<button class="row-link task-id" data-action="task" data-id="${esc(task.task_id)}" title="${esc(task.task_id)}">${esc(String(task.task_id || '').slice(0, 8))}…</button>`;
const duration = value => value == null ? '—' : Number(value) < 1000 ? `${num(value)} ms` : `${num(Number(value) / 1000)} s`;
export const collectionTaskTypes = Object.freeze({shop_profile:'店铺档案',product_snapshot:'商品快照'});

/** Keep bigint database IDs as strings all the way to the API. */
export function parseCollectionResourceIds(value, maximum = 100) {
  const ids = String(value || '').trim().split(/[\s,，;；]+/).filter(Boolean);
  if (!ids.length) throw new Error('请填写至少一个内部资源 ID。');
  if (ids.some(id => !/^[1-9]\d{0,19}$/.test(id) || BigInt(id) > 18446744073709551615n)) {
    throw new Error('资源 ID 需为完整的正整数，请使用系统内部 ID，不要填写链接或平台 ID。');
  }
  const unique = [...new Set(ids)];
  if (unique.length > maximum) throw new Error(`每个计划最多选择 ${maximum} 个资源。`);
  return unique;
}

export function collectionErrorMessage(error) {
  const known = {
    collection_paused:'采集已全局暂停，请恢复调度后再执行。',
    collection_plan_not_found:'该采集计划不存在，请刷新列表。',
    collection_run_not_found:'该执行记录不存在或不属于当前市场。',
    task_not_found:'该任务不存在或不属于当前市场。',
    collector_not_enabled:'当前服务尚未启用此类采集任务。',
    invalid_plan_resources:'部分资源不存在或不属于当前市场，请核对内部 ID。',
    unsupported_plan_type:'此任务类型尚不支持计划调度。',
    invalid_plan_interval:'执行间隔不在允许范围内，请按表单提示调整。',
    collection_plan_busy:'该计划仍有任务等待或执行中，请完成后再运行。',
    invalid_task_transition:'任务状态已变化，当前操作不可执行，请刷新。',
    unsupported_task_retry:'此类任务暂不支持手动重试。',
    idempotency_conflict:'请求已使用不同的内容提交，请关闭表单后重新操作。',
    write_api_disabled:'服务尚未配置写入令牌，当前仅可查看。',
    database_unavailable:'数据服务暂不可用，请稍后重试。',
    token_required:'请先加载已配置的 API 写入令牌。',
    invalid_api_token:'写入令牌无效，请重新连接。',
  };
  if (known[error?.code]) return known[error.code];
  if ([401,403].includes(error?.status)) return '当前请求没有写入权限，请检查 API 令牌。';
  if (error?.status === 422) return '表单参数不正确，请核对资源 ID 和执行间隔。';
  if (error?.status === 409) return '任务或计划状态已变化，请刷新后重试。';
  if (error?.status === 429) return '请求较频繁，请稍后重试。';
  if (error?.status === 404) return '采集管理接口暂不可用，请确认服务已更新。';
  return '暂时无法连接采集服务，请稍后重试。';
}

function pager(kind, model, noun) {
  return `<div class="table-footer"><span>本页 ${num(model.items.length)} ${noun}</span><div class="pagination">${button(`${kind}-prev`,'上一页','',model.cursors.length === 1 || model.loading ? 'disabled' : '')}<span>第 ${model.cursors.length} 页</span>${button(`${kind}-next`,'下一页','',!model.next || model.loading ? 'disabled' : '')}</div></div>`;
}

function planSchedule(plan) {
  return plan.schedule_mode === 'once' ? '执行一次' : `每 ${num(Number(plan.interval_seconds) / 60)} 分钟`;
}

export function renderCollectionManagement(state, icon) {
  const control = state.control;
  const paused = control?.paused;
  const disabled = !state.tokenConnected || !control?.writes_enabled || state.busy ? 'disabled' : '';
  const enabledTypes = control?.enabled_task_types || [];
  const createDisabled = disabled || (Object.keys(collectionTaskTypes).some(type => enabledTypes.includes(type)) ? '' : 'disabled');
  const header = `<section class="collection-command panel"><div class="collection-command-copy"><span class="collection-state ${control ? paused ? 'paused' : 'active' : ''}"><span class="status-dot${control && !paused ? ' online' : ''}"></span>${control ? paused ? '调度已暂停' : '调度已启用' : '调度状态未知'}</span><h2>采集计划与执行</h2><p>以店铺或商品为范围，管理每一次采集。</p></div><div class="collection-command-actions">${button('auth',`${icon('shield')}${state.tokenConnected ? '管理写入令牌' : '连接写入令牌'}`)}${button('control',`${icon(paused ? 'refresh' : 'clock')}${paused ? '恢复采集' : '暂停采集'}`,'',disabled)}<button class="button primary" data-collection-action="create" ${createDisabled}>${icon('layers')}新建计划</button></div></section>`;
  const messages = `${state.error ? notice(`${state.error}${state.loaded ? ' 当前保留上次结果。' : ''}`, true) : ''}${control && paused ? notice('暂停期间不会开始新的来源采集。正在执行的请求会继续完成，数据处理任务保持运行。', true) : ''}${control && !control.writes_enabled ? notice('当前服务未启用写入，计划、历史和运行状态仍可查看。') : ''}`;
  const tabs = `<div class="section-tabbar collection-tabbar"><div class="analysis-tabs" role="group" aria-label="采集管理视图">${[['plans','采集计划'],['audit','操作记录']].map(([key, label]) => `<button data-collection-action="tab" data-value="${key}" class="${state.tab === key ? 'active' : ''}" aria-pressed="${state.tab === key}">${label}</button>`).join('')}</div><a class="text-link" href="#collection-monitor">运行监控 ${icon('arrow')}</a></div>`;
  if (state.tab === 'audit') return `${header}${messages}${tabs}${renderCollectionAudit(state.audit)}`;
  const model = state.plans;
  const rows = model.items.map(plan => `<tr><td><button class="product-title" data-collection-action="plan" data-id="${esc(plan.id)}">${esc(plan.name)}</button><span class="cell-secondary">计划 #${esc(plan.id)}</span></td><td>${esc(collectionTaskTypes[plan.task_type] || plan.task_type)}<span class="cell-secondary">${num(plan.resource_ids?.length)} 个${plan.task_type === 'shop_profile' ? '店铺' : '商品'}${enabledTypes.includes(plan.task_type) ? '' : ' · 采集器未启用'}</span></td><td>${badge(plan.enabled ? '已启用' : '已停用', plan.enabled ? 'green' : '')}</td><td>${esc(planSchedule(plan))}<span class="cell-secondary">下次 ${time(plan.next_run_at)}</span></td><td class="event-time">${time(plan.last_run_at)}</td><td><div class="collection-row-actions">${button('run','立即运行',plan.id,disabled || (paused || !enabledTypes.includes(plan.task_type) ? 'disabled' : ''))}${button('toggle',plan.enabled ? '停用' : '启用',plan.id,disabled || (!plan.enabled && !enabledTypes.includes(plan.task_type) ? 'disabled' : ''))}${button('plan','详情',plan.id)}</div></td></tr>`).join('');
  const body = !model.loaded && model.loading ? loading() : rows ? `<div class="table-wrap"><table class="collection-plan-table"><thead><tr><th>计划</th><th>采集范围</th><th>调度状态</th><th>执行安排</th><th>上次派发</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table></div>` : empty('尚无采集计划','创建一个计划，为指定店铺补充档案，或周期性记录商品快照。');
  return `${header}${messages}${tabs}<section class="panel"><div class="toolbar"><span class="collection-table-label">${icon('layers')} 计划列表</span><span class="filter-label">按创建时间倒序 · 每页 20 条</span></div>${body}${pager('plans',model,'个计划')}</section><p class="collection-footnote">计划支持店铺档案、商品快照。停用计划只停止后续自动派发，已创建的任务可在执行记录中查看。</p>`;
}

function renderCollectionAudit(model) {
  const actions = {plan_created:'创建计划',plan_updated:'更新计划',plan_run:'手动运行计划',plan_run_requested:'手动运行计划',control_updated:'更新采集开关',collection_paused:'暂停采集',collection_resumed:'恢复采集',task_cancelled:'取消任务',task_retried:'重试任务'};
  const types = {plan:'采集计划',task:'采集任务',control:'市场调度',market:'市场调度'};
  const body = !model.loaded && model.loading ? loading() : model.error ? notice(model.error, true) : model.items.length ? `<div class="table-wrap"><table><thead><tr><th>操作时间</th><th>操作</th><th>对象</th></tr></thead><tbody>${model.items.map(row => `<tr><td class="event-time">${time(row.created_at)}</td><td>${esc(actions[row.action] || '采集配置变更')}</td><td>${esc(types[row.target_type] || row.target_type)} <span class="cell-secondary">${esc(row.target_id || '当前市场')}</span></td></tr>`).join('')}</tbody></table></div>` : empty('暂无操作记录','计划与任务的管理操作会记录在这里。');
  return `<section class="panel"><div class="panel-header"><div><h2 class="panel-title">操作记录</h2><p class="panel-subtitle">当前市场的管理变更，不展示令牌或请求内容</p></div></div>${body}${pager('audit',model,'条操作')}</section>`;
}

export function renderCollectionAuth(connected) {
  return `<h2 id="dialog-title">采集写入连接</h2><p class="detail-subtitle">使用服务已配置的 API 写入令牌。仅保留在当前页面内存，刷新或关闭页面后清除。</p><form id="collection-auth-form" class="collection-form"><label for="collection-token">API 写入令牌</label><input id="collection-token" type="password" autocomplete="off" spellcheck="false" required minlength="24" placeholder="输入已配置的写入令牌"><div id="collection-form-error" class="notice warning" role="alert" hidden></div><div class="collection-form-actions">${connected ? button('disconnect','断开连接') : ''}<button type="submit" class="button primary">加载令牌</button></div></form>`;
}

export function renderCollectionPlanForm(plan, control) {
  const edit = Boolean(plan?.id), enabledTypes = control?.enabled_task_types || [];
  const type = plan?.task_type || Object.keys(collectionTaskTypes).find(key => enabledTypes.includes(key)) || 'shop_profile';
  const minimum = control?.limits?.min_interval_seconds?.[type] || (type === 'shop_profile' ? 3600 : 60);
  const max = control?.limits?.max_resources || 100;
  return `<h2 id="dialog-title">${edit ? '编辑采集计划' : '新建采集计划'}</h2><p class="detail-subtitle">使用工作台的内部资源 ID，计划将保存到当前市场。</p><form id="collection-plan-form" class="collection-form" data-plan-id="${esc(plan?.id || '')}"><label for="collection-name">计划名称</label><input id="collection-name" name="name" maxlength="80" required value="${esc(plan?.name || '')}" placeholder="例如：重点店铺每小时档案更新"><div class="collection-form-grid"><div><label for="collection-type">采集类型</label><select id="collection-type" name="task_type" ${edit ? 'disabled' : ''}>${Object.entries(collectionTaskTypes).map(([key,label]) => `<option value="${key}" ${key === type ? 'selected' : ''} ${enabledTypes.includes(key) ? '' : 'disabled'}>${label}${enabledTypes.includes(key) ? '' : '（未启用）'}</option>`).join('')}</select></div><div><label for="collection-schedule">执行方式</label><select id="collection-schedule" name="schedule_mode" ${edit ? 'disabled' : ''}><option value="once" ${plan?.schedule_mode !== 'interval' ? 'selected' : ''}>执行一次</option><option value="interval" ${plan?.schedule_mode === 'interval' ? 'selected' : ''}>按间隔执行</option></select></div></div><label id="collection-resources-label" for="collection-resources">${type === 'shop_profile' ? '店铺' : '商品'}内部 ID</label><textarea id="collection-resources" name="resource_ids" rows="4" required aria-describedby="collection-resource-hint" placeholder="例如：12, 37, 58">${esc((plan?.resource_ids || []).join(', '))}</textarea><p id="collection-resource-hint" class="collection-form-hint">以逗号或换行分隔，最多 ${max} 个。可从店铺或商品详情核对内部 ID。</p><div id="collection-interval-field" ${plan?.schedule_mode === 'interval' ? '' : 'hidden'}><label for="collection-interval">执行间隔（分钟）</label><input id="collection-interval" name="interval_minutes" type="number" ${plan?.schedule_mode === 'interval' ? '' : 'disabled'} min="${minimum / 60}" max="${(control?.limits?.max_interval_seconds || 604800) / 60}" step="1" value="${(plan?.interval_seconds || minimum) / 60}"><p id="collection-interval-hint" class="collection-form-hint">最短 ${minimum / 60} 分钟，最长 7 天。上次仍在执行时不会重复派发。</p></div><label class="collection-check"><input id="collection-enabled" type="checkbox" name="enabled" ${!plan || plan.enabled ? 'checked' : ''}>保存后启用自动调度</label><div id="collection-form-error" class="notice warning" role="alert" hidden></div><div class="collection-form-actions">${button('close','取消')}<button type="submit" class="button primary">保存计划</button></div></form>`;
}

export function renderCollectionPlanDetail(plan, runs, canWrite) {
  const resources = plan.resource_ids || [];
  const disabled = canWrite ? '' : 'disabled';
  const rows = runs.items.map(run => `<tr><td>${time(run.scheduled_for)}</td><td>${run.trigger === 'manual' ? '手动' : '计划'}</td><td>${runBadge(run.status)}</td><td class="num">${num(run.task_count)}</td><td>${button('run-detail','查看',run.id)}</td></tr>`).join('');
  return `<h2 id="dialog-title">${esc(plan.name)}</h2><p class="detail-subtitle">计划 #${esc(plan.id)} · ${esc(collectionTaskTypes[plan.task_type] || plan.task_type)}</p><div class="collection-detail-actions">${badge(plan.enabled ? '已启用' : '已停用',plan.enabled ? 'green' : '')}${button('edit','编辑计划',plan.id,disabled)}${button('run','立即运行',plan.id,disabled)}</div><div class="detail-grid">${field('执行安排',planSchedule(plan))}${field('下次自动派发',formatDate(plan.next_run_at))}${field('上次派发',formatDate(plan.last_run_at))}${field('创建时间',formatDate(plan.created_at))}</div><div class="detail-section"><h3>${plan.task_type === 'shop_profile' ? '店铺' : '商品'}内部 ID · ${resources.length} 个</h3><div class="collection-resource-list">${resources.map(id => `<span class="context-tag">${esc(id)}</span>`).join('')}</div></div><div class="detail-section"><h3>执行记录</h3>${notice('记录统计该次派发的直接任务；后续自动产生的数据处理任务不计入本次完成状态。')}${runs.error ? notice(runs.error,true) : ''}${runs.loading && !runs.loaded ? loading() : rows ? `<div class="table-wrap"><table><thead><tr><th>计划时间</th><th>触发</th><th>状态</th><th>任务数</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>` : empty('暂无执行记录','自动调度或手动运行后，将显示派发的任务。')}${pager('runs',runs,'次执行')}</div>`;
}

export function collectionTaskActions(task, canWrite) {
  const supported = ['shop_profile','product_snapshot','product_detail','product_discovery'].includes(task.task_type);
  if (!supported) return '';
  const disabled = canWrite ? '' : 'disabled';
  if (['pending','retry_waiting','abandoned'].includes(task.status)) return button('cancel-task','取消任务',task.task_id,disabled);
  if (['dead','cancelled'].includes(task.status)) return button('retry-task','重试任务',task.task_id,disabled);
  return '';
}

export function renderCollectionRun(run, canWrite) {
  const taskRow = task => `<tr><td>${taskLink(task)}<span class="cell-secondary">${esc(task.resource_id)}</span></td><td>${taskBadge(task.status)}</td><td>${num(task.attempt_count)}</td><td>${collectionTaskActions(task,canWrite)}</td></tr>`;
  const rows = (run.tasks || []).map(taskRow).join('') + ((run.retry_tasks || []).length ? `<tr><th colspan="4">派生重试任务 · ${num(run.retry_task_count)}</th></tr>${run.retry_tasks.map(taskRow).join('')}` : '');
  return `<h2 id="dialog-title">执行记录 #${esc(run.id)}</h2><p class="detail-subtitle">${run.trigger === 'manual' ? '手动运行' : '自动调度'} · ${time(run.scheduled_for)}</p><div class="collection-detail-actions">${runBadge(run.status)}${button('plan','返回计划',run.plan_id)}${button('run-detail','刷新状态',run.id)}</div><div class="detail-grid">${field('请求资源数',num(run.requested_count ?? run.task_count))}${field('直接任务数',num(run.task_count))}${field('成功任务',num(run.status_counts?.succeeded || 0))}${field('等待 / 重试',num((run.status_counts?.pending || 0)+(run.status_counts?.retry_waiting || 0)))}${field('执行中',num(run.status_counts?.running || 0))}${run.retry_task_count ? field('派生重试 / 仍在执行',`${num(run.retry_task_count)} / ${num(run.active_retry_count)}`) : ''}</div><div class="detail-section">${run.error_code ? notice(collectionErrorMessage({code:run.error_code}),true) : ''}${notice('取消仅适用于尚未执行的任务。重试任务另列，重试成功不改写最初派发任务的历史状态。')}${rows ? `<div class="table-wrap"><table><thead><tr><th>任务 / 资源</th><th>状态</th><th>尝试次数</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table></div>` : empty('暂无直接任务',run.status === 'failed' ? '本次运行未能派发任务，请检查资源和采集器配置。' : '稍后刷新查看任务派发结果。')}</div>`;
}

export function renderCollectionConfirmation(kind, item, paused) {
  const config = kind === 'control' ? {title:paused ? '恢复来源采集' : '暂停来源采集',description:paused ? '恢复后，待执行任务和已启用计划将继续调度。' : '停止开始新的来源采集。正在执行的请求继续完成，本地数据处理保持运行。',label:paused ? '恢复采集' : '暂停采集'} : kind === 'run' ? {title:'立即运行采集计划',description:`为「${item.name}」的 ${item.resource_ids?.length || 0} 个资源派发一次采集。计划原有的执行安排保持有效。`,label:'派发任务'} : kind === 'cancel-task' ? {title:'取消待执行任务',description:'取消后不再执行该任务。已保存的采集数据和尝试记录会保留。',label:'取消任务'} : {title:'重新执行采集任务',description:'创建一个新的重试任务并保留原任务历史。相同原任务重复提交只会创建一次重试。',label:'创建重试任务'};
  return `<h2 id="dialog-title">${config.title}</h2><p class="detail-subtitle">${esc(config.description)}</p><div id="collection-form-error" class="notice warning" role="alert" hidden></div><div class="collection-form-actions">${button('close','返回')}<button class="button primary" data-collection-action="confirm">${config.label}</button></div>`;
}

function monitorMetric(label,value,caption,symbol,icon) {
  return `<div class="kpi"><div class="kpi-label">${esc(label)}${icon(symbol)}</div><div class="kpi-value">${value}</div><div class="kpi-caption">${esc(caption)}</div></div>`;
}

function renderTrend(data) {
  const buckets = data.trend || [];
  const maximum = Math.max(1,...buckets.map(bucket => Number(bucket.completed) || 0));
  if (!buckets.length) return empty('窗口内暂无完成记录','任务完成后，采集吞吐趋势会显示在这里。');
  return `<div class="collection-trend" role="img" aria-label="${esc(`${data.window?.minutes || 60} 分钟内成功 ${data.attempts?.succeeded || 0} 次，失败 ${data.attempts?.failed || 0} 次`)}">${buckets.map(bucket => {const success = Math.max(0,Number(bucket.succeeded)||0), failed = Math.max(0,Number(bucket.failed)||0);return `<div class="collection-trend-slot" title="${time(bucket.time)} · 成功 ${num(success)} · 失败 ${num(failed)}"><div class="collection-trend-bar"><span class="failed" style="height:${failed/maximum*100}%"></span><span class="succeeded" style="height:${success/maximum*100}%"></span></div></div>`;}).join('')}</div><div class="collection-trend-axis"><span>${time(buckets[0].time)}</span><span>${time(buckets.at(-1).time)}</span></div>`;
}

export function renderCollectionMonitor(state, icon) {
  const data = state.data;
  const toolbar = `<div class="collection-monitor-toolbar"><div class="collection-live"><span class="status-dot${data && !state.error ? ' online' : ''}"></span><span>${state.polling ? '每 15 秒自动刷新' : '自动刷新已暂停'}</span><small>离开页面后停止读取</small></div><div class="collection-monitor-tools"><select id="collection-monitor-window" aria-label="采集监控统计窗口">${[[15,'最近 15 分钟'],[60,'最近 1 小时'],[360,'最近 6 小时'],[1440,'最近 24 小时']].map(([value,label]) => `<option value="${value}" ${state.windowMinutes === value ? 'selected' : ''}>${label}</option>`).join('')}</select>${button('poll',`${icon(state.polling ? 'clock' : 'refresh')}${state.polling ? '暂停刷新' : '恢复刷新'}`)}<a class="button" href="#collections">管理采集 ${icon('arrow')}</a></div></div>`;
  const warning = state.error ? notice(`${state.error}${data ? ` 当前显示上次结果，采样于 ${formatDate(data.generated_at)}。` : ''}`,true) : '';
  if (!data) return `${toolbar}${warning}<section class="panel">${state.loading ? loading() : empty('暂无运行数据','点击右上角刷新数据重新连接采集服务。')}</section>`;
  const q = data.queue || {}, attempts = data.attempts || {}, runtime = data.runtime || {}, leases = data.leases || {};
  const pending = q.status === 'pending' ? null : (q.by_status?.pending || 0)+(q.by_status?.retry_waiting || 0);
  const minutes = data.window?.minutes || state.windowMinutes;
  const queueFreshness = q.status === 'pending' ? notice('队列统计正在准备，当前任务总量暂不可用。') : q.status === 'stale' ? notice(`队列统计为上次结果，采样于 ${formatDate(q.snapshot_at)}。`,true) : '';
  const sample = attempts.truncated ? notice(`窗口内完成记录超过 ${num(attempts.limit)} 条，仅统计最近样本，吞吐数为该窗口的下限，成功率和错误分布并非全量。`,true) : '';
  const metrics = `<div class="kpi-grid collection-monitor-kpis">${monitorMetric('等待执行 / 重试',num(pending),'当前队列 · 直接任务与后续任务','clock',icon)}${monitorMetric('正在执行',num(q.status === 'pending' ? null : q.by_status?.running || 0),`租约过期 ${leases.truncated ? '至少 ' : ''}${num(leases.expired)} 个`,'pulse',icon)}${monitorMetric('窗口成功率',attempts.success_rate == null ? '—' : `${num(attempts.success_rate)}<small>%</small>`,`${minutes} 分钟内 ${num(attempts.total)} 次完成尝试`,'check',icon)}${monitorMetric('采集吞吐',`${num(attempts.throughput_per_minute)}<small>/ 分钟</small>`,`平均耗时 ${duration(attempts.avg_duration_ms)} · P95 ${duration(attempts.p95_duration_ms)}`,'activity',icon)}</div>`;
  const alerts = (data.alerts || []).map(alert => notice(alert.message || '部分采集状态需要关注。',alert.severity !== 'info')).join('');
  const types = new Map();
  for (const row of q.by_type || []) {if(!types.has(row.task_type))types.set(row.task_type,{});types.get(row.task_type)[row.status]=(types.get(row.task_type)[row.status]||0)+row.count;}
  const queueRows = [...types].map(([type,counts]) => `<tr><td>${esc(labels.taskTypes[type] || type)}</td><td class="num">${num((counts.pending||0)+(counts.retry_waiting||0))}</td><td class="num">${num(counts.running||0)}</td><td class="num">${num(counts.succeeded||0)}</td><td class="num">${num((counts.dead||0)+(counts.abandoned||0))}</td></tr>`).join('');
  const queue = `<section class="panel"><div class="panel-header"><div><h2 class="panel-title">任务队列</h2><p class="panel-subtitle">当前保存的任务状态 · ${num(q.total)} 个任务</p></div><a class="text-link" href="#tasks">任务明细 ${icon('arrow')}</a></div>${queueRows ? `<div class="table-wrap collection-panel-body"><table><thead><tr><th>任务类型</th><th>等待</th><th>运行</th><th>成功</th><th>失败 / 失效</th></tr></thead><tbody>${queueRows}</tbody></table></div>` : empty('暂无队列统计','统计准备完成后显示各类型任务状态。')}</section>`;
  const errors = (data.errors || []).slice(0,8), errorTotal = errors.reduce((sum,row)=>sum+row.count,0);
  const errorPanel = `<section class="panel"><div class="panel-header"><div><h2 class="panel-title">失败原因分布</h2><p class="panel-subtitle">窗口内失败尝试 · 按错误类型聚合</p></div>${badge(`${num(attempts.failed)} 次`,attempts.failed ? 'amber' : '')}</div><div class="collection-error-list">${errors.length ? errors.map(row => `<div class="collection-error-item"><div><span>${esc(row.error_type || 'unknown')}</span><strong class="num">${num(row.count)}</strong></div><span class="collection-error-track"><span style="width:${errorTotal ? row.count/errorTotal*100 : 0}%"></span></span></div>`).join('') : empty('窗口内无失败记录','没有记录到已完成的失败尝试。')}</div></section>`;
  const recent = (data.recent_failures || []).slice(0,10);
  const failureTable = `<section class="panel"><div class="panel-header"><div><h2 class="panel-title">最近失败尝试</h2><p class="panel-subtitle">查看任务详情，核对响应状态和重试进度</p></div></div>${recent.length ? `<div class="table-wrap collection-panel-body"><table><thead><tr><th>任务</th><th>采集类型</th><th>错误类型</th><th>响应</th><th>完成时间</th></tr></thead><tbody>${recent.map(row => `<tr><td>${taskLink(row)}<span class="cell-secondary">第 ${num(row.attempt_no)} 次尝试</span></td><td>${esc(labels.taskTypes[row.task_type] || row.task_type)}</td><td>${esc(row.error_type || 'unknown')}</td><td class="num">${num(row.response_status)}</td><td class="event-time">${time(row.finished_at)}</td></tr>`).join('')}</tbody></table></div>` : empty('暂无失败尝试','窗口内完成的失败记录会显示在这里。')}</section>`;
  const leaseRows = (leases.items || []).filter(item => item.expired);
  const stalled = leaseRows.length ? `<section class="panel collection-stalled"><div class="panel-header"><div><h2 class="panel-title">租约已过期的任务</h2><p class="panel-subtitle">执行权已失效，等待运行层恢复处理</p></div>${badge(`${num(leases.expired)} 个`,'amber')}</div><div class="table-wrap collection-panel-body"><table><thead><tr><th>任务</th><th>类型</th><th>租约到期</th></tr></thead><tbody>${leaseRows.slice(0,10).map(row => `<tr><td>${taskLink(row)}</td><td>${esc(labels.taskTypes[row.task_type] || row.task_type)}</td><td class="event-time">${time(row.lease_expires_at)}</td></tr>`).join('')}</tbody></table></div>${leases.truncated ? '<p class="collection-footnote">租约列表达到读取上限，仅显示部分任务。</p>' : ''}</section>` : '';
  return `${toolbar}${warning}${queueFreshness}${sample}${alerts}${metrics}<div class="collection-monitor-grid"><section class="panel collection-trend-panel"><div class="panel-header"><div><h2 class="panel-title">完成尝试趋势</h2><p class="panel-subtitle">每次重试分别计数 · ${minutes} 分钟窗口</p></div><div class="collection-legend"><span class="success">成功</span><span class="failure">失败</span></div></div>${renderTrend(data)}</section>${renderRuntime(runtime,icon)}</div><div class="collection-monitor-grid collection-queue-grid">${queue}${errorPanel}</div>${stalled}${failureTable}<p class="collection-footnote">采样时间 ${time(data.generated_at)} · 成功率按完成尝试计算，不代表全部目标的采集覆盖率。</p>`;
}

function renderRuntime(runtime,icon) {
  const statusLabels = {online:'心跳正常',stale:'心跳延迟',offline:'心跳超时'};
  const roles = {worker:'采集 Worker',scheduler:'调度器',collection:'采集协调器'};
  const components = runtime.components || [];
  const body = runtime.status !== 'available' ? empty('运行心跳暂不可用',runtime.status === 'not_configured' ? '服务尚未配置心跳采集。' : '心跳存储暂不可用，无法判断进程在线状态。') : !components.length ? empty('尚未观察到进程心跳','等待采集进程首次上报，当前不判断为离线。') : `<div class="collection-runtime-list">${components.map(item => `<div class="collection-runtime-item"><span class="collection-runtime-icon">${icon(item.role === 'scheduler' ? 'clock' : 'pulse')}</span><div><strong>${esc(roles[item.role] || item.role)}</strong><span title="${esc(item.id)}">${esc(item.id)}</span><small>${item.concurrency == null ? '最近上报' : `${num(item.active_tasks)} / ${num(item.concurrency)} 并发占用 ·`} ${time(item.seen_at)}</small></div>${badge(statusLabels[item.status] || '未知',item.status === 'online' ? 'green' : 'amber')}</div>`).join('')}</div>`;
  return `<section class="panel"><div class="panel-header"><div><h2 class="panel-title">采集进程</h2><p class="panel-subtitle">基于进程主动上报的心跳</p></div>${icon('pulse')}</div>${body}${runtime.source_cooldown_seconds > 0 ? `<div class="collection-runtime-note">来源冷却中 · 剩余 ${num(runtime.source_cooldown_seconds)} 秒</div>` : ''}</section>`;
}
