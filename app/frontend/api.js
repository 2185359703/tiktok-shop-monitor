const API_ROOT = '/api/v1';
const modulePath = new URL(import.meta.url).pathname;
const deploymentPrefix = modulePath.includes('/ui-assets/') ? modulePath.slice(0, modulePath.indexOf('/ui-assets/')) : '';
export function deploymentPath(path) { return `${deploymentPrefix}${path}`; }

const errorMessages = Object.freeze({
  analytics_backfill_pending: '数据视图正在准备，完成后请刷新此页。',
  analytics_not_enabled: '新数据视图尚未启用，请稍后重试。',
  product_not_found: '该商品不存在或不属于当前市场。',
  shop_not_found: '该店铺不存在或不属于当前市场。',
  task_not_found: '该任务不存在或不属于当前市场。',
  snapshot_not_found: '该历史快照不存在或不属于当前市场。',
  report_task_not_found: '该报告任务不存在或不属于当前市场。',
  report_not_completed: '报告仍在生成，请稍后查看。',
  report_file_not_recorded: '尚未记录该格式的报告文件。',
  report_file_not_available: '报告文件暂不可用。',
  collection_not_started: '当前市场尚未启动采集批次。',
  collection_not_in_this_market: '当前市场尚无采集批次。',
});

export class ApiError extends Error {
  constructor(status, code, message) {
    super(message || errorMessages[code] || friendlyStatus(status));
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
  }
}

function friendlyStatus(status) {
  if (status === 0) return '无法连接服务，请检查网络后重试。';
  if (status === 401 || status === 403) return '当前请求没有访问权限。';
  if (status === 404) return '请求的数据暂不存在。';
  if (status === 422) return '查询参数不正确，请调整后重试。';
  if (status === 429) return '请求较频繁，请稍后重试。';
  if (status >= 500) return '服务暂时不可用，请稍后重试。';
  return '数据请求失败，请重试。';
}

function apiUrl(path) {
  if (typeof path !== 'string' || !path || /^[a-z][a-z\d+.-]*:/i.test(path) || path.startsWith('//')) {
    throw new ApiError(0, 'invalid_path', '查询地址不正确。');
  }
  const origin = globalThis.location?.origin || 'http://localhost';
  const relative = /^\/api\/v[12](?:\/|\?|$)/.test(path)
    ? path : `${API_ROOT}/${path.replace(/^\/+/, '')}`;
  const url = new URL(relative, origin);
  if (url.origin !== origin || !/^\/api\/v[12](?:\/|$)/.test(url.pathname) || url.hash) {
    throw new ApiError(0, 'invalid_path', '查询地址不正确。');
  }
  url.pathname = deploymentPath(url.pathname);
  return url;
}

/** Same-origin GET only. Server error details are converted to safe display text. */
export async function request(path, { signal, timeout = 8000 } = {}) {
  const url = apiUrl(path);
  const deadline = AbortSignal.timeout(timeout);
  const combined = signal ? AbortSignal.any([signal, deadline]) : deadline;
  let response;
  try {
    response = await fetch(`${url.pathname}${url.search}`, {
      method: 'GET', credentials: 'same-origin', cache: 'no-store',
      headers: { Accept: 'application/json' }, signal: combined,
    });
  } catch (error) {
    if (error?.name === 'AbortError' || signal?.aborted) throw error;
    if (deadline.aborted) throw new ApiError(0, 'request_timeout', '读取超时，请稍后重试；已显示的数据仍然保留。');
    throw new ApiError(0, 'network_error');
  }
  const isJson = (response.headers.get('content-type') || '').includes('json');
  let body;
  try {
    body = response.status === 204 ? null : isJson ? await response.json() : await response.text();
  } catch (error) {
    if (signal?.aborted) throw error;
    if (deadline.aborted) throw new ApiError(0, 'request_timeout', '读取超时，请稍后重试；已显示的数据仍然保留。');
    throw new ApiError(response.status, 'invalid_response', '服务返回的数据无法读取，请稍后重试。');
  }
  if (!response.ok) {
    const detail = body && typeof body === 'object' ? body.detail : null;
    const code = typeof detail === 'string' && /^[a-z][a-z0-9_]*$/.test(detail)
      ? detail : `http_${response.status}`;
    throw new ApiError(response.status, code);
  }
  return body;
}

