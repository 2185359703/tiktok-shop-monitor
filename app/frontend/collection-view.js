import { formatDate, labels } from './api.js';
import {renderCollectionPlanEditor} from './collection-editors.js';
import {renderCollectionManagementPage} from './collection-management-view.js';

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
    collection_plan_archived:'该计划已归档，请先恢复后再操作。',
    collection_launch_not_found:'该单次采集不存在或不属于当前市场。',
    collection_launch_cancelled:'所属单次采集已取消，不能为该批次单独创建重试。需要重新采集时请创建新批次。',
    collection_alert_not_found:'该告警不存在或不属于当前市场，请刷新告警列表。',
    collection_run_not_found:'该执行记录不存在或不属于当前市场。',
    task_not_found:'该任务不存在或不属于当前市场。',
    collector_not_enabled:'当前服务尚未启用此类采集任务。',
    invalid_plan_resources:'部分资源不存在或不属于当前市场，请核对内部 ID。',
    invalid_launch_resources:'部分资源当前不可采集，请重新核对所选范围。',
    invalid_launch_scope:'采集范围不正确，请重新选择资源或核对店铺链接。',
    invalid_launch_budget:'采集页数超出服务允许范围，请按表单提示调整。',
    invalid_shop_entry:'仅支持不含账号凭据的美国站 TikTok Shop 店铺 HTTPS 链接。',
    collection_resource_busy:'同一资源已有同类任务等待或执行中，请先查看任务进度。',
    seed_already_processed:'该店铺入口已处理，请先查看现有任务和店铺记录。',
    unsupported_source:'仅支持已知美国站公开店铺 HTTPS 链接。',
    unsupported_plan_type:'此任务类型尚不支持计划调度。',
    invalid_plan_interval:'执行间隔不在允许范围内，请按表单提示调整。',
    collection_plan_busy:'该计划仍有任务等待或执行中，请完成后再运行。',
    invalid_task_transition:'任务状态已变化，当前操作不可执行，请刷新。',
    unsupported_task_retry:'此类任务暂不支持手动重试。',
    idempotency_conflict:'该次请求已用不同内容提交，请先刷新列表核对执行结果。',
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
  return `<div class="table-footer"><span>本页 ${num(model.items.length)} ${noun}</span><div class="pagination">${button(`${kind}-prev`,'上一页','',model.cursors.length === 1 || model.loading || model.error ? 'disabled' : '')}<span>第 ${model.cursors.length} 页</span>${button(`${kind}-next`,'下一页','',!model.next || model.loading || model.error ? 'disabled' : '')}</div></div>`;
}

function planSchedule(plan) {
  return plan.schedule_mode === 'once' ? '执行一次' : `每 ${num(Number(plan.interval_seconds) / 60)} 分钟`;
}

export function renderCollectionManagement(state, icon) {
  return renderCollectionManagementPage(state,icon);
}

export function renderCollectionAuth(connected) {
  return `<h2 id="dialog-title">采集写入连接</h2><p class="detail-subtitle">使用服务已配置的 API 写入令牌。仅保留在当前页面内存，刷新或关闭页面后清除。</p><form id="collection-auth-form" class="collection-form"><label for="collection-token">API 写入令牌</label><input id="collection-token" type="password" autocomplete="off" spellcheck="false" required minlength="24" placeholder="输入已配置的写入令牌"><div id="collection-form-error" class="notice warning" role="alert" hidden></div><div class="collection-form-actions">${connected ? button('disconnect','断开连接') : ''}<button type="submit" class="button primary">加载令牌</button></div></form>`;
}

export function renderCollectionPlanForm(plan, control, draft = plan) {
  return renderCollectionPlanEditor(plan,control,draft);
}

