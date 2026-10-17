const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const action = (kind,label,extra='') => `<button type="button" class="button" data-collection-action="${kind}" ${extra}>${label}</button>`;
export const launchTaskTypes = Object.freeze({shop_discovery:'导入店铺链接',product_discovery:'发现店铺商品',product_detail:'更新商品详情',product_reviews:'采集商品评论'});
const planTypes = {shop_profile:'店铺档案',product_snapshot:'商品快照'};
export const collectionEligibility = Object.freeze({collector_not_enabled:'采集器未启用',invalid_source_url:'缺少有效来源链接',detail_not_collected:'请先采集商品详情',unsupported_currency:'暂不支持此币种',unsupported_pricing_context:'暂不支持此价格场景',invalid_resource_identity:'资源身份不完整或无效',resource_missing:'资源不存在或不属于当前市场'});
const isShop = type => ['shop_profile','product_discovery'].includes(type);
const typeOptions = (types,current,enabled) => Object.entries(types).map(([key,label])=>`<option value="${key}" ${key===current?'selected':''} ${enabled.includes(key)?'':'disabled'}>${label}${enabled.includes(key)?'':'（未启用）'}</option>`).join('');
const formEnd = label => `<div id="collection-form-error" class="notice warning" role="alert" hidden></div><div class="collection-form-actions">${action('close','暂存并关闭')}<button type="submit" class="button primary">${label}</button></div>`;

function resourcePicker(type,ids,maximum) {
  return `<div id="collection-picker"><label id="collection-resource-label" for="collection-resource-query">选择${isShop(type)?'店铺':'商品'}</label><input id="collection-resource-query" type="search" autocomplete="off" placeholder="搜索名称、平台 ID 或内部 ID" aria-controls="collection-resource-results"><p class="collection-form-hint">最多选择 ${maximum} 个，搜索和翻页会保留已选范围。</p><div id="collection-resource-selected" aria-live="polite"></div><div id="collection-resource-results" aria-live="polite"></div><details class="collection-advanced"><summary>高级：按内部 ID 选择</summary><label for="collection-resources">内部资源 ID</label><textarea id="collection-resources" rows="3" placeholder="使用逗号或换行分隔">${esc(ids.join(', '))}</textarea><p class="collection-form-hint">这里填写系统内部 ID。点击替换后核对资源名称。</p>${action('resource-replace','替换已选范围')}</details></div>`;
}

export function renderCollectionPlanEditor(plan,control,draft=plan) {
  const edit=Boolean(plan?.id),enabled=control?.enabled_task_types||[];
  const type=draft?.task_type||Object.keys(planTypes).find(kind=>enabled.includes(kind))||'shop_profile';
  const mode=draft?.schedule_mode||'once';
  const minimum=control?.limits?.min_interval_seconds?.[type]||(type==='shop_profile'?3600:60);
  return `<h2 id="dialog-title">${edit?'编辑采集计划':'新建采集计划'}</h2><p class="detail-subtitle">选择采集范围与执行安排。草稿仅保留在当前页面，刷新后清除。</p><form id="collection-plan-form" class="collection-form"><label for="collection-name">计划名称</label><input id="collection-name" name="name" maxlength="80" required value="${esc(draft?.name||'')}" placeholder="例如：重点店铺每小时档案更新"><div class="collection-form-grid"><div><label for="collection-type">采集类型</label><select id="collection-type" name="task_type" ${edit?'disabled':''}>${typeOptions(planTypes,type,enabled)}</select></div><div><label for="collection-schedule">执行方式</label><select id="collection-schedule" name="schedule_mode" ${edit?'disabled':''}><option value="once" ${mode==='once'?'selected':''}>执行一次</option><option value="interval" ${mode==='interval'?'selected':''}>按间隔执行</option></select></div></div>${resourcePicker(type,draft?.resource_ids||[],control?.limits?.max_resources||100)}<div id="collection-interval-field" ${mode==='interval'?'':'hidden'}><label for="collection-interval">执行间隔（分钟）</label><input id="collection-interval" name="interval_minutes" type="number" ${mode==='interval'?'':'disabled'} min="${minimum/60}" max="${(control?.limits?.max_interval_seconds||604800)/60}" step="1" value="${(draft?.interval_seconds||minimum)/60}"><p id="collection-interval-hint" class="collection-form-hint">最短 ${minimum/60} 分钟，最长 7 天。上一批仍在执行时不重复派发。</p></div><label class="collection-check"><input id="collection-enabled" type="checkbox" ${draft?.enabled===false?'':'checked'}>启用自动调度</label><p id="collection-schedule-hint" class="collection-form-hint">${mode==='once'?'启用后自动派发一次并停用；停用时仅保存计划。':'启用后按间隔自动运行；更改名称和范围不会推迟既定执行时间。'}</p>${formEnd('保存计划')}</form>`;
}

export function renderCollectionLaunchDependencies(taskType,control) {
  const dependencies={shop_discovery:['product_discovery','product_detail','shop_profile'],product_discovery:['product_detail']};
  const names={product_discovery:'商品发现',product_detail:'商品详情',shop_profile:'店铺档案'};
  if(!Array.isArray(control?.enabled_task_types))return '';
  const missing=(dependencies[taskType]||[]).filter(type=>!control.enabled_task_types.includes(type));
  if(!missing.length)return '';
  return `<div class="notice warning" role="status">后续${missing.map(type=>names[type]).join('、')}未启用，相关任务将等待服务器启用。仍可提交本次采集；请在批次详情中跟进后续任务状态。</div>`;
}