/** Decisions are written only with a user-supplied token held by the UI in memory. */
export async function write(path, body, { method = 'POST', token, signal } = {}) {
  if (!['POST', 'PATCH'].includes(method)) throw new ApiError(0, 'invalid_method', '不支持的写入操作。');
  if (!token || typeof token !== 'string') throw new ApiError(401, 'token_required', '请先连接已配置的 API 写入令牌。');
  const url=apiUrl(path);
  let response;
  try {
    response=await fetch(`${url.pathname}${url.search}`,{method,credentials:'same-origin',cache:'no-store',signal,
      headers:{Accept:'application/json','Content-Type':'application/json',Authorization:`Bearer ${token}`},body:JSON.stringify(body)});
  } catch(error) { if(error?.name==='AbortError')throw error; throw new ApiError(0,'network_error'); }
  const data=await response.json().catch(()=>null);
  if(!response.ok) {
    const detail=data?.detail,code=typeof detail==='string'?detail:detail?.code||`http_${response.status}`;
    const message=typeof detail?.message==='string'?detail.message.slice(0,600):code==='write_api_disabled'?'当前服务尚未启用匹配写入，请在服务配置中设置 API Token。':undefined;
    throw new ApiError(response.status,code,message);
  }
  return data;
}

/** List endpoints use string database IDs and ascending after_id cursors. */
export async function list(path, params = {}, { signal } = {}) {
  const url = apiUrl(path);
  for (const [key, value] of Object.entries(params)) {
    if (value === null || value === undefined || value === '') continue;
    if ((key === 'after_id' || key.endsWith('_id')) && typeof value === 'number' && !Number.isSafeInteger(value)) {
      throw new ApiError(0, 'invalid_identifier', '数据标识需要使用完整字符串。');
    }
    url.searchParams.set(key, String(value));
  }
  const result = await request(`${url.pathname.slice(deploymentPrefix.length)}${url.search}`, { signal });
  if (!result || !Array.isArray(result.items) || !Object.hasOwn(result, 'next_after_id') ||
      !(result.next_after_id === null || typeof result.next_after_id === 'string' && /^\d+$/.test(result.next_after_id))) {
    throw new ApiError(200, 'invalid_list_response', '服务返回的列表格式不正确，请稍后重试。');
  }
  return result;
}

/** Bounded collection: complete=false means more rows remain at next_after_id. */
export async function collect(path, params = {}, { signal, maxPages = 50 } = {}) {
  if (!Number.isSafeInteger(maxPages) || maxPages < 1) {
    throw new ApiError(0, 'invalid_page_budget', '分页次数需要为正整数。');
  }
  const items = [];
  const query = { limit: 100, ...params };
  if (typeof query.after_id === 'number' && !Number.isSafeInteger(query.after_id)) {
    throw new ApiError(0, 'invalid_identifier', '数据标识需要使用完整字符串。');
  }
  let next = query.after_id === undefined || query.after_id === null ? '0' : String(query.after_id);
  const visited = new Set([next]);
  for (let page = 0; page < maxPages; page += 1) {
    const result = await list(path, { ...query, after_id: next }, { signal });
    items.push(...result.items);
    next = result.next_after_id;
    if (next === null) return { items, complete: true, next_after_id: null };
    if (visited.has(next)) {
      throw new ApiError(200, 'repeated_cursor', '列表分页未能继续，请刷新后重试。');
    }
    visited.add(next);
  }
  return { items, complete: false, next_after_id: next };
}