export function renderCollectionPlanDetail(plan, runs, canWrite) {
  const resources = plan.resource_ids || [];
  const disabled = canWrite && !plan.archived_at ? '' : 'disabled';
  const actions = plan.archived_at ? button('restore','恢复计划',plan.id,canWrite ? '' : 'disabled') : `${button('edit','编辑计划',plan.id,disabled)}${button('run','立即运行',plan.id,disabled)}${button('archive','归档计划',plan.id,disabled)}`;
  const rows = runs.items.map(run => `<tr><td>${time(run.scheduled_for)}</td><td>${run.trigger === 'manual' ? '手动' : '计划'}</td><td>${runBadge(run.status)}</td><td class="num">${num(run.task_count)}</td><td>${button('run-detail','查看',run.id)}</td></tr>`).join('');
  return `<h2 id="dialog-title">${esc(plan.name)}</h2><p class="detail-subtitle">计划 #${esc(plan.id)} · ${esc(collectionTaskTypes[plan.task_type] || plan.task_type)}</p><div class="collection-detail-actions">${badge(plan.archived_at ? '已归档' : plan.enabled ? '已启用' : '已停用',plan.enabled && !plan.archived_at ? 'green' : '')}${actions}</div><div class="detail-grid">${field('执行安排',planSchedule(plan))}${field('下次自动派发',formatDate(plan.next_run_at))}${field('上次派发',formatDate(plan.last_run_at))}${field('创建时间',formatDate(plan.created_at))}</div><div class="detail-section"><h3>${plan.task_type === 'shop_profile' ? '店铺' : '商品'}内部 ID · ${resources.length} 个</h3><div class="collection-resource-list">${resources.map(id => `<span class="context-tag">${esc(id)}</span>`).join('')}</div></div><div class="detail-section"><div class="collection-detail-actions"><h3>执行记录</h3>${button('runs-refresh','刷新记录','',runs.loading ? 'disabled' : '')}</div>${notice('记录统计该次派发的直接任务；后续自动产生的数据处理任务不计入本次完成状态。')}${runs.error ? notice(runs.error,true) : ''}${runs.loading && !runs.loaded ? loading() : rows ? `<div class="table-wrap"><table><thead><tr><th>计划时间</th><th>触发</th><th>状态</th><th>任务数</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>` : empty('暂无执行记录','自动调度或手动运行后，将显示派发的任务。')}${pager('runs',runs,'次执行')}</div>`;
}

export function collectionTaskActions(task, canWrite, {retryDisabled=false}={}) {
  const supported = ['shop_discovery','shop_profile','product_snapshot','product_detail','product_discovery','product_reviews'].includes(task.task_type);
  if (!supported) return '';
  const disabled = canWrite ? '' : 'disabled';
  if (['pending','retry_waiting','abandoned'].includes(task.status)) return button('cancel-task','取消任务',task.task_id,disabled);
  if (['dead','cancelled'].includes(task.status)) return button('retry-task','重试任务',task.task_id,canWrite&&!retryDisabled?'':'disabled');
  return '';
}

export function renderCollectionRun(run, canWrite) {
  const taskRow = task => `<tr><td>${taskLink(task)}<span class="cell-secondary">${esc(task.resource_id)}</span></td><td>${taskBadge(task.status)}</td><td>${num(task.attempt_count)}</td><td>${collectionTaskActions(task,canWrite)}</td></tr>`;
  const rows = (run.tasks || []).map(taskRow).join('') + ((run.retry_tasks || []).length ? `<tr><th colspan="4">派生重试任务 · ${num(run.retry_task_count)}</th></tr>${run.retry_tasks.map(taskRow).join('')}` : '');
  return `<h2 id="dialog-title">执行记录 #${esc(run.id)}</h2><p class="detail-subtitle">${run.trigger === 'manual' ? '手动运行' : '自动调度'} · ${time(run.scheduled_for)}</p><div class="collection-detail-actions">${runBadge(run.status)}${button('plan','返回计划',run.plan_id)}${button('run-detail','刷新状态',run.id)}</div><div class="detail-grid">${field('请求资源数',num(run.requested_count ?? run.task_count))}${field('直接任务数',num(run.task_count))}${field('成功任务',num(run.status_counts?.succeeded || 0))}${field('等待 / 重试',num((run.status_counts?.pending || 0)+(run.status_counts?.retry_waiting || 0)))}${field('执行中',num(run.status_counts?.running || 0))}${run.retry_task_count ? field('派生重试 / 仍在执行',`${num(run.retry_task_count)} / ${num(run.active_retry_count)}`) : ''}</div><div class="detail-section">${run.error_code ? notice(collectionErrorMessage({code:run.error_code}),true) : ''}${notice('取消仅适用于尚未执行的任务。重试任务另列，重试成功不改写最初派发任务的历史状态。')}${rows ? `<div class="table-wrap"><table><thead><tr><th>任务 / 资源</th><th>状态</th><th>尝试次数</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table></div>` : empty('暂无直接任务',run.status === 'failed' ? '本次运行未能派发任务，请检查资源和采集器配置。' : '稍后刷新查看任务派发结果。')}</div>`;
}