export function renderCollectionLaunchForm(control,draft={}) {
  const enabled=(control?.launch_task_types||[]).filter(type=>!control?.enabled_task_types||control.enabled_task_types.includes(type));
  const type=draft.task_type||Object.keys(launchTaskTypes).find(kind=>enabled.includes(kind))||'shop_discovery';
  const budget=['product_discovery','product_reviews'].includes(type),maximum=control?.limits?.[type==='product_reviews'?'max_review_pages':'max_discovery_pages']||1;
  return `<h2 id="dialog-title">开始单次采集</h2><p class="detail-subtitle">提交后进入队列，由采集进程执行。可在本次采集中查看直接任务及新产生的后续任务。</p><form id="collection-launch-form" class="collection-form"><label for="collection-name">采集名称</label><input id="collection-name" name="name" maxlength="80" required value="${esc(draft.name||'')}" placeholder="例如：今日新店商品入库"><label for="collection-type">采集类型</label><select id="collection-type">${typeOptions(launchTaskTypes,type,enabled)}</select><div id="collection-seeds" ${type==='shop_discovery'?'':'hidden'}><label for="collection-shop-urls">美国站店铺链接</label><textarea id="collection-shop-urls" rows="5" placeholder="每行一个公开店铺 HTTPS 链接">${esc((draft.shop_urls||[]).join('\n'))}</textarea><p class="collection-form-hint">支持 shop.tiktok.com/us/store/…/店铺ID 与 www.tiktok.com/shop/store/…/店铺ID。最多 ${control?.limits?.max_resources||100} 个；已存在的导入任务会复用。</p></div><div id="collection-launch-picker" ${type==='shop_discovery'?'hidden':''}>${resourcePicker(type,draft.resource_ids||[],control?.limits?.max_resources||100)}</div><div id="collection-budget-field" ${budget?'':'hidden'}><label for="collection-max-pages">本次最多采集页数</label><input id="collection-max-pages" type="number" min="1" max="${maximum}" step="1" value="${draft.max_pages||maximum}" ${budget?'':'disabled'}><p id="collection-budget-hint" class="collection-form-hint">每个资源最多 ${maximum} 页，任务遇到末页时会提前完成。</p></div><div id="collection-launch-dependencies" aria-live="polite">${renderCollectionLaunchDependencies(type,control)}</div>${formEnd('提交采集任务')}</form>`;
}

export function renderCollectionResourceSelected(state) {
  const selected=[...state.selected.values()];
  return `<div class="collection-picker-heading"><strong>已选 ${selected.length} / ${state.maximum}</strong>${state.resolving?'<span>正在核对资源…</span>':''}</div>${state.selectionError?`<p class="collection-selection-error" role="alert">${esc(state.selectionError)} ${action('resource-resolve','重新核对')}</p>`:''}${selected.length?`<div class="collection-selected-list">${selected.map(row=>`<div class="collection-selected-item"><div><strong>${esc(row.name||`内部 ID ${row.id}`)}</strong><small>${row.platform_id?`平台 ${esc(row.platform_id)} · `:''}内部 ${esc(row.id)}${row.eligible===false?` · ${esc(collectionEligibility[row.eligibility_reason]||'当前不可采集')}`:row.eligible===null?' · 待核对':''}</small></div>${action('resource-remove','移除',`data-id="${esc(row.id)}" aria-label="移除 ${esc(row.name||row.id)}"`)}</div>`).join('')}</div>`:'<p class="collection-form-hint">尚未选择资源，请在下方勾选。</p>'}`;
}

export function renderCollectionResourceResults(state) {
  const disabled=state.loading||Boolean(state.error);
  return `${state.error?`<div class="notice warning" role="alert">${esc(state.error)} ${action('resource-refresh','重试')}</div>`:''}<div class="collection-candidates" aria-busy="${state.loading}">${state.items.length?state.items.map(row=>`<label class="collection-candidate${row.eligible?'':' unavailable'}"><input type="checkbox" data-collection-resource="${esc(row.id)}" ${state.selected.has(String(row.id))?'checked':''} ${!row.eligible||state.loading?'disabled':''}><span><strong>${esc(row.name||'未命名资源')}</strong><small>${row.shop_name?`${esc(row.shop_name)} · `:''}平台 ${esc(row.platform_id||'—')} · 内部 ${esc(row.id)}</small>${row.eligible?'':`<em>${esc(collectionEligibility[row.eligibility_reason]||'当前不可采集')}</em>`}</span></label>`).join(''):`<p class="collection-picker-empty">${state.loading?'正在查找资源…':state.loaded?'没有符合条件的资源，可更改搜索词。':'正在准备资源列表…'}</p>`}</div><div class="collection-picker-pager">${action('resource-prev','上一页',state.cursors.length===1||disabled?'disabled':'')}<span>${state.loading?'读取中…':`第 ${state.cursors.length} 页`}</span>${action('resource-next','下一页',!state.next||disabled?'disabled':'')}</div>`;
}

export function parseCollectionShopUrls(value,maximum=100) {
  const input=String(value||'').trim().split(/[\s,，]+/).filter(Boolean);
  if(!input.length)throw Error('请填写至少一个美国站店铺链接。');
  const urls=input.map(raw=>{
    let url;try{url=new URL(raw);}catch{throw Error('店铺链接格式不正确，请填写完整 HTTPS 链接。');}
    const route=url.hostname==='shop.tiktok.com'?/^\/us\/store\/[^/]+\/\d+\/?$/:url.hostname==='www.tiktok.com'?/^\/shop\/store\/[^/]+\/\d+\/?$/:null;
    if(url.protocol!=='https:'||url.username||url.password||url.port||!route?.test(url.pathname))throw Error('仅支持已知美国站公开店铺 HTTPS 链接。');
    return `${url.origin}${url.pathname}`;
  });
  const unique=[...new Set(urls)];if(unique.length>maximum)throw Error(`一次最多导入 ${maximum} 个店铺。`);
  return unique;
}