// Keys match app/models/enums.py; unknown future keys can be displayed verbatim by callers.
export const labels = Object.freeze({
  taskTypes: Object.freeze({
    shop_discovery: '店铺识别', shop_profile: '店铺档案', product_discovery: '商品发现', product_detail: '商品详情',
    product_reviews: '商品评论', product_snapshot: '商品快照', change_detection: '变化检测', report_generation: '报告生成',
  }),
  taskStatuses: Object.freeze({
    pending: '待执行', running: '执行中', retry_waiting: '等待重试', succeeded: '已成功',
    abandoned: '租约已失效', dead: '已失败', cancelled: '已取消',
  }),
  eventTypes: Object.freeze({
    product_new: '新发现商品', price_changed: '价格变化', discount_started: '折扣开始',
    discount_ended: '折扣结束', stockout: '缺货', restocked: '恢复库存',
    variant_changed: '规格变化', content_changed: '内容变化', product_removed: '商品下架',
  }),
  stockStatuses: Object.freeze({
    unknown: '库存未知', in_stock: '有货', out_of_stock: '缺货', partial: '部分有货',
  }),
  availabilityStatuses: Object.freeze({
    unknown: '状态未知', available: '可购买', unavailable: '暂不可购买', removed: '已下架',
  }),
});

/** Database datetimes without an offset are UTC, rendered explicitly in Shanghai. */
export function formatDate(value, { dateOnly = false } = {}) {
  if (value === null || value === undefined || value === '') return '—';
  let normalized = value;
  if (typeof value === 'string') {
    normalized = value.trim();
    if (/^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(normalized) && !/(?:Z|[+-]\d{2}:?\d{2})$/i.test(normalized)) {
      normalized = `${normalized.replace(' ', 'T')}Z`;
    }
  }
  const date = normalized instanceof Date ? normalized : new Date(normalized);
  if (!Number.isFinite(date.getTime())) return '—';
  const options = {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
    ...(dateOnly ? {} : { hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' }),
  };
  return new Intl.DateTimeFormat('zh-CN', options).format(date);
}

export function money(value, currency = 'USD') {
  if (value === null || value === undefined || typeof value === 'string' && !value.trim()) return '—';
  if (typeof value !== 'number' && typeof value !== 'string') return '—';
  const amount = Number(value);
  if (!Number.isFinite(amount)) return '—';
  try {
    return new Intl.NumberFormat('zh-CN', { style: 'currency', currency, minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(amount);
  } catch {
    return `${amount.toFixed(2)} ${String(currency || '').replace(/[^A-Za-z]/g, '')}`.trim();
  }
}

export function safeUrl(value) {
  if (typeof value !== 'string' || !value.trim()) return null;
  try {
    const url = new URL(value.trim());
    return url.protocol === 'http:' || url.protocol === 'https:' ? url.href : null;
  } catch {
    return null;
  }
}

function csvCell(value) {
  let text = value === null || value === undefined ? '' : typeof value === 'object' ? JSON.stringify(value) : String(value);
  // Spreadsheet apps may ignore leading whitespace before evaluating a formula.
  if (/^[\s\uFEFF]*[=+\-@]/.test(text) || /^[\t\r\n]/.test(text)) text = `'${text}`;
  return `"${text.replace(/"/g, '""')}"`;
}

export function csvDownload(filename, columns, rows) {
  const lines = [columns.map(column => csvCell(column.label)).join(',')];
  rows.forEach((row, index) => lines.push(columns.map(column => csvCell(
    typeof column.key === 'function' ? column.key(row, index) : row[column.key],
  )).join(',')));
  const blob = new Blob([`\uFEFF${lines.join('\r\n')}\r\n`], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = String(filename || 'export.csv').replace(/[\\/:*?"<>|]/g, '-');
  anchor.hidden = true;
  document.body.append(anchor);
  try {
    anchor.click();
  } finally {
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
}