export function renderCollectionConfirmation(kind, item, paused) {
  const scheduleNote=item?.schedule_mode==='once'?'本次派发后，一次性计划将自动停用。':item?.enabled?'本次派发后，下次自动执行从现在起重新计算一个间隔。':'此计划保持停用，本次只手动执行一轮。';
  const options={
    control:{title:paused?'恢复来源采集':'暂停来源采集',description:paused?'恢复后，待执行任务和已启用计划继续调度。':'停止开始新的来源采集。正在执行的请求继续完成，本地数据处理保持运行。',label:paused?'恢复采集':'暂停采集'},
    run:{title:'立即运行采集计划',description:`为「${item?.name}」的 ${item?.resource_ids?.length||0} 个资源派发一次采集。${scheduleNote}`,label:'派发任务'},
    archive:{title:'归档采集计划',description:`归档「${item?.name}」后停止后续自动派发，并禁止新增运行与重试。已派发任务继续执行，历史与数据保留，可随时恢复。`,label:'归档计划'},
    restore:{title:'恢复采集计划',description:`恢复「${item?.name}」后保持停用。需要继续采集时，再启用调度或手动运行。`,label:'恢复计划'},
    'launch-cancel':{title:'取消本批次采集',description:`停止「${item?.name}」尚未开始的独占任务，并阻止其后续任务继续开始。正在运行的请求允许完成；被其它有效批次共享的任务继续执行，批次会在关联任务结束后完成取消。剩余待执行任务由调度器分批处理，任务记录保留。`,label:'请求取消本批次'},
    'alert-ack':{title:'确认采集告警',description:`确认已查看「${item?.message}」。确认不代表故障已解决；条件恢复后系统记录恢复，再次发生时会重新打开。`,label:'确认已查看'},
    'cancel-task':{title:'取消待执行任务',description:'取消后不再执行该任务。已保存的采集数据和尝试记录保留。',label:'取消任务'},
    'retry-task':{title:'重新执行采集任务',description:'创建新的重试任务并保留原任务历史。重复提交同一原任务只创建一次重试。',label:'创建重试任务'},
  };
  const config=options[kind];
  return `<h2 id="dialog-title">${config.title}</h2><p class="detail-subtitle">${esc(config.description)}</p><div id="collection-form-error" class="notice warning" role="alert" hidden></div><div class="collection-form-actions">${button('close','返回')}<button class="button primary" data-collection-action="confirm">${config.label}</button></div>`;
}

export function renderCollectionReadError(title,action,id,message) {
  return `<h2 id="dialog-title">${esc(title)}</h2>${notice(message,true)}<div class="collection-form-actions">${button('close','返回')}${button(action,'重新读取',id)}</div>`;
}

function monitorMetric(label,value,caption,symbol,icon) {
  return `<div class="kpi"><div class="kpi-label">${esc(label)}${icon(symbol)}</div><div class="kpi-value">${value}</div><div class="kpi-caption">${esc(caption)}</div></div>`;
}

