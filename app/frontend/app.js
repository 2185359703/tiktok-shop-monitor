import { request, list, collect, write as writeRequest, labels, formatDate, money, safeUrl, csvDownload, deploymentPath } from './api.js';
import { createQueryCache, abortableDelay, readSnapshotCache } from './query-cache.js';
import { renderEventTrend, renderStockDonut, renderShopRanking, renderPriceDistribution, renderSampleMetrics } from './charts.js';
import { renderProductWorkspace, sortProducts, defaultColumns } from './product-table.js';
import { buildComparison } from './comparison.js';
import { renderWorkbench, renderDrillProducts, productPhoto } from './workbench.js';
import { renderMatching, renderMatchForm, renderMatchingAuth, renderReviewMatch } from './matching-ui.js';
import { renderProductInspector, renderProductPanel, renderCatalogState, skuCount } from './product-detail.js';
import { installScatterInteractions } from './scatter-chart.js';
import { installShopPicker } from './shop-picker.js';

const paths = {
  bag:'M5 7h14l-1 13H6L5 7ZM9 7V5a3 3 0 0 1 6 0v2',
  grid:'M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z',
  store:'M4 10v10h16V10M3 10l2-7h14l2 7M3 10a3 3 0 0 0 6 0 3 3 0 0 0 6 0 3 3 0 0 0 6 0M9 20v-6h6v6',
  shirt:'m8 3-5 3-2 5 4 2 1-3v11h12V10l1 3 4-2-2-5-5-3a4 4 0 0 1-8 0Z',
  activity:'M2 12h5l3-8 4 16 3-8h5',
  layers:'m12 3 10 5-10 5L2 8l10-5ZM2 12l10 5 10-5M2 16l10 5 10-5',
  file:'M14 2H5v20h14V7l-5-5ZM14 2v5h5M8 12h8M8 16h6',
  search:'M21 21l-5-5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0',
  arrow:'M4 12h15m-5-5 5 5-5 5', chevrons:'m9 9 3-3 3 3m-6 6 3 3 3-3',
  chevron:'m9 5 7 7-7 7', left:'m15 5-7 7 7 7',
  refresh:'M20 7v5h-5M4 17v-5h5M5 7a8 8 0 0 1 14-2l1 2M4 17l1 2a8 8 0 0 0 14-2',
  settings:'M9 3h6l1 3 3 1 2 5-2 5-3 1-1 3H9l-1-3-3-1-2-5 2-5 3-1 1-3ZM15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0',
  radar:'M20 7a9 9 0 1 1-8-4M17 9a6 6 0 1 1-5-3M12 12l9-9M13 12a1 1 0 1 1-2 0 1 1 0 0 1 2 0',
  pulse:'M4 8V4h4M16 4h4v4M20 16v4h-4M8 20H4v-4M6 12h3l2-4 3 8 2-4h2',
  menu:'M4 6h16M4 12h16M4 18h16', close:'m6 6 12 12M6 18 18 6',
  download:'M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5', external:'M14 3h7v7M21 3l-9 9M10 3H3v18h18v-7',
  clock:'M12 8v5l3 2M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0',
  info:'M12 11v6M12 7h.01M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0',
  chart:'M3 3v18h18M7 17v-5M12 17V8M17 17V5',
  check:'m5 12 4 4L19 6', shield:'m12 2 9 4v6c0 5-5 8-9 10-4-2-9-5-9-10V6l9-4Zm-4 10 3 3 5-6',
  box:'m12 3 9 5v9l-9 5-9-5V8l9-5ZM3 8l9 5 9-5M12 13v9M7 5l9 5',
  coin:'M12 5v14M16 8h-6a3 3 0 0 0 0 6h4a3 3 0 0 0 0-6M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0',
  trend:'m3 17 6-6 4 4 8-10M15 5h6v6', alert:'m12 3 10 18H2L12 3ZM12 9v5M12 17h.01',
  table:'M3 4h18v16H3V4ZM3 9h18M8 4v16',columns:'M3 4h18v16H3V4ZM9 4v16M15 4v16',
  compare:'M5 3v18M19 3v18M2 7h9m-3-3 3 3-3 3M22 17h-9m3-3-3 3 3 3',bookmark:'M5 3h14v18l-7-4-7 4V3Z',
};
const icon = name => `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="${paths[name] || paths.box}"/></svg>`;
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const number = value => value == null ? '—' : new Intl.NumberFormat('zh-CN').format(value);
const pct = value => value == null ? '—' : `${Number(value).toFixed(1)}%`;
const present = value => value == null || value === '' ? '—' : typeof value === 'object' ? JSON.stringify(value) : String(value);
const $ = id => document.getElementById(id);
const meta = {
  workbench:['数据工作台','用真实商品观察回答价格、规格、库存与竞品问题。','ANALYTICS WORKBENCH'],
  matches:['竞品匹配','核对同款和精确 SKU，对关系进行持久确认与追踪。','PRODUCT & SKU RELATIONSHIPS'],
  overview:['市场总览','价格、库存与商品变化，集中查看。','MONITORING OVERVIEW'],
  shops:['店铺监测','店铺档案、经营指标与商品样本。','SHOP MONITORING'],
  products:['商品监测','筛选商品、核对公开报价，追踪每一次实际观察。','PRODUCT MONITORING'],
  events:['变化事件','定位价格、库存和规格变化，核对前后观察。','CHANGE EVENTS'],
  tasks:['采集任务','执行状态、重试与采集进度。','COLLECTION TASKS'],
  reports:['监测报告','查看窗口内统计，下载与归档你的市场观察。','MONITORING REPORTS'],
};
const state = {
  initializing:true, uiV2:false, reportLoading:false, eventsData:null, eventStats:null, eventCursors:[null], reportArchive:null,
  page:'overview', hours:24, report:null, overview:null, shops:[], shopsComplete:true, capabilities:null,
  loading:true, errors:{}, query:'', category:'', stock:'', shop:'', eventType:'', shopFilter:'all',
  pageNumber:1, pageSize:20, taskStatus:'', taskType:'', tasks:[], taskNext:null, taskCursors:['0'],
  selectedProducts:new Set(),productColumns:[...defaultColumns],productView:'table',sortKey:'observed_at',sortDirection:'desc',savedViews:[],columnsOpen:false,overviewTab:'overview',stockLabels:labels.stockStatuses,
  productData:null,productsLoading:false,productError:null,productCache:new Map(),
  tasksLoading:false, taskError:null, taskStats:null, reportJobs:[], reportJobsNext:null, reportJobsCursors:['0'], jobsLoading:false, jobsError:null,
};
let refreshController, tasksController, jobsController, detailsController, detailFocus, toastTimer;
const wbState={data:null,dataKey:'',loading:true,error:null,filters:{shop_id:'',category:'',q:'',currency:'USD'},tab:'market',hiddenPanels:new Set(),categoryOptions:[],matchesSummary:null,matchItems:[]};
const workbenchCache=new Map();
const initialWorkbenchShop=new URLSearchParams(location.search).get('shop_id');
if(initialWorkbenchShop&&/^[1-9]\d{0,19}$/.test(initialWorkbenchShop))wbState.filters.shop_id=initialWorkbenchShop;
const matchingState={summary:null,items:[],candidates:null,tab:'candidates',loading:true,error:null,cursors:['0'],next:null,tokenConnected:false};
let wbController,matchingController,productsController,wbQueryTimer,matchToken='',matchingForm=null,drillState=null;
let catalogView=null,productDetail=null,drillRequestVersion=0;
const pageQueries=createQueryCache(request);
async function write(path,body,options){const data=await writeRequest(path,body,options);pageQueries.invalidate();workbenchCache.clear();return data;}
let eventsController,productStatsController,productQueryTimer;
async function pageRead(path,{signal,onData,force=false,maxAge=15000}={}){
  const cached=pageQueries.peek(path);
  if(cached&&onData&&!signal?.aborted)onData(cached);
  const started=Date.now();let attempt=0,lastData=null;
  while(true){
    const data=await pageQueries.load(path,{signal,force:force||attempt>0,maxAge,timeout:8000});
    if(signal?.aborted)throw signal.reason;
    if(data?._meta?.status!=='pending'){lastData=data;if(onData)onData(data);}
    const waiting=data?._meta?.status==='pending'||(data?._meta?.status==='stale'&&data._meta.refresh_state==='queued');
    if(!waiting)return data;
    if(Date.now()-started>60000||document.hidden){if(lastData)return lastData;throw new Error('统计仍在排队，请稍后重试。');}
    await abortableDelay(Math.min(10000,2000*2**attempt++),signal);
  }
}
function snapshotConnectionLabel(data,readyLabel='数据已连接'){
  if(data?._meta?.status!=='stale')return readyLabel;
  return `${data._meta.expired?'上次结果已过期':'上次结果'} · ${data._meta.refresh_state==='failed'?'等待重试':'后台更新中'}`;
}
function updateConnection(){
  const data={workbench:wbState.data,overview:state.report,shops:state.report,products:state.productStats,
    matches:matchingState.tab==='candidates'?matchingState.candidates:null,events:state.eventStats,
    tasks:state.taskStats,reports:state.reportArchive}[state.page];
  $('connection-label').textContent=data?snapshotConnectionLabel(data):state.capabilities?'服务已连接':'正在连接服务';
  $('connection-dot').className=`status-dot ${state.capabilities||data?'online':'offline'}`;
  $('system-dot').className=`status-dot ${state.capabilities?'online':'offline'}`;
  const generated=data?.generated_at||data?._meta?.generated_at;
  $('last-updated').textContent=generated?`${state.page==='reports'?'报告时间':'更新于'} ${formatDate(generated)}`:'';
  $('shop-count').textContent=number(state.overview?.shops_with_url??state.shops.length);
}
async function loadReportSummary({signal,force=false}={}){
  state.reportLoading=true;
  try{await pageRead(state.uiV2?`/api/v2/analytics/overview?lookback_hours=${state.hours}`:`reports/current?lookback_hours=${state.hours}`,
    {signal,force,onData:data=>{state.report=data;state.reportWindow=state.hours;state.reportLoading=false;state.errors.report=null;
      if(state.uiV2)state.overview={...state.overview,products:data.catalog_product_count,shops_with_url:data.summary.shop_entries};updateConnection();render();}});
  }catch(error){if(!signal?.aborted)state.errors.report=error;}
  finally{if(!signal?.aborted){state.reportLoading=false;render();}}
}
let shopDetail=null;
try{const hidden=JSON.parse(localStorage.getItem('shopscope-workbench-panels-v1')||'[]');if(Array.isArray(hidden))wbState.hiddenPanels=new Set(hidden.filter(v=>typeof v==='string'));}catch{}
const viewStorageKey='shopscope-product-views-v1',preferencesKey='shopscope-table-preferences-v1';
const validColumns=['shop','category','price','sku','stock','observed'];
try {
  const views=JSON.parse(localStorage.getItem(viewStorageKey)||'[]');
  if(Array.isArray(views))state.savedViews=views.filter(v=>v&&typeof v.name==='string'&&v.filters&&typeof v.filters==='object').slice(0,12);
  const preferences=JSON.parse(localStorage.getItem(preferencesKey)||'null');
  if(preferences?.view==='grid')state.productView='grid';
  if(Array.isArray(preferences?.columns))state.productColumns=validColumns.filter(key=>preferences.columns.includes(key));
  if([10,20,50,100].includes(preferences?.pageSize))state.pageSize=preferences.pageSize;
}catch{/* Default preferences also work when browser storage is unavailable. */}
function persistPreferences(){try{localStorage.setItem(preferencesKey,JSON.stringify({view:state.productView,columns:state.productColumns,pageSize:state.pageSize}));}catch{}}