function renderTrend(data) {
  if(data.statistics?.ready === false)return empty('分钟统计正在准备','汇总就绪后显示完整分钟内的尝试数量。');
  const buckets = data.trend || [];
  const maximum = Math.max(1,...buckets.map(bucket => Number(bucket.completed) || 0));
  if (!buckets.length) return empty('窗口内暂无完成记录','任务完成后，采集吞吐趋势会显示在这里。');
  return `<div class="collection-trend" role="img" aria-label="${esc(`${data.window?.minutes || 60} 分钟内成功 ${data.attempts?.succeeded || 0} 次，失败 ${data.attempts?.failed || 0} 次`)}">${buckets.map(bucket => {const success = Math.max(0,Number(bucket.succeeded)||0), failed = Math.max(0,Number(bucket.failed)||0);return `<div class="collection-trend-slot" title="${time(bucket.time)} · 成功 ${num(success)} · 失败 ${num(failed)}"><div class="collection-trend-bar"><span class="failed" style="height:${failed/maximum*100}%"></span><span class="succeeded" style="height:${success/maximum*100}%"></span></div></div>`;}).join('')}</div><div class="collection-trend-axis"><span>${time(buckets[0].time)}</span><span>${time(buckets.at(-1).time)}</span></div>`;
}

export function renderCollectionMonitor(state, icon) {
  const data = state.data;
  const group = {source:'来源采集',local:'本地处理',all:'全部类型'}[state.taskGroup || 'source'];
  const groupSelect = `<select id="collection-monitor-group" aria-label="监控任务分组">${[['source','来源采集'],['local','本地处理'],['all','全部类型']].map(([value,label])=>`<option value="${value}" ${(state.taskGroup || 'source')===value?'selected':''}>${label}</option>`).join('')}</select>`;
  const toolbar = `<div class="collection-monitor-toolbar"><div class="collection-live"><span class="status-dot${data && !state.error ? ' online' : ''}"></span><span>${state.polling ? '每 15 秒自动刷新' : '自动刷新已暂停'}</span><small>离开页面后停止读取</small></div><div class="collection-monitor-tools">${groupSelect}<select id="collection-monitor-window" aria-label="采集监控统计窗口">${[[15,'最近 15 分钟'],[60,'最近 1 小时'],[360,'最近 6 小时'],[1440,'最近 24 小时']].map(([value,label]) => `<option value="${value}" ${state.windowMinutes === value ? 'selected' : ''}>${label}</option>`).join('')}</select>${button('poll',`${icon(state.polling ? 'clock' : 'refresh')}${state.polling ? '暂停刷新' : '恢复刷新'}`)}<a class="button" href="#collections">管理采集 ${icon('arrow')}</a></div></div>`;
  const warning = state.error ? notice(`${state.error}${data ? ` 当前显示上次结果，采样于 ${formatDate(data.generated_at)}。` : ''}`,true) : '';
  if (!data) return `${toolbar}${warning}<section class="panel">${state.loading ? loading() : empty('暂无运行数据','点击右上角刷新数据重新连接采集服务。')}</section>`;
  const q = data.queue || {}, attempts = data.attempts || {}, runtime = data.runtime || {}, leases = data.leases || {}, statistics=data.statistics || {};
  const ready=statistics.ready !== false;
  const statisticsNotice=!ready ? notice('分钟统计正在准备，吞吐、成功率与耗时暂不可用。队列和进程状态仍可查看。') : statistics.status==='stale' ? notice(`分钟统计更新延迟，当前显示 ${formatDate(statistics.snapshot_at)} 的汇总。`,true) : '';
  const pending = q.status === 'pending' ? null : (q.by_status?.pending || 0)+(q.by_status?.retry_waiting || 0)+(q.by_status?.abandoned || 0);
  const minutes = data.window?.minutes || state.windowMinutes;
  const queueFreshness = q.status === 'pending' ? notice('队列统计正在准备，当前任务总量暂不可用。') : q.status === 'stale' ? notice(`队列统计为上次结果，采样于 ${formatDate(q.snapshot_at)}。`,true) : '';
  const sample = attempts.truncated ? notice(`窗口内完成记录超过 ${num(attempts.limit)} 条，仅统计最近样本，吞吐数为该窗口的下限，成功率和错误分布并非全量。`,true) : '';
  const metrics = `<div class="kpi-grid collection-monitor-kpis">${monitorMetric('等待执行 / 重试',num(pending),'全部类型 · 当前任务状态','clock',icon)}${monitorMetric('正在执行',num(q.status === 'pending' ? null : q.by_status?.running || 0),`租约过期 ${leases.truncated ? '至少 ' : ''}${num(leases.expired)} 个`,'pulse',icon)}${monitorMetric('窗口成功率',attempts.success_rate == null ? '—' : `${num(attempts.success_rate)}<small>%</small>`,`${group} · ${minutes} 分钟内 ${num(attempts.total)} 次完成尝试`,'check',icon)}${monitorMetric(`${group}吞吐`,`${num(attempts.throughput_per_minute)}<small>/ 分钟</small>`,`平均耗时 ${duration(attempts.avg_duration_ms)} · P95${statistics.p95_approximate?'近似上界':''} ${duration(attempts.p95_duration_ms)}`,'activity',icon)}</div>`;
  const alerts = (data.alerts || []).map(alert => notice(alert.message || '部分采集状态需要关注。',alert.severity !== 'info')).join('');
  const types = new Map();
  for (const row of q.by_type || []) {if(!types.has(row.task_type))types.set(row.task_type,{});types.get(row.task_type)[row.status]=(types.get(row.task_type)[row.status]||0)+row.count;}
  const queueRows = [...types].map(([type,counts]) => `<tr><td>${esc(labels.taskTypes[type] || type)}</td><td class="num">${num((counts.pending||0)+(counts.retry_waiting||0)+(counts.abandoned||0))}</td><td class="num">${num(counts.running||0)}</td><td class="num">${num(counts.succeeded||0)}</td><td class="num">${num(counts.dead||0)}</td></tr>`).join('');
  const queue = `<section class="panel"><div class="panel-header"><div><h2 class="panel-title">任务队列</h2><p class="panel-subtitle">全部类型 / 当前状态 · ${num(q.total)} 个任务</p></div><a class="text-link" href="#tasks">任务明细 ${icon('arrow')}</a></div>${queueRows ? `<div class="table-wrap collection-panel-body"><table><thead><tr><th>任务类型</th><th>等待 / 重试</th><th>运行</th><th>成功</th><th>失败终止</th></tr></thead><tbody>${queueRows}</tbody></table></div>` : empty('暂无队列统计','统计准备完成后显示各类型任务状态。')}</section>`;
  const errors = (data.errors || []).slice(0,8), errorTotal = errors.reduce((sum,row)=>sum+row.count,0);
  const errorPanel = `<section class="panel"><div class="panel-header"><div><h2 class="panel-title">失败原因分布</h2><p class="panel-subtitle">${esc(group)} · 窗口内失败尝试</p></div>${badge(`${num(attempts.failed)} 次`,attempts.failed ? 'amber' : '')}</div><div class="collection-error-list">${errors.length ? errors.map(row => `<div class="collection-error-item"><div><span>${esc(row.error_type || 'unknown')}</span><strong class="num">${num(row.count)}</strong></div><span class="collection-error-track"><span style="width:${errorTotal ? row.count/errorTotal*100 : 0}%"></span></span></div>`).join('') : empty(ready?'窗口内无失败记录':'失败统计正在准备',ready?'没有记录到已完成的失败尝试。':'等待分钟汇总就绪。')}</div></section>`;
  const recent = (data.recent_failures || []).slice(0,10);
  const failureTable = `<section class="panel"><div class="panel-header"><div><h2 class="panel-title">最近失败尝试</h2><p class="panel-subtitle">查看任务详情，核对响应状态和重试进度</p></div></div>${recent.length ? `<div class="table-wrap collection-panel-body"><table><thead><tr><th>任务</th><th>采集类型</th><th>错误类型</th><th>响应</th><th>完成时间</th></tr></thead><tbody>${recent.map(row => `<tr><td>${taskLink(row)}<span class="cell-secondary">第 ${num(row.attempt_no)} 次尝试</span></td><td>${esc(labels.taskTypes[row.task_type] || row.task_type)}</td><td>${esc(row.error_type || 'unknown')}</td><td class="num">${num(row.response_status)}</td><td class="event-time">${time(row.finished_at)}</td></tr>`).join('')}</tbody></table></div>` : empty(ready?'暂无失败尝试':'失败记录正在准备',ready?'窗口内完成的失败记录会显示在这里。':'等待分钟汇总就绪。')}</section>`;
  const leaseRows = (leases.items || []).filter(item => item.expired);
  const stalled = leaseRows.length ? `<section class="panel collection-stalled"><div class="panel-header"><div><h2 class="panel-title">租约已过期的任务</h2><p class="panel-subtitle">执行权已失效，等待运行层恢复处理</p></div>${badge(`${num(leases.expired)} 个`,'amber')}</div><div class="table-wrap collection-panel-body"><table><thead><tr><th>任务</th><th>类型</th><th>租约到期</th></tr></thead><tbody>${leaseRows.slice(0,10).map(row => `<tr><td>${taskLink(row)}</td><td>${esc(labels.taskTypes[row.task_type] || row.task_type)}</td><td class="event-time">${time(row.lease_expires_at)}</td></tr>`).join('')}</tbody></table></div>${leases.truncated ? '<p class="collection-footnote">租约列表达到读取上限，仅显示部分任务。</p>' : ''}</section>` : '';
  return `${toolbar}${warning}${queueFreshness}${statisticsNotice}${sample}${alerts}${metrics}<div class="collection-monitor-grid"><section class="panel collection-trend-panel"><div class="panel-header"><div><h2 class="panel-title">完成尝试趋势</h2><p class="panel-subtitle">${esc(group)} · 每次重试分别计数 · 完整分钟窗口</p></div><div class="collection-legend"><span class="success">成功</span><span class="failure">失败</span></div></div>${renderTrend(data)}</section>${renderRuntime(runtime,icon)}</div><div class="collection-monitor-grid collection-queue-grid">${queue}${errorPanel}</div>${stalled}${failureTable}<p class="collection-footnote">统计区间 ${time(data.window?.since)} 至 ${time(data.window?.until)}（不含结束时刻） · 采样 ${time(data.generated_at)}。成功率按完成尝试计算，不代表目标覆盖率。${statistics.p95_approximate?` P95 为直方图近似上界，最大相对误差约 ${num((statistics.p95_max_relative_error||0.1)*100)}%。`:''}</p>`;
}