function stockBadge(value) {
  const tone = {in_stock:'green',out_of_stock:'amber',partial:'blue'}[value] || '';
  return `<span class="badge ${tone}">${esc(labels.stockStatuses[value] || '库存未知')}</span>`;
}
function taskBadge(value) {
  const tone = {succeeded:'green',running:'blue',retry_waiting:'amber',dead:'red',abandoned:'red'}[value] || '';
  return `<span class="badge ${tone}">${esc(labels.taskStatuses[value] || value)}</span>`;
}
function price(row) {
  const low = row.price_min, high = row.price_max, currency = row.currency || state.report?.currency || 'USD';
  if (low == null) return '—';
  if (high == null) return `${money(low,currency)} <span class="cell-secondary">起价</span>`;
  return Number(low) === Number(high) ? money(low,currency) : `${money(low,currency)}–${money(high,currency)}`;
}
function external(url, text, className='text-link') {
  const link = safeUrl(url);
  return link ? `<a href="${esc(link)}" class="${className}" target="_blank" rel="noopener noreferrer">${esc(text)} ${icon('external')}</a>` : '<span class="muted">暂无链接</span>';
}
function notice(text, warning=false) { return `<div class="notice${warning?' warning':''}">${icon(warning?'alert':'info')}<span>${esc(text)}</span></div>`; }
function empty(title='暂无数据', message='新的观察将在采集完成后显示在这里。', compact=false) {
  return `<div class="empty-state ${compact?'compact':''}">${icon('box')}<h2>${esc(title)}</h2><p>${esc(message)}</p></div>`;
}
function loading(compact=false) { return `<div class="loading-state ${compact?'compact':''}"><span class="loader"></span><h2>正在读取数据</h2><p>请稍候，正在连接监测服务…</p></div>`; }
function errorPanel(error) { return `<div class="panel error-state">${icon('alert')}<h2>暂时无法读取数据</h2><p>${esc(error?.message || '请稍后刷新重试。')}</p><button class="button" data-action="refresh">${icon('refresh')}重新连接</button></div>`; }
function options(mapping, selected, all) { return `<option value="">${esc(all)}</option>`+Object.entries(mapping).map(([key,value])=>`<option value="${esc(key)}" ${selected===key?'selected':''}>${esc(value)}</option>`).join(''); }
function kpi(label, value, caption, symbol, featured=false) {
  return `<div class="kpi ${featured?'featured':''}"><div class="kpi-label">${esc(label)}${icon(symbol)}</div><div class="kpi-value">${value}</div><div class="kpi-caption">${caption}</div></div>`;
}
function renderKpis() {
  const summary = state.report?.summary;
  return `<div class="kpi-grid">${kpi('监测店铺',number(state.overview?.shops_with_url ?? summary?.shop_entries),`<strong>${number(summary?.sampled_shops)}</strong> 家已有商品采样`,'store')}${kpi('已采集商品',number(state.overview?.products ?? summary?.products),`<strong>${number(summary?.historical_detail_products)}</strong> 件已保存详情快照`,'shirt')}${kpi('商品起价中位数',money(summary?.starting_price_stats?.median,state.report?.currency),'基于已知公开起价 · 非成交价','coin')}${kpi('窗口内变化事件',number(summary?.events_in_window),`最近 ${state.hours===24?'24 小时':state.hours===168?'7 天':'30 天'} · 可追溯快照`,'activity')}</div>`;
}
function eventValue(value, type) {
  if (value == null) return '—';
  if (typeof value !== 'object') return esc(value);
  if (type==='price_changed') return Object.hasOwn(value,'price') ? esc(money(value.price,value.currency||state.report?.currency)) : price(value);
  if (Object.hasOwn(value,'stock_status')) return esc(labels.stockStatuses[value.stock_status]||present(value.stock_status));
  if (Object.hasOwn(value,'discount_rate')) return value.discount_rate!=null&&Number.isFinite(Number(value.discount_rate)) ? esc(`优惠 ${pct(Number(value.discount_rate)*100)}`) : '—';
  return esc(Object.entries(value).map(([k,v])=>`${k}: ${present(v)}`).join(' · '));
}
function eventRows(rows) {
  const byId=new Map((state.report?.products||[]).map(p=>[p.id,p]));
  return rows.map(e=>{const p=byId.get(e.product_id);return `<tr><td><div class="event-kind"><span class="event-symbol ${['stockout','product_removed'].includes(e.event_type)?'amber':''}">${icon(e.event_type==='price_changed'?'coin':e.event_type==='product_new'?'shirt':'activity')}</span><span>${esc(labels.eventTypes[e.event_type]||e.event_type)}</span></div></td><td><button class="product-title" data-action="product" data-id="${esc(e.product_id)}" title="${esc(e.title||p?.title)}">${esc(e.title||p?.title||'商品 '+e.product_id)}</button><span class="cell-secondary">${esc(e.shop_name||p?.shop_name||'—')}</span></td><td><span class="event-value old">${eventValue(e.old_value,e.event_type)}</span><span class="event-arrow">→</span><span class="event-value">${eventValue(e.new_value,e.event_type)}</span></td><td class="event-time">${esc(formatDate(e.observed_at||e.detected_at))}</td><td><button class="row-link" data-action="event" data-id="${esc(e.id)}">查看依据 ${icon('arrow')}</button></td></tr>`;}).join('');
}
function renderOverview() {
  if(state.reportLoading&&!state.report&&!state.overview)return loading();
  if (!state.report && !state.overview) return errorPanel(state.errors.report||state.errors.overview);
  const recent=state.report?.events?.slice(0,5)||[];
  const tabs=`<div class="section-tabbar"><div class="analysis-tabs" role="group" aria-label="分析主题">${[['overview','监测概览'],['prices','价格分析'],['quality','监测质量']].map(([key,label])=>`<button data-action="overview-tab" data-value="${key}" class="${state.overviewTab===key?'active':''}" aria-pressed="${state.overviewTab===key}">${label}</button>`).join('')}</div><a class="text-link" href="#reports">${icon('file')} 查看完整报告</a></div>`;
  if(!state.report)return `${renderKpis()}${notice('报告暂不可用，样本价格与图表等待数据恢复。',true)}`;
  if(state.overviewTab==='prices') {
    const categories=state.report.categories||[];
    return `${tabs}${renderKpis()}<div class="analysis-grid price-analysis">${renderPriceDistribution(state.report)}<section class="panel ranking-panel"><div class="panel-header"><div><h2 class="panel-title">类目公开起价</h2><p class="panel-subtitle">按页面公开类目分组，点击查看商品样本</p></div></div><div class="table-wrap"><table><thead><tr><th>公开类目</th><th class="align-right">样本</th><th class="align-right">起价中位数</th></tr></thead><tbody>${categories.map(c=>`<tr><td><button class="row-link category-cell" data-action="category-products" data-value="${esc(c.category)}" title="${esc(c.category)}">${esc(c.category==='unknown'?'类目未知':c.category)}</button></td><td class="align-right num">${number(c.products)}</td><td class="align-right cell-price">${money(c.starting_price_stats?.median,state.report.currency)}</td></tr>`).join('')}</tbody></table></div></section></div>${notice('同一商品的不同颜色与尺码可能价格不同。图中统一使用公开报价下限，不自动判断跨商品规格是否相同。')}`;
  }
  if(state.overviewTab==='quality') {
    const counts=tasksCounts();
    return `${tabs}${renderSampleMetrics(state.report)}<div class="quality-grid">${renderStockDonut(state.report)}<section class="panel"><div class="panel-header"><div><h2 class="panel-title">任务执行状态</h2><p class="panel-subtitle">当前数据库中的任务数量</p></div><a class="text-link" href="#tasks">查看任务 ${icon('arrow')}</a></div><div class="report-summary">${Object.entries(labels.taskStatuses).map(([key,label])=>`<div class="summary-row"><span>${label}</span><strong>${state.overview?.tasks?number(counts[key]||0):'—'}</strong></div>`).join('')}</div></section></div>${notice('未知库存不当作缺货；详情快照覆盖率只衡量已采集商品中的历史详情覆盖。')}`;
  }
  return `${tabs}${renderKpis()}${renderEventTrend(state.report,state.hours)}<div class="analysis-grid">${renderPriceDistribution(state.report)}${renderStockDonut(state.report)}${renderShopRanking(state.report)}</div>${renderSampleMetrics(state.report)}<section class="panel table-panel"><div class="panel-header"><div><h2 class="panel-title">近期变化明细</h2><p class="panel-subtitle">从变化定位商品，再核对历史与规格</p></div><a class="text-link" href="#events">查看全部 ${icon('arrow')}</a></div>${recent.length?`<div class="table-wrap"><table><thead><tr><th>变化类型</th><th>关联商品</th><th>变化内容</th><th>观察时间</th><th></th></tr></thead><tbody>${eventRows(recent)}</tbody></table></div>`:empty('当前窗口暂无变化','新的变化会在后续快照比较完成后显示。',true)}</section>`;
}
function searchControl(placeholder) { return `<label class="table-search">${icon('search')}<input id="table-query" data-filter="query" value="${esc(state.query)}" placeholder="${esc(placeholder)}" aria-label="${esc(placeholder)}" autocomplete="off"></label>`; }
function paginate(rows) {
  const pages=Math.max(1,Math.ceil(rows.length/state.pageSize));state.pageNumber=Math.min(pages,state.pageNumber);
  return {rows:rows.slice((state.pageNumber-1)*state.pageSize,state.pageNumber*state.pageSize),pages,total:rows.length};
}
function pageFooter(data,noun) {
  const known=data.total!=null;
  data={...data,pages:Math.max(state.pageNumber,data.pages||state.pageNumber+(data.hasMore?1:0))};
  const first=data.rows?.length?(state.pageNumber-1)*state.pageSize+1:0,last=first?first+data.rows.length-1:0;
  const pages=[1,state.pageNumber-1,state.pageNumber,state.pageNumber+1,data.pages].filter((p,i,all)=>p>=1&&p<=data.pages&&all.indexOf(p)===i).sort((a,b)=>a-b);
  let previous=0;
  const buttons=pages.map(p=>{const gap=previous&&p-previous>1?'<span class="pagination-gap">…</span>':'';previous=p;return `${gap}<button class="page-number-button ${p===state.pageNumber?'active':''}" data-action="page-number" data-value="${p}" ${p===state.pageNumber?'aria-current="page"':''} aria-label="第 ${p} 页">${p}</button>`;}).join('');
  return `<div class="table-footer"><div class="page-size-control"><span>${first}–${last} / ${known?number(data.total):'正在统计'} ${noun}${state.query?'，已筛选':''}</span><label>每页 <select data-page-size aria-label="每页记录数">${[10,20,50,100].map(n=>`<option value="${n}" ${state.pageSize===n?'selected':''}>${n}</option>`).join('')}</select> 条</label></div><div class="pagination"><button class="button" data-action="prev" ${state.pageNumber===1?'disabled':''} aria-label="上一页">${icon('left')}</button>${buttons}<button class="button" data-action="next" ${(data.hasMore!==undefined?!data.hasMore:state.pageNumber===data.pages)?'disabled':''} aria-label="下一页">${icon('chevron')}</button></div></div>`;
}
function allShops() {
  const map=new Map((state.report?.shops||[]).map(s=>[s.id,s]));
  return state.shops.map(s=>({...map.get(s.id),...s}));
}
function exactCount(value){if(value==null)return '未获取';try{return new Intl.NumberFormat('zh-CN').format(BigInt(String(value)));}catch{return '未获取';}}
function profileFieldState(row,key){return (row.profile_field_state||row.field_state||{})[key]?.state||(row[key]!=null?'exact':row[`${key}_text`]?'display_only':'not_provided');}
function profileMetric(row,key){return typeof row[`${key}_text`]==='string'&&row[`${key}_text`].trim()?row[`${key}_text`]:exactCount(row[key]);}
function profileMetricCell(row,key){const fieldState=profileFieldState(row,key);return `<span class="cell-primary num">${esc(profileMetric(row,key))}</span>${fieldState==='display_only'?'<span class="cell-secondary">仅展示值</span>':fieldState==='invalid'?'<span class="cell-secondary">来源异常</span>':''}`;}
function shopAvatar(row,large=false){const url=safeUrl(row.logo_url);return `<span class="shop-avatar${large?' shop-profile-avatar':''}">${url?`<img src="${esc(url)}" alt="${esc(row.name||'店铺')} Logo" loading="lazy" referrerpolicy="no-referrer">`:''}<span class="shop-logo-fallback" ${url?'hidden':''}>${esc((row.name||'S').slice(0,2).toUpperCase())}</span></span>`;}
function profileBadges(row){return `${row.verification_type==='blue_v'?`<span class="shop-blue-v" title="来源显示蓝 V" aria-label="来源显示蓝 V">${icon('check')}</span>`:''}${row.identity_label_key==='official'?`<span class="shop-official-label">${esc(row.identity_label_text||'官方店铺')}</span>`:row.identity_label_text?`<span class="badge">${esc(row.identity_label_text)}</span>`:''}`;}
function profileUpdated(row){return row.profile_captured_at?`${esc(formatDate(row.profile_captured_at))}${row.profile_stale?'<span class="cell-secondary shop-stale">上次采集 · 待更新</span>':row.profile_observation_status==='partial'?'<span class="cell-secondary">部分字段缺失</span>':''}`:'<span class="muted">尚未采集档案</span>';}
function filteredShops() {
  return allShops().filter(s=>(!state.report||state.shopFilter==='all'||(state.shopFilter==='sampled'?s.sampled_products>0:!s.sampled_products))&&`${s.name} ${s.platform_shop_id}`.toLowerCase().includes(state.query.toLowerCase()));
}
function renderShops() {
  if(state.errors.shops) return errorPanel(state.errors.shops);
  const data=paginate(filteredShops()), sampled=state.report?allShops().filter(s=>s.sampled_products>0).length:null;
  return `<div class="mini-stats"><div class="mini-stat"><div>${icon('store')}可访问店铺入口</div><strong>${number(state.overview?.shops_with_url??state.shops.length)}</strong></div><div class="mini-stat"><div>${icon('shirt')}已有商品采样</div><strong>${number(sampled)}</strong></div><div class="mini-stat"><div>${icon('clock')}等待商品采样</div><strong>${number(sampled==null?null:state.shops.length-sampled)}</strong></div></div>${state.errors.report?notice('样本汇总暂不可用，采样数量和价格统计等待报告恢复。',true):notice('粉丝、累计已售、视频来自店铺档案；商品样本数仅代表已采集商品。')}${!state.shopsComplete?notice('目前显示前 5,000 个店铺入口，搜索与导出仅覆盖这些入口。',true):''}<section class="panel"><div class="toolbar"><div class="toolbar-filters">${searchControl('搜索店铺名称或 ID')}<div class="filter-tabs" aria-label="采样筛选">${[['all','全部店铺'],['sampled','已有采样'],['waiting','等待采样']].map(([key,text])=>`<button class="filter-tab ${(state.report?state.shopFilter:'all')===key?'selected':''}" data-action="shop-filter" data-value="${key}" aria-pressed="${(state.report?state.shopFilter:'all')===key}" ${key!=='all'&&!state.report?'disabled':''}>${text}</button>`).join('')}</div></div><button class="button" data-action="export">${icon('download')}导出店铺</button></div>${data.rows.length?`<div class="table-wrap"><table class="shop-profile-table"><thead><tr><th>店铺</th><th>粉丝</th><th>累计已售</th><th>视频</th><th>档案更新时间</th><th>来源分组</th><th>商品样本</th><th>起价中位数</th><th>历史详情</th><th>最近观察</th><th></th></tr></thead><tbody>${data.rows.map(s=>`<tr><td><div class="table-main">${shopAvatar(s)}<div><div class="shop-name-line"><button class="product-title" data-action="shop" data-id="${esc(s.id)}">${esc(s.name)}</button>${profileBadges(s)}</div><div class="product-sub">${esc(s.platform_shop_id)}</div></div></div></td><td>${profileMetricCell(s,'follower_count')}</td><td>${profileMetricCell(s,'sales_count')}</td><td>${profileMetricCell(s,'video_count')}</td><td class="event-time">${profileUpdated(s)}</td><td>${esc(s.source_group||s.discovery_category||'未知')}</td><td><span class="cell-primary num">${state.report?number(s.sampled_products||0):'—'}</span></td><td class="cell-price">${money(s.starting_price_stats?.median,state.report?.currency)}</td><td class="num">${state.report?number(s.historical_detail_products||0):'—'}</td><td class="event-time">${esc(formatDate(s.last_seen_at))}</td><td>${external(s.shop_url,'访问店铺','row-link')}</td></tr>`).join('')}</tbody></table></div>`:empty('没有匹配的店铺','调整搜索内容或采样筛选后重试。')}${pageFooter(data,'家店铺')}</section>`;
}
function filteredProducts() {
  if(state.productData)return state.productData.items;
  return sortProducts((state.report?.products||[]).filter(p=>`${p.title} ${p.shop_name} ${p.platform_product_id}`.toLowerCase().includes(state.query.toLowerCase())&&(!state.category||p.category===state.category)&&(!state.stock||p.stock_status===state.stock)&&(!state.shop||p.shop_id===state.shop)),state.sortKey,state.sortDirection);
}
function renderProducts() {
  if(state.productError&&!state.productData)return errorPanel(state.productError);
  if(state.productsLoading&&!state.productData)return loading();
  return `${state.productError?notice(state.productError.message,true):''}${renderProductWorkspace(state,filteredProducts(),{esc,icon,number,money,price,stockBadge,formatDate,options,empty,loading,paginate,pageFooter})}`;
}
function filteredEvents() {
  if(state.uiV2)return state.eventsData?.items||[];
  const products=new Map((state.report?.products||[]).map(p=>[p.id,p]));
  return (state.report?.events||[]).filter(e=>(!state.eventType||e.event_type===state.eventType)&&`${e.title||products.get(e.product_id)?.title||''} ${e.shop_name||''} ${e.platform_product_id||''}`.toLowerCase().includes(state.query.toLowerCase()));
}
function renderEvents() {
  if(state.eventsLoading&&!state.eventsData)return loading();
  if(state.errors.events&&!state.eventsData)return errorPanel(state.errors.events);
  if(!state.uiV2&&state.errors.report)return errorPanel(state.errors.report);
  const report=state.uiV2?{summary:{events_in_window:state.eventStats?.events_in_window},events:state.eventsData?.items||[],events_truncated:false}:state.report;
  const data=state.uiV2?{rows:filteredEvents(),total:state.eventStats?.events_in_window,pages:state.eventCursors.filter(Boolean).length+1,hasMore:Boolean(state.eventsData?.next_cursor)}:paginate(filteredEvents()),events=report?.events||[];
  return `<div class="mini-stats"><div class="mini-stat"><div>${icon('activity')}窗口内全部事件</div><strong>${number(report?.summary?.events_in_window)}</strong></div><div class="mini-stat"><div>${icon('coin')}已载入价格变化</div><strong>${number(events.filter(e=>e.event_type==='price_changed').length)}</strong></div><div class="mini-stat"><div>${icon('box')}已载入库存变化</div><strong>${number(events.filter(e=>['stockout','restocked'].includes(e.event_type)).length)}</strong></div></div>${report?.events_truncated?notice('窗口内事件超过 1,000 条，目前展示最近 1,000 条；分类计数、筛选和导出仅覆盖已载入事件。',true):''}<section class="panel"><div class="toolbar"><div class="toolbar-filters">${searchControl('搜索商品或店铺')}<select data-filter="eventType" aria-label="筛选变化类型">${options(labels.eventTypes,state.eventType,'全部变化类型')}</select></div><button class="button" data-action="export">${icon('download')}导出事件</button></div>${data.rows.length?`<div class="table-wrap"><table><thead><tr><th>变化类型</th><th>关联商品</th><th>变化内容</th><th>观察时间</th><th></th></tr></thead><tbody>${eventRows(data.rows)}</tbody></table></div>`:empty('当前窗口暂无匹配的变化','尝试扩大观察区间，或查看其他变化类型。')}${pageFooter(data,'条已载入事件')}</section><div class="stack-gap">${notice('新发现商品表示首次被系统采集；观察时间来自商品快照，不代表商品上架时间。')}</div>`;
}
function tasksCounts() {
  const counts={};for(const row of state.overview?.tasks||[]) counts[row.status]=(counts[row.status]||0)+row.count;return counts;
}
function renderTasks() {
  const counts=tasksCounts();
  const body=state.tasksLoading?loading():state.taskError?errorPanel(state.taskError):state.tasks.length?`<div class="table-wrap"><table><thead><tr><th>任务标识</th><th>任务类型</th><th>资源</th><th>执行状态</th><th>尝试次数</th><th>可执行时间</th><th></th></tr></thead><tbody>${state.tasks.map(t=>`<tr><td><button class="row-link task-id" data-action="task" data-id="${esc(t.task_id)}" title="${esc(t.task_id)}">${esc(t.task_id.slice(0,8))}… ${icon('arrow')}</button><span class="cell-secondary">#${esc(t.id)}</span></td><td class="cell-primary">${esc(labels.taskTypes[t.task_type]||t.task_type)}</td><td>${esc(t.resource_type)}<span class="cell-secondary">${esc(t.resource_id)}</span></td><td>${taskBadge(t.status)}</td><td class="num">${number(t.attempt_count)} / ${number(t.max_attempts)}</td><td class="event-time">${esc(formatDate(t.available_at))}</td><td><button class="row-link" data-action="task" data-id="${esc(t.task_id)}">执行详情 ${icon('arrow')}</button></td></tr>`).join('')}</tbody></table></div>`:empty('暂无匹配的任务','调整状态或类型筛选后重试。');
  return `<div class="mini-stats"><div class="mini-stat"><div>${icon('clock')}待执行 / 重试</div><strong>${state.overview?.tasks?number((counts.pending||0)+(counts.retry_waiting||0)):'—'}</strong></div><div class="mini-stat"><div>${icon('pulse')}正在执行</div><strong>${state.overview?.tasks?number(counts.running||0):'—'}</strong></div><div class="mini-stat"><div>${icon('check')}已成功</div><strong>${state.overview?.tasks?number(counts.succeeded||0):'—'}</strong></div></div><section class="panel"><div class="toolbar"><div class="toolbar-filters"><select data-filter="taskStatus" aria-label="筛选任务状态">${options(labels.taskStatuses,state.taskStatus,'全部任务状态')}</select><select data-filter="taskType" aria-label="筛选任务类型">${options(labels.taskTypes,state.taskType,'全部任务类型')}</select></div><span class="filter-label">按任务 ID 升序</span></div>${body}<div class="table-footer"><span>当前页 ${state.tasks.length} 条任务</span><div class="pagination"><button class="button" data-action="task-prev" ${state.taskCursors.length===1||state.tasksLoading?'disabled':''}>${icon('left')}上一页</button><span>第 ${state.taskCursors.length} 页</span><button class="button" data-action="task-next" ${!state.taskNext||state.tasksLoading?'disabled':''}>下一页${icon('chevron')}</button></div></div></section><div class="stack-gap">${notice('任务状态由采集运行层更新。点击任务可查看执行尝试、响应状态和采集进度。')}</div>`;
}
function renderReports() {
  const r=state.reportArchive,s=r?.summary;
  const jobBody=state.jobsLoading?loading(true):state.jobsError?`<div class="empty-inline">${esc(state.jobsError.message)}</div>`:state.reportJobs.length?`<div class="table-wrap"><table><thead><tr><th>报告任务</th><th>生成状态</th><th>计划时间</th><th>完成时间</th><th>下载文件</th></tr></thead><tbody>${state.reportJobs.map(j=>`<tr><td><button class="row-link task-id" data-action="task" data-id="${esc(j.task_id)}">${esc(j.task_id.slice(0,8))}…</button></td><td>${taskBadge(j.status)}</td><td class="event-time">${esc(formatDate(j.scheduled_for))}</td><td class="event-time">${esc(formatDate(j.finished_at))}</td><td>${j.status==='succeeded'?['html','json','md'].map(f=>`<button class="row-link" data-action="download-job" data-id="${esc(j.task_id)}" data-format="${f}">${f.toUpperCase()}</button>`).join(' &nbsp; '):'<span class="report-status">生成完成后可下载</span>'}</td></tr>`).join('')}</tbody></table></div>`:empty('暂无归档报告','报告任务成功完成后，文件会出现在这里。',true);
  const current = r ? `<section class="panel report-hero"><div><span class="context-tag">${esc(r.region)} · 服装商品 · ${(r.lookback_hours||24)/24} 天观察窗口</span><h2>服装市场监测报告</h2><p>截至 ${esc(formatDate(r.as_of))}，记录 ${number(s.products)} 件商品与 ${number(s.events_in_window)} 条变化。</p><div class="report-actions"><button class="button primary" data-action="preview-report">${icon('file')}查看完整报告</button><button class="button" data-action="download-report" data-format="html">${icon('download')}HTML</button><button class="button" data-action="download-report" data-format="json">JSON</button><button class="button" data-action="download-report" data-format="md">Markdown</button></div></div><div class="report-illustration">${icon('chart')}</div></section><div class="report-columns"><section class="panel"><div class="panel-header"><h3 class="panel-title">本次报告概览</h3><span class="context-tag">${esc(r.currency)}</span></div><div class="report-summary">${[['店铺入口',number(s.shop_entries)],['已采样店铺',number(s.sampled_shops)],['商品样本',number(s.products)],['已观察 SKU',number(s.observed_skus)],['明确库存状态 SKU',number(s.known_stock_skus)],['明确缺货 SKU',number(s.stockout_skus)],['SKU 缺货率',pct(s.sku_stockout_pct)]].map(([k,v])=>`<div class="summary-row"><span>${k}</span><strong>${v}</strong></div>`).join('')}</div></section><section class="panel"><div class="panel-header"><h3 class="panel-title">读懂这份报告</h3>${icon('info')}</div><ul class="notes-list">${(r.notes||[]).map(n=>`<li>${esc(n)}</li>`).join('')}</ul></section></div>` : state.jobsLoading?loading(true):empty('暂无已完成报告','后台报告完成后，可在此预览与下载。',true);
  return `${current}<section class="panel"><div class="panel-header"><div><h3 class="panel-title">归档报告</h3><p class="panel-subtitle">由后台任务保存的历史报告文件</p></div></div><div class="stack-gap">${jobBody}</div><div class="table-footer"><span>当前页 ${state.reportJobs.length} 份报告任务</span><div class="pagination"><button class="button" data-action="job-prev" ${state.reportJobsCursors.length===1||state.jobsLoading?'disabled':''}>${icon('left')}上一页</button><button class="button" data-action="job-next" ${!state.reportJobsNext||state.jobsLoading?'disabled':''}>下一页${icon('chevron')}</button></div></div></section>`;
}
let renderFrame=null;
function render() {
  if(renderFrame!==null)return;
  renderFrame=requestAnimationFrame(()=>{renderFrame=null;renderNow();});
}
function renderNow() {
  const active=document.activeElement, activeId=active?.id, pos=active?.selectionStart;
  const activeAction=active?.dataset.action,activeValue=active?.dataset.value,activeItem=active?.dataset.id,activeColumn=active?.dataset.column;
  if(state.page==='products')state.columnsOpen=document.querySelector('.column-settings')?.open??state.columnsOpen;
  const [title,desc,eyebrow]=meta[state.page];$('page-title').textContent=title;$('breadcrumb').textContent=title;$('page-description').textContent=desc;$('eyebrow').textContent=eyebrow;
  document.title=`${title} · ShopScope`;
  document.body.dataset.view=state.page;
  if(state.page==='workbench'){if(wbState.loading&&!wbState.data){$('connection-label').textContent='正在读取筛选数据';$('last-updated').textContent='';}else if(wbState.error&&!wbState.data){$('connection-label').textContent='筛选数据暂不可用';$('last-updated').textContent='';}else if(wbState.data&&!wbState.error){$('connection-label').textContent=snapshotConnectionLabel(wbState.data,'分析数据已连接');$('last-updated').textContent=`分析更新于 ${formatDate(wbState.data.generated_at)}`;$('connection-dot').className='status-dot online';}}
  document.querySelectorAll('[data-page]').forEach(a=>{const selected=a.dataset.page===state.page;a.classList.toggle('active',selected);if(selected)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current');});
  $('window-select').hidden=['shops','tasks'].includes(state.page);
  $('page-content').setAttribute('aria-busy',String(state.page==='workbench'?wbState.loading:state.loading));
  const view={workbench:()=>renderWorkbench(wbState,{shops:state.shops,icon}),matches:()=>renderMatching(matchingState,state.capabilities||{}),overview:renderOverview,shops:renderShops,products:renderProducts,events:renderEvents,tasks:renderTasks,reports:renderReports};
  $('page-content').innerHTML=state.initializing?loading():`<div class="page-enter">${view[state.page]()}</div>`;
  if(['table-query','wb-query'].includes(activeId)&&$(activeId)){const next=$(activeId);next.focus({preventScroll:true});if(pos!=null)next.setSelectionRange(pos,pos);}
  else if(activeAction){const selector=`[data-action="${CSS.escape(activeAction)}"]${activeItem?`[data-id="${CSS.escape(activeItem)}"]`:''}${activeValue?`[data-value="${CSS.escape(activeValue)}"]`:''}`;document.querySelector(selector)?.focus({preventScroll:true});}
  else if(activeColumn)document.querySelector(`[data-column="${CSS.escape(activeColumn)}"]`)?.focus({preventScroll:true});
  const selectPage=document.querySelector('[data-action="select-page"]');
  if(selectPage){const displayed=document.querySelectorAll('[data-action="select-product"]'),selected=[...displayed].filter(input=>input.checked).length;selectPage.indeterminate=selected>0&&selected<displayed.length;}
}
async function loadShopDirectory(signal){
  const result=await collect('shops',{with_url:true},{signal});
  if(!signal.aborted){state.shops=result.items;state.shopsComplete=result.complete;if(state.page==='workbench')render();}
  return result;
}
async function refresh({reusePageRequest=false,force=true}={}) {
  refreshController?.abort();refreshController=new AbortController();const {signal}=refreshController;
  if(force)pageQueries.invalidate();
  state.loading=false;$('refresh-button').disabled=true;$('refresh-button').classList.add('spinning');
  const pending=[];
  pending.push(pageRead('capabilities',{signal,maxAge:300000,onData:data=>{state.capabilities=data;updateConnection();render();}}).catch(error=>{if(!signal.aborted)state.errors.capabilities=error;}));
  if(!state.shops.length||force)pending.push(loadShopDirectory(signal).then(()=>{if(!signal.aborted){state.errors.shops=null;updateConnection();render();}}).catch(error=>{if(!signal.aborted)state.errors.shops=error;}));
  if(state.page==='overview'||state.uiV2&&['shops','products'].includes(state.page))pending.push(loadReportSummary({signal,force}));
  if(state.page==='events'){if(state.uiV2)pending.push(loadEvents({reset:true}));else pending.push(loadReportSummary({signal,force}));}
  if(state.page==='tasks'){pending.push(loadTasks());if(state.uiV2)pending.push(pageRead('/api/v2/tasks/summary',{signal,onData:data=>{state.taskStats=data;state.overview={...state.overview,tasks:data.tasks};updateConnection();render();}}).catch(()=>{}));}
  if(state.page==='reports')pending.push(loadJobs());
  if(!reusePageRequest){if(state.page==='workbench')pending.push(loadWorkbench({force}));if(state.page==='matches')pending.push(loadMatching());if(state.page==='products')pending.push(loadProducts());}
  render();await Promise.allSettled(pending);
  if(!signal.aborted){$('refresh-button').disabled=false;$('refresh-button').classList.remove('spinning');updateConnection();}
}
function queryPath(path,parameters){const query=new URLSearchParams();for(const [key,value]of Object.entries(parameters))if(value!==null&&value!==undefined&&value!=='')query.set(key,String(value));return `${path}?${query}`;}
function rememberProducts(rows){for(const p of rows){const row={...p,observed_at:p.observed_at||p.last_seen_at,sku_count:p.sku_count??p.variant_count,shop_name:p.shop_name||'店铺 '+p.shop_id};state.productCache.set(row.id,row);}}
async function loadProducts(){
  productsController?.abort();productsController=new AbortController();const {signal}=productsController;
  state.productsLoading=true;state.productError=null;
  const sort={observed_at:'recent',title:state.sortDirection==='asc'?'title':'title_desc',shop_name:`shop_${state.sortDirection}`,price_min:`price_${state.sortDirection}`,sku_count:`sku_count_${state.sortDirection}`,stock_status:`stock_${state.sortDirection}`}[state.sortKey]||'recent';
  const filters={q:state.query,category:state.category,stock:state.stock,shop_id:state.shop},filterKey=JSON.stringify(filters);
  const path=queryPath(state.uiV2?'/api/v2/catalog/products':'analytics/products',{page:state.pageNumber,page_size:state.pageSize,...filters,sort,include_stats:false});
  const accept=data=>{if(signal.aborted)return;rememberProducts(data.items);data.items=data.items.map(p=>state.productCache.get(p.id));data._loaded=true;state.productData=data;state.productFilterKey=filterKey;state.productsLoading=false;if(state.page==='products')render();};
  if(state.productRequestPath!==path)state.productData={items:[],page:state.pageNumber,page_size:state.pageSize,total:null,total_pages:null,has_more:false};
  state.productRequestPath=path;if(state.page==='products')render();
  try{const data=await pageRead(path,{signal,onData:accept});if(signal.aborted)return;
    if(data.total_pages&&state.pageNumber>data.total_pages){state.pageNumber=data.total_pages;loadProducts();return;}
    if(state.uiV2){productStatsController?.abort();productStatsController=new AbortController();const statsSignal=productStatsController.signal;
      pageRead(queryPath('/api/v2/catalog/stats',filters),{signal:statsSignal,onData:stats=>{if(signal.aborted||statsSignal.aborted||state.productFilterKey!==filterKey)return;
        state.productStats=stats;state.productData={...state.productData,total:stats.total,total_pages:Math.ceil(stats.total/state.pageSize),field_coverage:stats.field_coverage};updateConnection();if(state.page==='products')render();}}).catch(()=>{});}
  }catch(e){if(!signal.aborted){state.productError=e;if(!state.productData?._loaded)state.productData=null;}}
  finally{if(!signal.aborted){state.productsLoading=false;if(state.page==='products')render();}}
}
function workbenchKey(filters=wbState.filters,hours=state.hours,tab=wbState.tab){return JSON.stringify([String(filters.shop_id||''),filters.category||'',(filters.q||'').trim(),(filters.currency||'USD').toUpperCase(),hours,state.uiV2?tab:'all']);}
function updateWorkbenchShopUrl(){const url=new URL(location.href);if(wbState.filters.shop_id)url.searchParams.set('shop_id',wbState.filters.shop_id);else url.searchParams.delete('shop_id');history.replaceState(null,'',url);}
function selectWorkbenchShop(id){if(String(wbState.filters.shop_id||'')===String(id||''))return;clearTimeout(wbQueryTimer);wbState.filters.shop_id=String(id||'');updateWorkbenchShopUrl();loadWorkbench();}
async function loadWorkbench({force=false}={}){
  wbController?.abort();wbController=new AbortController();const {signal}=wbController;
  const filters={...wbState.filters,q:(wbState.filters.q||'').trim()},hours=state.hours,key=workbenchKey(filters,hours);
  const current=()=>!signal.aborted&&key===workbenchKey();
  const cached=workbenchCache.get(key),restored=readSnapshotCache(cached),fresh=restored.fresh;
  if(wbState.dataKey!==key)wbState.data=null;
  if(restored.data&&!force){wbState.data=restored.data;wbState.dataKey=key;}
  wbState.loading=!(fresh&&!force);wbState.error=null;if(state.page==='workbench')render();
  try{
    if(!fresh||force){
      const data=await pageRead(queryPath(state.uiV2?'/api/v2/analytics/workbench':'analytics/workbench',{...filters,lookback_hours:hours,...(state.uiV2?{topic:wbState.tab}:{})}),{signal,force:force||Boolean(cached?.data?._meta&&!fresh),onData:data=>{if(current()){wbState.data=data;wbState.dataKey=key;wbState.loading=false;updateConnection();if(state.page==='workbench')render();}}});if(!current())return;
      if(String(data.scope?.shop_id||'')!==String(filters.shop_id||''))throw new Error('店铺数据范围不一致，请重试。');
      wbState.data=data;wbState.dataKey=key;
      workbenchCache.delete(key);workbenchCache.set(key,{data,savedAt:Date.now()});
      while(workbenchCache.size>6)workbenchCache.delete(workbenchCache.keys().next().value);
      if(!filters.shop_id&&!filters.category&&!filters.q)wbState.categoryOptions=data.categories;
    }
    if(wbState.tab==='competition'){const results=await Promise.allSettled([request('matches/summary',{signal}),list('matches',{limit:100,status:'confirmed'},{signal})]);if(!current())return;wbState.matchesSummary=results[0].status==='fulfilled'?results[0].value:null;wbState.matchItems=results[1].status==='fulfilled'?results[1].value.items:[];}
  }catch(e){if(current())wbState.error=e;}
  finally{if(current()){wbState.loading=false;if(state.page==='workbench')render();}}
}
async function loadMatching(){
  matchingController?.abort();matchingController=new AbortController();const {signal}=matchingController;matchingState.loading=true;matchingState.error=null;if(state.page==='matches')render();
  try{const jobs=[
    pageRead(state.uiV2?'/api/v2/matches/summary':'matches/summary',{signal,onData:data=>{matchingState.summary=data;if(state.page==='matches')render();}}),
    list('matches',{limit:12,after_id:matchingState.cursors.at(-1),status:matchingState.tab==='candidates'?'confirmed':matchingState.tab},{signal}).then(data=>{if(signal.aborted)return;matchingState.items=data.items;matchingState.next=data.next_after_id;matchingState.loading=false;for(const pair of data.items)rememberProducts([pair.left_product,pair.right_product]);if(state.page==='matches')render();})];
    if(matchingState.tab==='candidates')jobs.push(pageRead(state.uiV2?'/api/v2/matches/candidates':'matches/candidates?limit=20',{signal,onData:data=>{matchingState.candidates=data;for(const pair of data.items)rememberProducts([pair.left_product,pair.right_product]);if(state.page==='matches')render();}}));
    await Promise.allSettled(jobs.map(job=>job.catch(error=>{if(!signal.aborted){matchingState.error=error;if(state.page==='matches')render();}})));
  }catch(e){if(!signal.aborted)matchingState.error=e;}
  finally{if(!signal.aborted){matchingState.loading=false;if(state.page==='matches')render();}}
}
async function openDrill(element){
  const params={...wbState.filters},action=element.dataset.wbDrill;let title='当前范围商品明细';
  if(action==='price'){params.price_min=element.dataset.low||undefined;params.price_max=element.dataset.high||undefined;params.price_upper_exclusive=true;title=`公开起价 ${params.price_min||0}–${params.price_max||'以上'} 商品`;}
  if(action==='price-unknown'){params.price_unknown=true;title='未获取公开报价的商品';}
  if(action==='category'){params.category=element.dataset.value;title=params.category==='unknown'?'公开类目未知商品':params.category;}
  if(action==='stock'){params.stock=element.dataset.value;title=labels.stockStatuses[params.stock]||'库存商品';}
  if(action==='attribute'){params.attribute=element.dataset.dimension;params.value=element.dataset.value;title=`${params.value} · 商品明细`;}
  if(action==='shop-name'){const shop=wbState.data.shops.find(s=>String(s.id)===element.dataset.key)||wbState.data.shops.find(s=>s.name===element.dataset.value);if(shop){params.shop_id=shop.id;title=`${shop.name} · 商品明细`;}}
  drillState={params,title,page:1,result:null,scrollTop:0,focusId:null};
  openDialog('ANALYTICS / 图表明细');$('detail-dialog').classList.add('catalog-dialog');catalogView='list';loadDrill();
}
async function loadDrill(){
  const signal=detailsController.signal,version=++drillRequestVersion,context=drillState;
  $('dialog-content').innerHTML=renderCatalogState({title:'商品明细'});focusCatalogHeader();
  try{
    const data=await pageRead(queryPath(state.uiV2?'/api/v2/catalog/products':'analytics/products',{...context.params,page:context.page,page_size:12,include_stats:false}),{signal});
    if(signal.aborted||version!==drillRequestVersion||catalogView!=='list')return;
    rememberProducts(data.items);context.result=data;context.scrollTop=0;
    $('dialog-content').innerHTML=renderDrillProducts(data,context.title);focusCatalogHeader();
  }catch(e){if(!signal.aborted&&version===drillRequestVersion&&catalogView==='list'){$('dialog-content').innerHTML=renderCatalogState({title:'商品明细',error:e,retry:'drill'});focusCatalogHeader();}}
}
function inspectWorkbench(key,detail=''){
  const descriptions={price:'当前公开价格下限的分布。未知报价不进入价格区间，From 起价和价格区间不等于统一单价。',shops:'按当前筛选范围内各店铺已采集商品计算公开起价中位数。不同店铺样本结构、采样深度和规格会影响对照。',categories:'商品原始公开类目或已清洗类目的计数。未提供类目独立归入未知；不表示市场份额。',scatter:'只展示公开价格与评分均已知的商品。评分轴按样本范围自动缩放；同评分的重叠点最多做 6 像素的水平避让，价格纵坐标保持真实，精确数值见悬停。点大小依据评价数，未知评价数使用最小标记。',discount:'依据商品公开原价与现价计算的折扣分组，未知折扣单独计数。',stock:'商品层级的实际库存状态。部分有货、未知和明确缺货分别统计。',matrix:'只统计最近成功详情明确出现、且颜色尺码齐全的 SKU。格子没有观察表示未知组合，不能判断缺货；颜色最多 12 个、尺码最多 10 个。',coverage:'已知字段数量除以当前筛选范围内的商品数量。资料缺失不填零，不能据此推断商品没有该属性。',freshness:'按最近一次成功详情采集时间分组；没有成功详情的商品单独统计。',history:'至少两个不同时间的成功观察才支持历史比较。覆盖跨度来自真实快照最早与最近时间，不代表持续无间断监测。',events:'以实际商品快照对应的观察时间统计变化；首次发现不等于平台上架，日分组按接口标注时区。',matches:'同款与 SKU 关系来自人工确认。只有当前完整规格、币种、价格口径仍一致的已确认 SKU 才可计算价差。'};
  if(key==='shops'&&wbState.tab==='stock')descriptions.shops='以本次有效详情中库存状态明确的 SKU 为分母计算店铺缺货率；保留但本次未出现的历史 SKU 不进入当前分母。';
  const explanation=[detail,descriptions[key]||'按商品对原始公开标签去重计数；多标签商品可以进入多个分组。'].filter(Boolean).join('\n\n');
  openDialog('DATA BASIS / 统计口径');$('dialog-content').innerHTML=`<h2 id="dialog-title">数据口径与覆盖</h2><p class="description-text">${esc(explanation)}</p><div class="detail-grid">${detailField('筛选商品数',number(wbState.data?.summary?.products))}${detailField('币种与价格上下文',`${wbState.data?.scope?.currency} / ${wbState.data?.scope?.pricing_context}`)}${detailField('聚合范围','全部符合当前筛选条件的已采集记录')}${detailField('数据生成时间',formatDate(wbState.data?.generated_at))}</div><div class="stack-gap"><ul class="notes-list">${(wbState.data?.notes||[]).map(n=>`<li>${esc(n)}</li>`).join('')}</ul></div>`;
}
function matchOptions(){return{tokenConnected:matchingState.tokenConnected,writesEnabled:Boolean(state.capabilities?.writes_enabled)};}
function drawMatchForm(){if(matchingForm)$('dialog-content').innerHTML=renderMatchForm(matchingForm,matchOptions());}
async function openManualMatch(pair=null,relation=null){
  const signal=openDialog('MATCH REVIEW / 关系核对');$('detail-dialog').classList.add('match-dialog');
  matchingForm={left:pair?.left_product||relation?.left_product||null,right:pair?.right_product||relation?.right_product||null,relation,type:relation?'same_sku':'same_product',products:[],search:'',leftVariants:[],rightVariants:[],leftVariant:'',rightVariant:'',note:'',loading:false};
  try{const data=await request('analytics/products?page_size=100&sort=recent',{signal});if(signal.aborted)return;rememberProducts(data.items);matchingForm.products=data.items;if(relation){const variants=await Promise.all([collect(`products/${relation.left_product_id}/variants`,{}, {signal,maxPages:10}),collect(`products/${relation.right_product_id}/variants`,{}, {signal,maxPages:10})]);if(signal.aborted)return;matchingForm.leftVariants=variants[0].items.filter(v=>v.seen_in_latest_detail!==false);matchingForm.rightVariants=variants[1].items.filter(v=>v.seen_in_latest_detail!==false);}drawMatchForm();}
  catch(e){if(!signal.aborted)$('dialog-content').innerHTML=`<h2 id="dialog-title">核对匹配关系</h2>${detailError(e)}`;}
}
function matchError(message){if($('match-form-error'))$('match-form-error').textContent=message;else toast(message);}
async function saveMatchingDecision(){
  if(!$('match-confirmed')?.checked){matchError('请先核对双方信息并勾选确认。');return;}if(!matchToken){matchError('请先连接已配置的 API 写入令牌。');return;}
  const f=matchingForm;if(!f?.left||!f?.right||f.left.id===f.right.id){matchError('请选择两件不同的商品。');return;}
  const body={left_product_id:f.left.id,right_product_id:f.right.id,match_type:f.type,confirmed:true,note:$('match-note').value.trim()||null};
  if(f.type==='same_sku'){body.left_variant_id=$('match-left-variant').value;body.right_variant_id=$('match-right-variant').value;if(!body.left_variant_id||!body.right_variant_id){matchError('请选择双方实际的 SKU 规格。');return;}}
  const button=document.querySelector('[data-match-action="save"]');button.disabled=true;
  try{await write('matches',body,{token:matchToken,signal:detailsController.signal});closeDialog();matchingForm=null;toast('匹配关系已保存到数据库。');loadMatching();if(state.page==='workbench')loadWorkbench();}
  catch(e){if(e.name!=='AbortError'){matchError(e.message);button.disabled=false;}}
}
async function handleMatchingAction(element){
  const action=element.dataset.matchAction;
  if(action==='manual'){openManualMatch();return;}if(action==='candidate'){openManualMatch(matchingState.candidates?.items[Number(element.dataset.index)]);return;}
  if(action==='sku'){const relation=matchingState.items.find(r=>r.id===element.dataset.id);if(relation)openManualMatch(null,relation);return;}
  if(action==='auth'){if(matchingForm&&$('match-note'))matchingForm.note=$('match-note').value;openDialog('WRITE ACCESS / 写入连接');$('detail-dialog').classList.add('match-dialog');$('dialog-content').innerHTML=renderMatchingAuth(Boolean(matchToken));return;}
  if(action==='connect-token'){const value=$('matching-token').value.trim();if(value.length<24){matchError('请输入服务中配置的完整写入令牌（至少 24 个字符）。');return;}matchToken=value;matchingState.tokenConnected=true;if(matchingForm)drawMatchForm();else closeDialog();toast('写入令牌已保留在当前页面会话。');render();return;}
  if(action==='disconnect-token'){matchToken='';matchingState.tokenConnected=false;closeDialog();toast('写入令牌已断开。');render();return;}
  if(action==='search'){matchingForm.search=$('match-search').value.trim();if($('match-note'))matchingForm.note=$('match-note').value;try{const data=await request(queryPath('analytics/products',{q:matchingForm.search,page_size:100,sort:'title'}),{signal:detailsController.signal});rememberProducts(data.items);matchingForm.products=data.items;drawMatchForm();}catch(e){if(e.name!=='AbortError')matchError(e.message);}return;}
  if(action==='save'){saveMatchingDecision();return;}
  if(action==='review'||action==='review-sku'){const item=matchingState.items.find(r=>r.id===element.dataset.id);if(item){matchingForm=null;openDialog('MATCH REVIEW / 关系复核');$('detail-dialog').classList.add('match-dialog');$('dialog-content').innerHTML=renderReviewMatch(item,element.dataset.status,element.dataset.sku);}return;}
  if(action==='save-review'){if(!$('match-confirmed')?.checked){matchError('请勾选确认本次关系复核。');return;}if(!matchToken){matchError('请先在匹配页面连接写入令牌。');return;}const {id,sku,status}=element.dataset;try{element.disabled=true;await write(sku?`matches/${id}/skus/${sku}`:`matches/${id}`,{status,note:$('match-note').value.trim()||null},{method:'PATCH',token:matchToken,signal:detailsController.signal});closeDialog();toast('关系状态与证据已更新。');loadMatching();}catch(e){if(e.name!=='AbortError'){matchError(e.message);element.disabled=false;}}}
}
async function loadTasks() {
  tasksController?.abort();tasksController=new AbortController();const {signal}=tasksController;state.tasksLoading=true;state.taskError=null;render();
  try{const data=await list('tasks',{limit:state.pageSize,after_id:state.taskCursors.at(-1),status:state.taskStatus,task_type:state.taskType},{signal});if(!signal.aborted){state.tasks=data.items;state.taskNext=data.next_after_id;}}
  catch(e){if(!signal.aborted){state.tasks=[];state.taskNext=null;state.taskError=e;}}
  finally{if(!signal.aborted){state.tasksLoading=false;if(state.page==='tasks')render();}}
}
async function loadJobs() {
  jobsController?.abort();jobsController=new AbortController();const {signal}=jobsController;state.jobsLoading=true;state.jobsError=null;render();
  try{const data=await (state.apiV2?pageRead(queryPath('/api/v2/reports',{limit:8,after_id:state.reportJobsCursors.at(-1)}),{signal}):list('tasks',{task_type:'report_generation',limit:8,after_id:state.reportJobsCursors.at(-1)},{signal}));if(!signal.aborted){state.reportJobs=data.items;state.reportJobsNext=data.next_after_id;
    const archived=data.latest_completed||data.items.find(job=>job.status==='succeeded'&&job.summary);
    if(archived)state.reportArchive={task_id:archived.task_id,summary:archived.summary,as_of:archived.report_as_of,generated_at:archived.report_as_of,currency:state.capabilities?.currency||'USD',region:state.capabilities?.region||'US',lookback_hours:archived.lookback_hours,notes:['统计来自这份已完成报告，观察截止时间保持不变。']};}}
  catch(e){if(!signal.aborted){state.reportJobs=[];state.reportJobsNext=null;state.jobsError=e;}}
  finally{if(!signal.aborted){state.jobsLoading=false;if(state.page==='reports')render();}}
}
async function loadEvents({reset=false}={}){
  eventsController?.abort();eventsController=new AbortController();const {signal}=eventsController;
  if(reset){state.eventCursors=[null];state.pageNumber=1;state.eventsData=null;state.eventStats=null;}
  state.eventsLoading=true;state.errors.events=null;if(state.page==='events')render();
  const filters={lookback_hours:state.hours,event_type:state.eventType,q:state.query};
  try{await pageRead(queryPath('/api/v2/events',{...filters,cursor:state.eventCursors[state.pageNumber-1],limit:state.pageSize}),{signal,maxAge:10000,onData:data=>{
    if(signal.aborted)return;state.eventsData=data;state.eventsLoading=false;state.eventCursors=state.eventCursors.slice(0,state.pageNumber);state.eventCursors[state.pageNumber]=data.next_cursor;if(state.page==='events')render();}});
    if(signal.aborted)return;
    await pageRead(queryPath('/api/v2/events/summary',{...filters,...state.eventsData.window}),{signal,onData:data=>{state.eventStats=data;if(state.page==='events')render();}});
  }catch(error){if(!signal.aborted)state.errors.events=error;}
  finally{if(!signal.aborted){state.eventsLoading=false;if(state.page==='events')render();}}
}
function navigate() {
  clearTimeout(productQueryTimer);
  for(const controller of [refreshController,productsController,productStatsController,wbController,matchingController,eventsController,tasksController,jobsController])controller?.abort();
  const page=location.hash.slice(1).split('?')[0];state.page=Object.hasOwn(meta,page)?page:'workbench';
  state.query='';state.pageNumber=1;state.category='';state.stock='';state.shop='';state.eventType='';state.shopFilter='all';
  state.eventCursors=[null];state.eventsData=null;state.eventStats=null;
  closeMenu();render();if(!state.initializing)refresh({force:false});
  window.scrollTo({top:0,behavior:'instant'});
}
function toast(message) { clearTimeout(toastTimer);$('toast').textContent=message;$('toast').hidden=false;toastTimer=setTimeout(()=>$('toast').hidden=true,4500); }
function openDialog(eyebrow='DETAILS') {
  detailsController?.abort();detailsController=new AbortController();if(!$('detail-dialog').open)detailFocus=document.activeElement;$('dialog-eyebrow').textContent=eyebrow;
  catalogView=null;productDetail=null;shopDetail=null;
  $('detail-dialog').classList.remove('comparison-dialog');
  $('detail-dialog').classList.remove('match-dialog','wb-drill-dialog','catalog-dialog');
  $('dialog-content').innerHTML=`<h2 id="dialog-title">详情</h2>${loading()}`;
  if(!$('detail-dialog').open)$('detail-dialog').showModal();$('detail-dialog').scrollTop=0;return detailsController.signal;
}
function detailField(label,value) { return `<div class="detail-field"><small>${esc(label)}</small><span>${esc(present(value))}</span></div>`; }
function detailError(e) { return `<div class="notice warning">${icon('alert')}<span>${esc(e.message||'详情暂不可用。')}</span></div>`; }
function focusCatalogHeader(){document.querySelector('.catalog-dialog-top button')?.focus({preventScroll:true});}
function productIsActive(model){return productDetail===model&&catalogView==='product'&&$('detail-dialog').open&&!detailsController.signal.aborted;}
function drawProductPanel({focusTab=false,preserveSearch=true}={}){
  if(!productDetail?.product||catalogView!=='product')return;
  const active=document.activeElement,input=preserveSearch&&active===$('pi-sku-search')?active:null,selection=input?[input.selectionStart,input.selectionEnd]:null;
  const panel=$('pi-panel'),restorePanelFocus=panel.contains(active),filter=active?.dataset.piFilter;
  panel.innerHTML=renderProductPanel(productDetail);panel.setAttribute('aria-labelledby',`pi-tab-${productDetail.tab}`);panel.scrollTop=0;
  document.querySelectorAll('.pi-tab').forEach(tab=>{const active=tab.dataset.piTab===productDetail.tab;tab.classList.toggle('active',active);tab.setAttribute('aria-selected',String(active));tab.tabIndex=active?0:-1;});
  if(focusTab)$(`pi-tab-${productDetail.tab}`).focus({preventScroll:true});
  if(!focusTab){if(selection&&$('pi-sku-search')){$('pi-sku-search').focus({preventScroll:true});$('pi-sku-search').setSelectionRange(...selection);}else if(filter){document.querySelector(`[data-pi-filter="${filter}"]`)?.focus({preventScroll:true});}else if(restorePanelFocus)panel.focus({preventScroll:true});}
}
function selectProductTab(tab,focusTab=true){if(!productDetail?.product||!['overview','variants','reviews','history','info'].includes(tab))return;productDetail.tab=tab;drawProductPanel({focusTab});const key=tab==='info'?'content':tab==='reviews'?'reviews':null;if(key&&!productDetail[key]&&!productDetail.loading[key]&&!productDetail.errors[key])loadProductResource(key);}
async function openProduct(id,{back}={}) {
  if(back===undefined)back=catalogView==='list'&&Boolean(drillState?.result);
  if(back&&catalogView==='list'){drillState.scrollTop=document.querySelector('.catalog-list-body')?.scrollTop||0;drillState.focusId=id;}
  const signal=openDialog('PRODUCT / 商品档案'),path=`products/${encodeURIComponent(id)}`;
  const model=productDetail={id,back,product:null,variants:null,snapshots:null,images:null,content:null,reviews:null,errors:{},loading:{},tab:'overview',skuSearch:'',skuScope:'all',skuPage:1,historyPage:1,historyContext:''};
  catalogView='product';$('detail-dialog').classList.add('catalog-dialog');$('dialog-content').innerHTML=renderCatalogState({back});focusCatalogHeader();
  const resources=[['product',request(path,{signal})],['variants',collect(`${path}/variants`,{}, {signal,maxPages:1})],['snapshots',collect(`${path}/snapshots`,{}, {signal,maxPages:1})],['images',request(`${path}/images`,{signal})]];
  for(const [key]of resources)model.loading[key]=true;
  await Promise.allSettled(resources.map(async([key,promise])=>{
    try{const data=await promise;if(!productIsActive(model))return;model[key]=data;}
    catch(error){if(!productIsActive(model))return;model.errors[key]=error;}
    finally{if(productIsActive(model)){model.loading[key]=false;if(model.product){$('dialog-content').innerHTML=renderProductInspector(model);if(key==='product')focusCatalogHeader();}else if(model.errors.product){$('dialog-content').innerHTML=renderCatalogState({title:'商品详情',back,error:model.errors.product});}}}
  }));
}
function returnToDrill(){
  if(!drillState?.result)return;
  openDialog('ANALYTICS / 图表明细');catalogView='list';$('detail-dialog').classList.add('catalog-dialog');
  $('dialog-content').innerHTML=renderDrillProducts(drillState.result,drillState.title);
  const body=document.querySelector('.catalog-list-body');if(body)body.scrollTop=drillState.scrollTop||0;
  const trigger=[...document.querySelectorAll('.catalog-list-body [data-action="product"]')].find(el=>el.dataset.id===drillState.focusId);if(trigger)trigger.focus({preventScroll:true});else focusCatalogHeader();
}
async function loadProductResource(key,more=false){
  const model=productDetail;if(!model?.product||model.loading[key]||!['variants','snapshots','images','content','reviews'].includes(key))return;
  const signal=detailsController.signal,path=`products/${encodeURIComponent(model.id)}/${key}`;
  model.loading[key]=true;delete model.errors[key];drawProductPanel();
  try{
    const data=key==='reviews'?await list(path,{limit:20,...(more?{after_id:model.reviews?.next_after_id}:{})},{signal}):['images','content'].includes(key)?await request(path,{signal}):await collect(path,more?{after_id:model[key]?.next_after_id}:{},{signal,maxPages:10});
    if(!productIsActive(model))return;
    if(key==='content'&&(!data||String(data.product_id)!==String(model.id)||typeof data.readiness!=='boolean'||typeof data.available!=='boolean'||!(data.content===null||typeof data.content==='object')))throw new Error('商品介绍数据无法读取，请重试。');
    if(key==='reviews'){if(String(data.product_id)!==String(model.id)||typeof data.readiness!=='boolean')throw new Error('评论数据无法读取，请重试。');data.complete=data.next_after_id===null;}
    model[key]=more?{...data,items:[...(model[key]?.items||[]),...data.items]}:data;
  }catch(e){if(productIsActive(model))model.errors[key]=e;}
  finally{
    if(productIsActive(model)){
      model.loading[key]=false;
      if(key==='images'){
        const holder=document.createElement('div');holder.innerHTML=renderProductInspector(model);document.querySelector('.pi-media').replaceWith(holder.querySelector('.pi-media'));
      }
      const count=document.querySelector(`[data-pi-count="${key==='snapshots'?'history':key}"]`);
      if(count&&model[key])count.textContent=key==='reviews'?model.reviews.collected_count:`${model[key].items.length}${model[key].complete?'':'+'}`;
      if(key==='variants')$('pi-sku-count').textContent=skuCount(model);
      if((key==='variants'&&model.tab==='variants')||(key==='reviews'&&model.tab==='reviews')||(key==='snapshots'&&model.tab==='history')||model.tab==='info')drawProductPanel();
    }
  }
}
function openShop(id) {
  loadShopDetail(id);
}
const profileStateLabels={exact:'精确值',display_only:'仅展示值',not_provided:'未获取',invalid:'来源异常',observed:'来源已显示',not_displayed:'来源未展示'};
function shopIsActive(model){return shopDetail===model&&$('detail-dialog').open&&!detailsController.signal.aborted;}
function renderShopHistory(model){
  const rows=model.history?.items||[];
  return `<div class="table-title-row"><h3>历史档案观察</h3><span class="muted">每页 20 次 · 按保存顺序</span></div>${model.historyError?detailError(model.historyError):model.historyLoading?loading(true):rows.length?`<div class="table-wrap"><table><thead><tr><th>采集时间</th><th>粉丝</th><th>累计已售</th><th>视频</th><th>字段状态</th></tr></thead><tbody>${rows.map(row=>`<tr><td class="event-time">${esc(formatDate(row.captured_at))}</td><td>${profileMetricCell(row,'follower_count')}</td><td>${profileMetricCell(row,'sales_count')}</td><td>${profileMetricCell(row,'video_count')}</td><td><span class="badge ${row.observation_status==='complete'?'green':'amber'}">${row.observation_status==='complete'?'完整':'部分缺失'}</span></td></tr>`).join('')}</tbody></table></div>`:empty('尚无档案历史','独立的店铺档案采集完成后，每次有效观察会保存在这里。',true)}<div class="shop-history-footer"><span>第 ${model.cursors.length} 页 · 时间为北京时间</span><div class="pagination"><button class="button" data-action="shop-history-prev" ${model.cursors.length===1||model.historyLoading?'disabled':''}>${icon('left')}上一页</button><button class="button" data-action="${model.historyError?'shop-history-retry':'shop-history-next'}" ${model.historyLoading||(!model.historyError&&!model.history?.next_after_id)?'disabled':''}>${model.historyError?'重试':'下一页'}${icon('chevron')}</button></div></div>`;
}
function drawShopDetail(model){
  const s=model.shop,task=s.latest_profile_task,metrics=[['follower_count','粉丝'],['sales_count','累计已售'],['video_count','视频']];
  const old=s.profile_stale||Boolean(task?.last_attempt?.succeeded===false);
  const statusNote=!s.profile_captured_at?'尚未获取店铺档案，商品采样时间不代表档案已刷新。':old?`上次采集于 ${formatDate(s.profile_captured_at)}；当前保留上一次有效观察。`:s.profile_observation_status==='partial'?'本次档案已保存，部分字段未获取或只有来源展示值。':`档案采集于 ${formatDate(s.profile_captured_at)}（北京时间）。`;
  const detailMetrics=[...metrics,['global_sales_count','全球累计已售（来源标注）'],['product_count','平台在售商品数'],['review_count','店铺评价数']];
  $('dialog-content').innerHTML=`<div class="shop-profile-heading">${shopAvatar(s,true)}<div><div class="shop-name-line"><h2 id="dialog-title">${esc(s.name)}</h2>${profileBadges(s)}</div><p class="detail-subtitle">TikTok Shop · ${esc(s.region)} · ${esc(s.platform_shop_id)}</p>${external(s.shop_url,'访问公开店铺')}</div></div><div class="detail-stats shop-profile-stats">${metrics.map(([key,label])=>`<div class="detail-stat"><small>${label}</small><strong>${esc(profileMetric(s,key))}</strong><span class="shop-metric-state">${esc(profileStateLabels[profileFieldState(s,key)]||'未获取')}</span></div>`).join('')}</div>${notice(statusNote,old||!s.profile_captured_at)}<div class="detail-section"><h3>指标原始值</h3><div class="table-wrap"><table><thead><tr><th>指标</th><th>精确值</th><th>来源展示</th><th>状态</th></tr></thead><tbody>${detailMetrics.map(([key,label])=>`<tr><td>${label}</td><td class="num">${esc(exactCount(s[key]))}</td><td>${esc(s[`${key}_text`]||'—')}</td><td>${esc(profileStateLabels[profileFieldState(s,key)]||'未获取')}</td></tr>`).join('')}</tbody></table></div><div class="detail-grid">${detailField('店铺评分',s.rating??'未获取')}${detailField('在售商品数采集时间',formatDate(s.product_count_captured_at))}${detailField('蓝 V 标识',s.verification_type==='blue_v'?'来源显示蓝 V':profileStateLabels[s.profile_field_state?.verification_type?.state]||'未获取')}${detailField('官方店铺标识',s.identity_label_text||profileStateLabels[s.profile_field_state?.identity_label_key?.state]||'未获取')}</div><p class="snapshot-note">累计已售为店铺页来源指标；未将缩写换算为精确值，未从商品销量相加推算。</p></div><div class="detail-section"><h3>档案来源与采集状态</h3><div class="detail-grid">${detailField('档案采集时间',formatDate(s.profile_captured_at))}${detailField('最近档案任务',task?labels.taskStatuses[task.status]||task.status:'尚无任务')}${detailField('任务尝试次数',task?`${task.attempt_count} / ${task.max_attempts}`:null)}${detailField('最近响应',task?.last_attempt?.response_status)}${detailField('失败类型',task?.last_attempt?.error_type||'—')}${detailField('解析版本',s.profile_parser_version)}</div><div class="stack-gap">${external(s.profile_source_url,'查看档案来源')}${task?` <button class="row-link" data-action="task" data-id="${esc(task.task_id)}">查看采集任务 ${icon('arrow')}</button>`:''}</div></div><div id="shop-profile-history" class="detail-section">${renderShopHistory(model)}</div><div class="detail-section"><h3>已采商品样本</h3><div class="detail-stats">${[['商品样本',state.report?number(s.sampled_products||0):'—'],['起价中位数',money(s.starting_price_stats?.median,state.report?.currency)],['历史详情',state.report?number(s.historical_detail_products||0):'—']].map(([k,v])=>`<div class="detail-stat"><small>${k}</small><strong>${v}</strong></div>`).join('')}</div><div class="detail-grid">${detailField('来源分组',s.source_group||s.discovery_category)}${detailField('最近观察',formatDate(s.last_seen_at))}${detailField('已观察 SKU',state.report?s.sku_count||0:null)}${detailField('已知库存 SKU',state.report?s.known_stock_skus||0:null)}${detailField('明确缺货 SKU',state.report?s.sku_out_of_stock||0:null)}${detailField('SKU 缺货率',pct(s.sku_stockout_pct))}</div><div class="stack-gap">${notice('样本指标仅覆盖已采集商品，尚未采样不等于店铺没有商品。')}</div><button class="button primary" data-action="shop-products" data-id="${esc(s.id)}">查看该店铺商品 ${icon('arrow')}</button></div>`;
}
async function loadShopDetail(id){
  const signal=openDialog('SHOP / 店铺档案'),model=shopDetail={id,shop:null,history:null,cursors:['0'],historyLoading:false,historyError:null};
  const results=await Promise.allSettled([request(`shops/${encodeURIComponent(id)}`,{signal}),list(`shops/${encodeURIComponent(id)}/snapshots`,{limit:20},{signal})]);
  if(!shopIsActive(model))return;
  if(results[0].status==='rejected'){$('dialog-content').innerHTML=`<h2 id="dialog-title">店铺档案</h2>${detailError(results[0].reason)}`;return;}
  model.shop={...allShops().find(s=>s.id===id),...results[0].value};
  if(results[1].status==='fulfilled')model.history=results[1].value;else model.historyError=results[1].reason;
  drawShopDetail(model);
}
async function loadShopHistory(direction){
  const model=shopDetail;if(!model||model.historyLoading)return;
  if(direction==='next'&&model.history?.next_after_id)model.cursors.push(model.history.next_after_id);
  if(direction==='prev'&&model.cursors.length>1)model.cursors.pop();
  model.historyLoading=true;model.historyError=null;$('shop-profile-history').innerHTML=renderShopHistory(model);
  try{const history=await list(`shops/${encodeURIComponent(model.id)}/snapshots`,{limit:20,after_id:model.cursors.at(-1)},{signal:detailsController.signal});if(shopIsActive(model))model.history=history;}
  catch(error){if(shopIsActive(model))model.historyError=error;}
  finally{if(shopIsActive(model)){model.historyLoading=false;$('shop-profile-history').innerHTML=renderShopHistory(model);}}
}
async function openTask(id) {
  const signal=openDialog('TASK / 执行详情');
  try{
    const data=await request(`tasks/${encodeURIComponent(id)}/attempts`,{signal});if(signal.aborted)return;const t=data.task;
    const progressLabels={page_number:'采集页码',page_products:'本页商品',new_products:'新增商品',new_detail_tasks:'新增详情任务',has_more:'尚有后续页面',complete:'采集完成',detail_updated:'详情已更新',variants_seen:'观察 SKU',variants_upserted:'写入 SKU',new_variants:'新增 SKU',snapshots_written:'已写入快照',variant_snapshots_written:'SKU 快照',events_detected:'检测事件',events_written:'写入事件',baseline:'初始基线',snapshot_id:'快照 ID',previous_snapshot_id:'前次快照 ID'};
    $('dialog-content').innerHTML=`<h2 id="dialog-title">${esc(labels.taskTypes[t.task_type]||t.task_type)}</h2><p class="detail-subtitle">${esc(t.task_id)}</p>${taskBadge(t.status)}<div class="detail-grid">${[['资源',`${t.resource_type} · ${t.resource_id}`],['执行次数',`${t.attempt_count} / ${t.max_attempts}`],['开始时间',formatDate(t.started_at)],['完成时间',formatDate(t.finished_at)],['Worker',t.worker_id],['租约到期',formatDate(t.lease_expires_at)]].map(([k,v])=>detailField(k,v)).join('')}</div><div class="detail-section"><h3>采集进度</h3><div class="detail-grid">${Object.entries(t.progress||{}).map(([k,v])=>detailField(progressLabels[k]||k,typeof v==='boolean'?(v?'是':'否'):v)).join('')||'<span class="muted">尚无采集检查点</span>'}</div></div><div class="detail-section"><h3>执行尝试</h3>${data.attempts.length?`<div class="table-wrap"><table><thead><tr><th>尝试</th><th>状态</th><th>响应</th><th>耗时</th><th>错误类型</th></tr></thead><tbody>${data.attempts.map(a=>`<tr><td>#${a.attempt_no}</td><td><span class="badge ${a.succeeded?'green':'amber'}">${a.succeeded?'成功':a.finished_at?'未成功':'执行中'}</span></td><td>${esc(present(a.response_status))}</td><td>${a.latency_ms==null?'—':number(a.latency_ms)+' ms'}</td><td>${esc(a.error_type||'—')}</td></tr>`).join('')}</tbody></table></div>`:empty('尚未开始执行','执行尝试将在 Worker 处理任务后显示。',true)}</div>`;
  }catch(e){if(!signal.aborted)$('dialog-content').innerHTML=`<h2 id="dialog-title">任务详情</h2>${detailError(e)}`;}
}
function openEvent(id) {
  const e=[...(state.eventsData?.items||[]),...(state.report?.events||[])].find(e=>e.id===id);if(!e)return;openDialog('EVENT / 变化依据');
  $('dialog-content').innerHTML=`<h2 id="dialog-title">${esc(labels.eventTypes[e.event_type]||e.event_type)}</h2><p class="detail-subtitle">${esc(e.title||'商品 '+e.product_id)} · ${esc(e.shop_name||'')}</p><div class="detail-grid">${detailField('观察时间',formatDate(e.observed_at))}${detailField('检测时间',formatDate(e.detected_at))}${detailField('当前快照',e.snapshot_id)}${detailField('前次快照',e.previous_snapshot_id)}${detailField('平台 SKU',e.platform_variant_id)}${detailField('SKU 选项',e.variant_options)}${detailField('变化前',e.old_value)}${detailField('变化后',e.new_value)}</div><div class="stack-gap">${notice('变化由前后快照比较产生。首次观察建立基线，SKU 缺失不直接判定为缺货。')}</div><button class="button primary" data-action="product" data-id="${esc(e.product_id)}">查看商品及历史快照 ${icon('arrow')}</button>`;
}
async function openComparison() {
  const byId=new Map([...(state.report?.products||[]).map(p=>[p.id,p]),...state.productCache]);
  const products=[...state.selectedProducts].map(id=>byId.get(id)).filter(Boolean);
  if(products.length<2||products.length>4){toast('请选择 2–4 件商品进行对照。');return;}
  const signal=openDialog('PRODUCT COMPARISON / 商品对照');$('detail-dialog').classList.add('comparison-dialog');
  try{const html=await buildComparison(products,{signal});if(!signal.aborted&&$('detail-dialog').open)$('dialog-content').innerHTML=html;}
  catch(e){if(!signal.aborted)$('dialog-content').innerHTML=`<h2 id="dialog-title">商品对照</h2>${detailError(e)}`;}
}
function saveView(){
  const name=window.prompt('为当前商品筛选视图命名','商品观察视图');if(!name?.trim())return;
  const view={name:name.trim().slice(0,48),filters:{query:state.query,category:state.category,stock:state.stock,shop:state.shop},columns:[...state.productColumns],sortKey:state.sortKey,sortDirection:state.sortDirection};
  const duplicate=state.savedViews.findIndex(v=>v.name===view.name);if(duplicate>=0)state.savedViews[duplicate]=view;else state.savedViews.unshift(view);
  state.savedViews=state.savedViews.slice(0,12);
  try{localStorage.setItem(viewStorageKey,JSON.stringify(state.savedViews));toast('筛选视图已保存在当前浏览器。');}catch{toast('浏览器暂不能保存视图；本次会话仍可使用。');}
  render();
}
function openSystem() {
  openDialog('SYSTEM / 工作台状态');const c=state.capabilities;
  $('dialog-content').innerHTML=`<h2 id="dialog-title">系统状态</h2><p class="detail-subtitle">当前市场与采集能力</p>${!c?notice('系统能力接口暂不可用。请刷新数据后重试。',true):''}<div class="system-grid">${[['市场',c?.region],['平台',c?.platform],['服务能力',c?.implemented_task_types?.map(t=>labels.taskTypes[t]||t).join('、')],['当前启用采集',c?.enabled_task_types?.length?c.enabled_task_types.map(t=>labels.taskTypes[t]||t).join('、'):c?'尚未启用':null],['写接口状态',c?c.writes_enabled?'已配置':'未启用':null],['数据连接',state.overview?'可用':'暂不可用'],['观察窗口',`${state.hours} 小时`],['显示时区','Asia/Shanghai']].map(([k,v])=>`<div class="summary-row"><span>${k}</span><strong>${esc(present(v))}</strong></div>`).join('')}</div><div class="read-mode">${icon('shield')}当前工作台提供数据查看与文件导出</div><div class="stack-gap"><a class="button" href="/docs" target="_blank" rel="noopener noreferrer">查看接口文档 ${icon('external')}</a></div>`;
}
function exportRows() {
  const columns={shops:[{key:'name',label:'店铺名称'},{key:'platform_shop_id',label:'店铺 ID'},{key:'shop_url',label:'公开链接'},{key:'sampled_products',label:'商品样本数'},{key:r=>r.starting_price_stats?.median,label:'起价中位数'}],products:[{key:'title',label:'商品名称'},{key:'platform_product_id',label:'商品 ID'},{key:'shop_name',label:'店铺'},{key:'category',label:'类目'},{key:'currency',label:'币种'},{key:'price_min',label:'价格下限'},{key:'price_max',label:'价格上限'},{key:r=>labels.stockStatuses[r.stock_status],label:'库存状态'},{key:'observed_at',label:'观察时间'},{key:'product_url',label:'商品链接'}],events:[{key:'id',label:'事件 ID'},{key:r=>labels.eventTypes[r.event_type],label:'事件类型'},{key:'title',label:'商品名称'},{key:'shop_name',label:'店铺'},{key:'old_value',label:'变化前'},{key:'new_value',label:'变化后'},{key:'observed_at',label:'观察时间'},{key:'snapshot_id',label:'当前快照'},{key:'previous_snapshot_id',label:'前次快照'}]};
  columns.shops.push({key:'platform',label:'平台'},{key:'region',label:'市场'},{key:'logo_url',label:'Logo 来源'},
    {key:'verification_type',label:'认证标识'},{key:'identity_label_key',label:'店铺身份代码'},{key:'identity_label_text',label:'店铺身份原文'},
    ...[['follower_count','粉丝'],['sales_count','累计已售'],['global_sales_count','全球累计已售'],['video_count','视频'],['product_count','平台在售商品']].flatMap(([key,label])=>[{key,label:`${label}精确值`},{key:`${key}_text`,label:`${label}展示原文`}]),
    {key:'rating',label:'店铺评分'},{key:'review_count',label:'店铺评价数'},{key:'profile_captured_at',label:'档案采集时间 UTC'},
    {key:'product_count_captured_at',label:'在售商品数采集时间 UTC'},{key:'profile_source_url',label:'档案来源'},
    {key:'profile_parser_version',label:'档案解析版本'},{key:'profile_observation_status',label:'档案观察状态'},
    {key:'profile_status',label:'档案当前状态'},{key:'profile_field_state',label:'字段状态与口径'});
  const rows=state.page==='products'&&state.selectedProducts.size?[...state.selectedProducts].map(id=>state.productCache.get(id)).filter(Boolean):{shops:filteredShops,products:filteredProducts,events:filteredEvents}[state.page]?.();if(!rows?.length){toast('没有可导出的匹配数据。');return;}
  csvDownload(`shopscope-${state.page}-${new Date().toISOString().slice(0,10)}.csv`,columns[state.page],rows);toast(`已导出 ${rows.length} 条匹配数据。`);
}
async function downloadReport(format,jobId) {
  jobId=jobId||state.reportArchive?.task_id;
  if(!jobId){toast('请先选择已完成的报告。');return;}
  const path=jobId?`/api/v1/reports/jobs/${encodeURIComponent(jobId)}/files/${format}`:`/api/v1/reports/current?lookback_hours=${state.hours}&format=${format}`;
  try{const response=await fetch(deploymentPath(path),{credentials:'same-origin',cache:'no-store'});if(!response.ok){toast(response.status===404?'报告文件暂不可用。':'报告下载失败，请稍后重试。');return;}const blob=await response.blob(),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=`shopscope-report-${jobId||'current'}.${format}`;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),10000);toast('报告下载已开始。');}catch{toast('无法连接服务，报告下载失败。');}
}
function closeDialog(){detailsController?.abort();$('detail-dialog').close();}
const mobileLayout=window.matchMedia('(max-width:900px)');
function syncSidebar(){ $('sidebar').inert=mobileLayout.matches&&!$('sidebar').classList.contains('open'); }
function closeMenu(){const focusInNav=$('sidebar').contains(document.activeElement);$('sidebar').classList.remove('open');$('mobile-backdrop').hidden=true;document.querySelector('[data-action="menu"]').setAttribute('aria-expanded','false');syncSidebar();if(focusInNav&&mobileLayout.matches)document.querySelector('[data-action="menu"]').focus();}