function renderRuntime(runtime,icon) {
  const statusLabels = {online:'心跳正常',stale:'心跳延迟',offline:'心跳超时'};
  const roles = {worker:'采集 Worker',scheduler:'调度器',collection:'采集协调器'};
  const states = {running:'运行中',starting:'启动中',connecting:'连接中',stopping:'停止中',stopped:'已停止',disabled:'未启用'};
  const components = runtime.components || [];
  const body = runtime.status !== 'available' ? empty('运行心跳暂不可用',runtime.status === 'not_configured' ? '服务尚未配置心跳采集。' : '心跳存储暂不可用，无法判断进程在线状态。') : !components.length ? empty('尚未观察到进程心跳','等待采集进程首次上报，当前不判断为离线。') : `<div class="collection-runtime-list">${components.map(item => `<div class="collection-runtime-item"><span class="collection-runtime-icon">${icon(item.role === 'scheduler' ? 'clock' : 'pulse')}</span><div><strong>${esc(roles[item.role] || item.role)}</strong><span title="${esc(item.id)}">${esc(item.id)}</span><small>${item.concurrency == null ? '最近上报' : `${num(item.active_tasks)} / ${num(item.concurrency)} 并发占用 ·`} ${time(item.seen_at)}</small><small>${esc(states[item.state] || item.state || '状态未知')}${item.role==='scheduler'?` · 上次成功 ${time(item.last_success_at)}`:''}</small></div>${badge(item.ready ? '运行就绪' : item.status==='online' ? '未就绪' : statusLabels[item.status] || '未知',item.ready ? 'green' : 'amber')}</div>`).join('')}</div>`;
  return `<section class="panel"><div class="panel-header"><div><h2 class="panel-title">采集进程</h2><p class="panel-subtitle">${runtime.ready_counts ? `就绪 Worker ${num(runtime.ready_counts.worker)} · 调度器 ${num(runtime.ready_counts.scheduler)}` : '基于进程主动上报的心跳'}</p></div>${icon('pulse')}</div>${body}${runtime.source_cooldown_seconds > 0 ? `<div class="collection-runtime-note">来源冷却中 · 剩余 ${num(runtime.source_cooldown_seconds)} 秒</div>` : ''}</section>`;
}