document.addEventListener('click',e=>{
  if(e.target.closest('[data-wb-action="retry"]')){loadWorkbench({force:true});return;}
  const pi=e.target.closest('[data-pi-tab],[data-pi-page],[data-pi-more],[data-pi-retry]');
  if(pi&&!pi.disabled){
    if(pi.dataset.piTab){selectProductTab(pi.dataset.piTab);return;}
    if(pi.dataset.piPage&&productDetail){const field=pi.dataset.piPage==='variants'?'skuPage':'historyPage';productDetail[field]+=Number(pi.dataset.step);drawProductPanel();$('pi-panel').focus({preventScroll:true});return;}
    if(pi.dataset.piMore){loadProductResource(pi.dataset.piMore,true);return;}
    if(pi.dataset.piRetry==='drill'){loadDrill();return;}
    if(pi.dataset.piRetry==='product'&&productDetail){openProduct(productDetail.id,{back:productDetail.back});return;}
    if(pi.dataset.piRetry){loadProductResource(pi.dataset.piRetry);return;}
  }
  const wb=e.target.closest('[data-wb-tab],[data-wb-drill],[data-wb-inspect],[data-wb-action],[data-wb-drill-page]');
  if(wb&&!wb.disabled){if(wb.dataset.wbTab){wbState.tab=wb.dataset.wbTab;render();if(state.uiV2||wbState.tab==='competition')loadWorkbench();return;}if(wb.dataset.wbDrill){openDrill(wb);return;}if(wb.dataset.wbInspect){inspectWorkbench(wb.dataset.wbInspect,wb.dataset.wbDetail);return;}if(wb.dataset.wbDrillPage){drillState.page+=wb.dataset.wbDrillPage==='next'?1:-1;loadDrill();return;}if(wb.dataset.wbAction==='clear'){wbState.filters={shop_id:'',category:'',q:'',currency:wbState.filters.currency};clearTimeout(wbQueryTimer);updateWorkbenchShopUrl();loadWorkbench();return;}}
  const match=e.target.closest('[data-match-action],[data-match-tab],[data-match-page]');
  if(match&&!match.disabled){if(match.dataset.matchAction){handleMatchingAction(match);return;}if(match.dataset.matchTab){matchingState.tab=match.dataset.matchTab;matchingState.cursors=['0'];loadMatching();return;}if(match.dataset.matchPage){if(match.dataset.matchPage==='next')matchingState.cursors.push(matchingState.next);else matchingState.cursors.pop();loadMatching();return;}}
  const photo=e.target.closest('[data-photo-url]');if(photo){const url=safeUrl(photo.dataset.photoUrl);if(url&&$('gallery-main-photo')){$('gallery-main-photo').src=url;$('gallery-main-photo').hidden=false;const fallback=$('gallery-main-photo').parentElement.querySelector('.product-photo-fallback');if(fallback)fallback.hidden=true;document.querySelectorAll('.photo-thumb,.pi-thumb').forEach(el=>{el.classList.toggle('active',el===photo);el.setAttribute('aria-pressed',String(el===photo));});if($('pi-full-image'))$('pi-full-image').href=url;const original=document.querySelector('.pi-original-link');if(original)original.href=url;if($('pi-photo-index'))$('pi-photo-index').textContent=`${photo.dataset.photoIndex} / ${document.querySelectorAll('.pi-thumb').length}`;}return;}
  const el=e.target.closest('[data-action]');if(!el||el.disabled)return;const {action,id,value,format}=el.dataset;
  if(action==='refresh'){refresh();return;}if(action==='system'){openSystem();return;}
  if(action==='menu'){const open=$('sidebar').classList.toggle('open');$('mobile-backdrop').hidden=!open;el.setAttribute('aria-expanded',String(open));syncSidebar();if(open)$('sidebar').querySelector('[aria-current="page"]')?.focus();return;}
  if(action==='close-dialog'){closeDialog();return;}if(action==='catalog-back'){returnToDrill();return;}if(action==='product'){openProduct(id);return;}if(action==='shop'){openShop(id);return;}if(action==='task'){openTask(id);return;}if(action==='event'){openEvent(id);return;}
  if(action==='shop-products'){closeDialog();location.hash='products';setTimeout(()=>{state.shop=id;state.pageNumber=1;loadProducts();},0);return;}
  if(action==='shop-history-next'||action==='shop-history-prev'||action==='shop-history-retry'){loadShopHistory(action.slice('shop-history-'.length));return;}
  if(action==='shop-filter'){state.shopFilter=value;state.pageNumber=1;render();return;}
  if(action==='overview-tab'){state.overviewTab=value;render();return;}
  if(action==='category-products'){location.hash='products';setTimeout(()=>{state.category=value;state.pageNumber=1;loadProducts();},0);return;}
  if(action==='sort-products'){state.sortDirection=state.sortKey===value&&state.sortDirection==='asc'?'desc':'asc';if(value==='observed_at')state.sortDirection='desc';state.sortKey=value;state.pageNumber=1;loadProducts();return;}
  if(action==='product-view'){state.productView=value;persistPreferences();render();return;}
  if(action==='select-product'){if(el.checked)state.selectedProducts.add(id);else state.selectedProducts.delete(id);render();return;}
  if(action==='select-page'){const rows=state.productData?.items||paginate(filteredProducts()).rows;rows.forEach(p=>el.checked?state.selectedProducts.add(p.id):state.selectedProducts.delete(p.id));render();return;}
  if(action==='clear-selection'){state.selectedProducts.clear();render();return;}
  if(action==='compare-products'){openComparison();return;}
  if(action==='save-view'){saveView();return;}
  if(action==='clear-product-filters'){state.query='';state.category='';state.stock='';state.shop='';state.pageNumber=1;loadProducts();return;}
  if(action==='page-number'){state.pageNumber=Number(value);if(state.page==='products')loadProducts();else if(state.page==='events'&&state.uiV2)loadEvents();else render();return;}
  if(action==='prev'||action==='next'){state.pageNumber+=action==='prev'?-1:1;if(state.page==='products')loadProducts();else if(state.page==='events'&&state.uiV2)loadEvents();else render();return;}
  if(action==='task-next'){state.taskCursors.push(state.taskNext);loadTasks();return;}if(action==='task-prev'){state.taskCursors.pop();loadTasks();return;}
  if(action==='job-next'){state.reportJobsCursors.push(state.reportJobsNext);loadJobs();return;}if(action==='job-prev'){state.reportJobsCursors.pop();loadJobs();return;}
  if(action==='export'){exportRows();return;}if(action==='download-report'||action==='download-job'){downloadReport(format,id);return;}
  if(action==='preview-report'&&state.reportArchive?.task_id)window.open(deploymentPath(`/api/v1/reports/jobs/${encodeURIComponent(state.reportArchive.task_id)}/files/html`),'_blank','noopener,noreferrer');
});
function updateQuery(e){if(e.target.dataset.piFilter==='search'&&productDetail&&!e.isComposing){productDetail.skuSearch=e.target.value;productDetail.skuPage=1;drawProductPanel({preserveSearch:true});return;}if(e.target.dataset.wbFilter==='q'&&!e.isComposing){wbState.filters.q=e.target.value;clearTimeout(wbQueryTimer);wbQueryTimer=setTimeout(loadWorkbench,250);return;}if(e.target.dataset.filter==='query'&&!e.isComposing){state.query=e.target.value;state.pageNumber=1;if(state.page==='products'||state.page==='events'&&state.uiV2){clearTimeout(productQueryTimer);const page=state.page;productQueryTimer=setTimeout(()=>{if(state.page!==page)return;if(page==='products')loadProducts();else loadEvents({reset:true});},300);}else render();}}
document.addEventListener('input',updateQuery);
document.addEventListener('compositionend',updateQuery);
document.addEventListener('change',e=>{
  if(e.target.dataset.piFilter&&productDetail){const field=e.target.dataset.piFilter;if(field==='scope'){productDetail.skuScope=e.target.value;productDetail.skuPage=1;}if(field==='history-context'){productDetail.historyContext=e.target.value;productDetail.historyPage=1;}drawProductPanel();document.querySelector(`[data-pi-filter="${field}"]`)?.focus({preventScroll:true});return;}
  if(e.target.dataset.wbFilter){wbState.filters[e.target.dataset.wbFilter]=e.target.value;if(e.target.dataset.wbFilter!=='q')loadWorkbench();return;}
  if(e.target.dataset.wbPanel){if(e.target.checked)wbState.hiddenPanels.delete(e.target.dataset.wbPanel);else wbState.hiddenPanels.add(e.target.dataset.wbPanel);try{localStorage.setItem('shopscope-workbench-panels-v1',JSON.stringify([...wbState.hiddenPanels]));}catch{}render();return;}
  if(e.target.dataset.matchField&&matchingForm){const field=e.target.dataset.matchField;if(field==='left'||field==='right'){matchingForm.note=$('match-note').value;matchingForm[field]=state.productCache.get(e.target.value)||matchingForm.products.find(p=>p.id===e.target.value)||null;drawMatchForm();}else matchingForm[field]=e.target.value;return;}
  if(e.target.dataset.pageSize!==undefined){state.pageSize=Number(e.target.value);state.pageNumber=1;persistPreferences();if(state.page==='products')loadProducts();else if(state.page==='events'&&state.uiV2)loadEvents({reset:true});else render();return;}
  if(e.target.dataset.column){const key=e.target.dataset.column,current=new Set(state.productColumns);if(e.target.checked)current.add(key);else current.delete(key);state.productColumns=validColumns.filter(k=>current.has(k));persistPreferences();render();return;}
  if(e.target.dataset.savedView!==undefined){const view=state.savedViews[Number(e.target.value)];if(e.target.value!==''&&view){for(const key of ['query','category','stock','shop'])state[key]=typeof view.filters[key]==='string'?view.filters[key]:'';if(Array.isArray(view.columns))state.productColumns=validColumns.filter(k=>view.columns.includes(k));if(['title','shop_name','category','price_min','sku_count','stock_status','observed_at'].includes(view.sortKey))state.sortKey=view.sortKey;state.sortDirection=view.sortDirection==='asc'?'asc':'desc';state.pageNumber=1;loadProducts();}return;}
  const key=e.target.dataset.filter;if(!key||key==='query')return;state[key]=e.target.value;state.pageNumber=1;
  if(key==='taskStatus'||key==='taskType'){state.taskCursors=['0'];loadTasks();}else if(state.page==='products')loadProducts();else if(state.page==='events'&&state.uiV2)loadEvents({reset:true});else render();
});
$('window-select').addEventListener('change',e=>{state.hours=Number(e.target.value);state.pageNumber=1;refresh();});
$('global-search').addEventListener('submit',e=>{e.preventDefault();const query=$('global-query').value.trim();if(!query)return;location.hash='products';setTimeout(()=>{state.query=query;state.pageNumber=1;loadProducts();},0);});
$('detail-dialog').addEventListener('close',()=>{detailsController?.abort();matchingForm=null;productDetail=null;shopDetail=null;catalogView=null;++drillRequestVersion;if(detailFocus?.isConnected)detailFocus.focus({preventScroll:true});});
$('detail-dialog').addEventListener('click',e=>{if(e.target===$('detail-dialog')){const rect=e.target.getBoundingClientRect();if(e.clientX<rect.left||e.clientX>rect.right||e.clientY<rect.top||e.clientY>rect.bottom)closeDialog();}});
$('mobile-backdrop').addEventListener('click',closeMenu);
document.addEventListener('keydown',e=>{if(e.key==='/'&&!document.querySelector('dialog[open]')&&!['INPUT','TEXTAREA','SELECT'].includes(document.activeElement.tagName)){e.preventDefault();$('global-query').focus();}if(e.key==='Escape')closeMenu();});
document.addEventListener('keydown',e=>{if(['Enter',' '].includes(e.key)&&e.target instanceof SVGElement&&e.target.matches('[role="button"]')){e.preventDefault();e.target.dispatchEvent(new MouseEvent('click',{bubbles:true}));}});
document.addEventListener('keydown',e=>{if(e.target.matches('.pi-tab')&&['ArrowLeft','ArrowRight','Home','End'].includes(e.key)){e.preventDefault();const tabs=['overview','variants','reviews','history','info'],index=tabs.indexOf(e.target.dataset.piTab);selectProductTab(tabs[e.key==='Home'?0:e.key==='End'?tabs.length-1:(index+(e.key==='ArrowRight'?1:tabs.length-1))%tabs.length]);}});
document.addEventListener('error',e=>{if(e.target instanceof HTMLImageElement&&e.target.closest('.product-photo,.photo-stage,.pi-image,.pi-description-figure')){e.target.hidden=true;const fallback=e.target.parentElement.querySelector('.product-photo-fallback');if(fallback)fallback.hidden=false;}},true);
document.addEventListener('error',e=>{if(e.target instanceof HTMLImageElement&&e.target.closest('.shop-avatar')){e.target.hidden=true;const fallback=e.target.parentElement.querySelector('.shop-logo-fallback');if(fallback)fallback.hidden=false;}},true);
window.addEventListener('hashchange',navigate);
mobileLayout.addEventListener('change',()=>{closeMenu();syncSidebar();});
document.querySelectorAll('[data-icon]').forEach(el=>el.innerHTML=icon(el.dataset.icon));
installScatterInteractions({getData:()=>wbState.data,getShops:()=>[...allShops(),...(wbState.data?.shops||[])],onProduct:id=>openProduct(id)});
installShopPicker({getShops:()=>state.shops.length?state.shops:wbState.data?.shops||[],getValue:()=>wbState.filters.shop_id,onChange:selectWorkbenchShop});
navigate();
(window.__shopScopeBootstrap||request('/api/v2/ui/bootstrap')).then(data=>{
  if(data.bootstrapErrorStatus!==undefined)throw {status:data.bootstrapErrorStatus};
  state.apiV2=data.schema_version===2;state.uiV2=Boolean(data.enabled);
  const initial=data.initial_workbench,filters=wbState.filters;
  if(state.uiV2&&initial?.summary&&!filters.shop_id&&!filters.category&&!filters.q&&
    (filters.currency||'USD').toUpperCase()===initial.scope?.currency&&state.hours===initial.scope?.lookback_hours){
    for(const tab of ['market','attributes','stock','competition','quality'])
      workbenchCache.set(workbenchKey(filters,state.hours,tab),{data:structuredClone(initial),savedAt:Date.now()});
  }
}).catch(error=>{state.apiV2=error.status!==404;state.uiV2=error.status!==404;}).finally(()=>{state.initializing=false;refresh({force:false});});
