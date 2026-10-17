import { formatDate, labels, money, safeUrl } from './api.js';

const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num = value => value == null ? '—' : new Intl.NumberFormat('zh-CN').format(value);
const has = value => value != null && value !== '' && (!Array.isArray(value) || value.length > 0);
const text = value => !has(value) ? '—' : typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value);
const paths = {close:'m6 6 12 12M6 18 18 6',left:'m15 5-7 7 7 7',external:'M14 3h7v7M21 3l-9 9M10 3H3v18h18v-7',shirt:'m8 3-5 3-2 5 4 2 1-3v11h12V10l1 3 4-2-2-5-5-3a4 4 0 0 1-8 0Z',search:'M21 21l-5-5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0',chart:'M3 3v18h18M7 17v-5M12 17V8M17 17V5'};
const icon = name => `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="${paths[name] || paths.chart}"/></svg>`;
const empty = label => `<div class="pi-empty">${icon('chart')}<strong>${esc(label)}</strong></div>`;
const fact = (label,value) => `<div class="pi-fact"><dt>${esc(label)}</dt><dd>${esc(text(value))}</dd></div>`;
const facts = rows => `<dl class="pi-facts">${rows.filter(([,v])=>has(v)).map(([k,v])=>fact(k,v)).join('')}</dl>`;
const chips = values => `<div class="pi-chips">${values.map(v=>`<span class="pi-chip">${esc(v)}</span>`).join('')}</div>`;
const contextName = value => ({public_listed:'公开报价',public:'公开报价',public_price:'公开报价',unknown:'报价口径未知'}[value] || value || '报价口径未知');
const stock = value => `<span class="badge ${{in_stock:'green',out_of_stock:'amber',partial:'blue'}[value]||''}">${esc(labels.stockStatuses[value]||'库存未知')}</span>`;
const link = (url,label,className='pi-link') => safeUrl(url) ? `<a class="${className}" href="${esc(safeUrl(url))}" target="_blank" rel="noopener noreferrer">${esc(label)}${icon('external')}</a>` : '';
const range = (p,original=false) => {
  const low=p[original?'original_price_min':'price_min'],high=p[original?'original_price_max':'price_max'];
  if(low==null)return '—';
  const first=money(low,p.currency);
  return esc(high==null?`${first} 起`:Number(low)===Number(high)?first:`${first}–${money(high,p.currency)}`);
};

export function catalogHeader(back=false,label='商品详情') {
  return `<div class="catalog-dialog-top">${back?`<button class="catalog-back" data-action="catalog-back">${icon('left')}返回结果</button>`:`<span class="catalog-context">${esc(label)}</span>`}<button class="icon-button" data-action="close-dialog" aria-label="关闭详情">${icon('close')}</button></div>`;
}

export function renderCatalogState({title='商品详情',back=false,error=null,retry='product'}={}) {
  return `<div class="product-inspector">${catalogHeader(back,title)}<div class="catalog-heading"><h2 class="catalog-title" id="dialog-title">${esc(title)}</h2></div><div class="pi-panel">${error?`<div class="pi-error"><span>${esc(error.message||'暂时无法读取')}</span><button class="button" data-pi-retry="${esc(retry)}">重试</button></div>`:'<div class="pi-empty" role="status"><span class="loader"></span><span>加载中</span></div>'}</div></div>`;
}

function resourceState(model,key,label) {
  if(model.loading?.[key]&&!model[key])return `<div class="pi-empty" role="status">${esc(label)}加载中</div>`;
  if(model.errors[key]&&!model[key])return `<div class="pi-error"><span>${esc(label)}暂不可用</span><button class="button" data-pi-retry="${key}">重试</button></div>`;
  return '';
}

function currentVariants(model) { return (model.variants?.items||[]).filter(v=>v.seen_in_latest_detail===true); }
export function skuCount(model) {
  if(!model.variants)return '—';
  const count=currentVariants(model).length;
  const uncertain=model.variants.items.some(v=>v.seen_in_latest_detail!==true&&v.seen_in_latest_detail!==false)||!model.variants.complete;
  if(!count&&(uncertain||!model.product.last_success_at))return '—';
  return `${num(count)}${uncertain?'+':''}`;
}
function photoGallery(model) {
  const gallery=(model.images?.items||[]).filter(p=>safeUrl(p.image_url));
  if(!gallery.length)return `<div class="pi-no-image">${icon('shirt')}<span>${model.errors.images?'图片读取失败':'暂无图片'}</span>${model.errors.images?'<button class="pi-link" data-pi-retry="images">重试</button>':''}</div>`;
  return `<a class="pi-image" id="pi-full-image" href="${esc(safeUrl(gallery[0].image_url))}" target="_blank" rel="noopener noreferrer" aria-label="查看商品原图"><img id="gallery-main-photo" src="${esc(safeUrl(gallery[0].image_url))}" alt="${esc(model.product.title||'商品图片')}" referrerpolicy="no-referrer"><span class="product-photo-fallback" hidden>图片暂不可用</span></a><div class="pi-thumbs" aria-label="商品图片">${gallery.map((p,i)=>`<button class="pi-thumb${i===0?' active':''}" data-photo-url="${esc(safeUrl(p.image_url))}" aria-pressed="${i===0}" aria-label="查看商品图 ${i+1}" data-photo-index="${i+1}"><img src="${esc(safeUrl(p.thumbnail_url)||safeUrl(p.image_url))}" alt="" loading="lazy" referrerpolicy="no-referrer"></button>`).join('')}</div><div class="pi-meta"><span id="pi-photo-index">1 / ${gallery.length}</span>${link(gallery[0].image_url,'原图','pi-link pi-original-link')}</div>`;
}

export function renderProductInspector(model) {
  const p=model.product;
  const tabs=[['overview','概览',''],['variants','规格',model.variants?`${num(model.variants.items.length)}${model.variants.complete?'':'+'}`:''],['reviews','评论',model.reviews?num(model.reviews.collected_count):''],['history','走势',model.snapshots?`${num(model.snapshots.items.length)}${model.snapshots.complete?'':'+'}`:''],['info','资料','']];
  return `<div class="product-inspector">${catalogHeader(model.back)}<div class="pi-hero"><div class="pi-media">${photoGallery(model)}</div><div class="pi-identity"><h2 id="dialog-title" class="pi-title" title="${esc(p.title)}">${esc(p.title||p.platform_product_id)}</h2><button class="pi-title-toggle" data-pi-tab="info">完整名称与资料 ${icon('external')}</button><div class="pi-subline"><strong>${esc(p.shop_name||'店铺未知')}</strong>${link(p.product_url,'商品页面')}</div><div class="pi-metrics"><div class="pi-metric"><small>${esc(contextName(p.pricing_context))} · ${esc(p.currency||'币种未知')}</small><strong class="pi-price">${range(p)}</strong>${p.original_price_min!=null?`<small>原价 ${range(p,true)}</small>`:''}</div><div class="pi-metric"><small>库存</small><strong><span class="pi-status" data-state="${esc(p.stock_status||'unknown')}">${esc(labels.stockStatuses[p.stock_status]||'库存未知')}</span></strong></div><div class="pi-metric"><small>评分</small><strong>${p.rating==null?'—':esc(Number(p.rating).toFixed(1))}<small> / 5</small></strong>${p.review_count!=null?`<small>${num(p.review_count)} 条评价</small>`:''}</div><div class="pi-metric"><small>当前 SKU</small><strong id="pi-sku-count">${skuCount(model)}</strong></div></div></div></div><div class="pi-tabs" role="tablist" aria-label="商品详情">${tabs.map(([key,label,count])=>`<button id="pi-tab-${key}" class="pi-tab${model.tab===key?' active':''}" data-pi-tab="${key}" role="tab" aria-selected="${model.tab===key}" aria-controls="pi-panel" tabindex="${model.tab===key?'0':'-1'}">${label}<small data-pi-count="${key}">${count}</small></button>`).join('')}</div><div class="pi-panel" id="pi-panel" role="tabpanel" aria-labelledby="pi-tab-${model.tab}" tabindex="0">${renderProductPanel(model)}</div></div>`;
}

const attributeFields = p => [['品牌',p.brand],['面料',p.material],['性别',p.gender],['季节',p.season],['风格',p.style],['版型',p.fit],['衣长',p.garment_length],['图案',p.pattern]];
function overview(model) {
  const p=model.product,attributes=attributeFields(p).filter(([,v])=>has(v));
  const colors=Array.isArray(p.colors)?p.colors:[],sizes=Array.isArray(p.sizes)?p.sizes:[];
  return `<div class="pi-overview-grid"><section class="pi-section"><h3>商品属性</h3>${attributes.length?facts(attributes):'<span class="muted">暂无属性</span>'}</section><section class="pi-section">${colors.length?`<h3>颜色 <small>${colors.length}</small></h3>${chips(colors)}`:''}${sizes.length?`<h3 class="pi-spaced-heading">尺码 <small>${sizes.length}</small></h3>${chips(sizes)}`:''}${!colors.length&&!sizes.length?'<h3>规格</h3><span class="muted">暂无颜色与尺码</span>':''}<button class="pi-link pi-spaced-heading" data-pi-tab="variants">查看规格与库存 →</button></section></div><section class="pi-section pi-overview-footer">${facts([['类目',p.original_category?.split(/\s*[›>]\s*/).at(-1)||p.normalized_category],['最近观察',p.last_seen_at?formatDate(p.last_seen_at):null],['上架状态',p.availability_status&&p.availability_status!=='unknown'?labels.availabilityStatuses[p.availability_status]||p.availability_status:null],['公开销量',p.sales_count_text|| (p.sales_count!=null?num(p.sales_count):null)]])}</section>`;
}

function optionsHtml(options) {
  if(!has(options)||typeof options!=='object')return '';
  return facts(Object.entries(options).map(([key,value])=>[key,text(value)]));
}
function lifecycle(v) { return v.seen_in_latest_detail===true?'当前规格':v.seen_in_latest_detail===false?'历史保留':'未确认'; }
function pager(page,pages,count,kind) {
  return `<div class="pi-pagination"><span>${num(count)} 条${pages>1?` · ${page} / ${pages} 页`:''}</span>${pages>1?`<div><button class="button" data-pi-page="${kind}" data-step="-1" ${page<=1?'disabled':''}>上一页</button><button class="button" data-pi-page="${kind}" data-step="1" ${page>=pages?'disabled':''}>下一页</button></div>`:''}</div>`;
}
function loadMore(model,key) {
  if(model[key]?.complete!==false)return '';
  return `<div class="pi-pagination"><span>已加载 ${num(model[key].items.length)} 条${model.errors[key]?' · 续载失败':''}</span><button class="button" data-pi-more="${key}" ${model.loading?.[key]?'disabled':''}>${model.loading?.[key]?'加载中':'继续加载'}</button></div>`;
}
function variants(model) {
  const unavailable=resourceState(model,'variants','规格');if(unavailable)return unavailable;
  const rows=model.variants?.items||[],q=model.skuSearch.trim().toLocaleLowerCase();
  const filtered=rows.filter(v=>(model.skuScope==='all'||model.skuScope===(v.seen_in_latest_detail===true?'current':v.seen_in_latest_detail===false?'historical':'unknown'))&&(!q||[v.platform_variant_id,v.color,v.size,v.material,JSON.stringify(v.options||{})].some(x=>String(x||'').toLocaleLowerCase().includes(q))));
  const pageSize=8,pages=Math.max(1,Math.ceil(filtered.length/pageSize));model.skuPage=Math.max(1,Math.min(model.skuPage,pages));
  const visible=filtered.slice((model.skuPage-1)*pageSize,model.skuPage*pageSize);
  return `<div class="pi-toolbar"><label class="pi-search">${icon('search')}<input id="pi-sku-search" data-pi-filter="search" value="${esc(model.skuSearch)}" placeholder="搜索颜色、尺码、SKU" aria-label="搜索规格"></label><select class="pi-select" data-pi-filter="scope" aria-label="规格范围">${[['all','全部规格'],['current','当前规格'],['historical','历史保留'],['unknown','未确认']].map(([k,v])=>`<option value="${k}" ${model.skuScope===k?'selected':''}>${v}</option>`).join('')}</select></div>${visible.length?`<div class="pi-table-wrap"><table class="pi-table"><thead><tr><th>颜色 / SKU</th><th>尺码</th><th>公开报价</th><th>库存</th><th>数量</th></tr></thead><tbody>${visible.map(v=>`<tr><td><details class="pi-sku-details"><summary>${esc(v.color||'颜色未知')}</summary><div class="pi-sku-extra">${facts([['SKU',v.platform_variant_id],['范围',lifecycle(v)],['面料',v.material],['最近更新',v.updated_at?formatDate(v.updated_at):null]])}${optionsHtml(v.options)}</div></details>${v.seen_in_latest_detail!==true?`<span class="cell-secondary">${lifecycle(v)}</span>`:''}</td><td>${esc(v.size||'—')}</td><td class="cell-price">${esc(money(v.price,v.currency))}${v.original_price!=null?`<span class="cell-secondary">原价 ${esc(money(v.original_price,v.currency))}</span>`:''}</td><td>${stock(v.stock_status)}</td><td>${num(v.available_quantity)}</td></tr>`).join('')}</tbody></table></div>${pager(model.skuPage,pages,filtered.length,'variants')}`:empty(rows.length?'没有匹配规格':'暂无规格记录')}${loadMore(model,'variants')}`;
}

function timestamp(value) {
  if(typeof value!=='string'||!value.trim())return NaN;
  let normalized=value.trim().replace(' ','T');
  if(!/(?:Z|[+-]\d{2}:?\d{2})$/i.test(normalized))normalized+='Z';
  return Date.parse(normalized);
}
function historyChart(rows,currency) {
  const known=rows.filter(r=>r.price_min!=null&&Number.isFinite(Number(r.price_min))&&Number.isFinite(timestamp(r.captured_at))).sort((a,b)=>timestamp(a.captured_at)-timestamp(b.captured_at));
  if(!known.length)return empty('暂无已知价格');
  const values=known.map(r=>Number(r.price_min)),min=Math.min(...values),max=Math.max(...values),pad=Math.max((max-min)*.2,max*.02,.5),low=Math.max(0,min-pad),high=max+pad;
  const first=timestamp(known[0].captured_at),last=timestamp(known.at(-1).captured_at);
  const points=values.map((v,i)=>[first===last?348:76+(timestamp(known[i].captured_at)-first)/(last-first)*544,132-(v-low)/(high-low)*108]);
  const ticks=[low,(low+high)/2,high];
  return `<div class="pi-chart"><svg viewBox="0 0 640 155" role="img" aria-label="${esc(`${known.length} 次公开起价观察，${money(min,currency)} 至 ${money(max,currency)}`)}">${ticks.map((v,i)=>`<line x1="76" y1="${132-i*54}" x2="620" y2="${132-i*54}" stroke="#e9eef6" stroke-dasharray="3 4"/><text x="65" y="${136-i*54}" text-anchor="end" fill="#8b9ab0" font-size="10">${esc(money(v,currency))}</text>`).join('')}<polyline points="${points.map(p=>p.join(',')).join(' ')}" fill="none" stroke="#4779ed" stroke-width="2"/>${points.map(([x,y],i)=>`<circle cx="${x}" cy="${y}" r="4" fill="#4779ed" stroke="#fff" stroke-width="2"><title>${esc(formatDate(known[i].captured_at))} · ${esc(money(values[i],currency))}</title></circle>`).join('')}</svg><div class="pi-chart-range"><span>${esc(formatDate(known[0].captured_at))}</span><span>${esc(formatDate(known.at(-1).captured_at))}</span></div></div>`;
}
function history(model) {
  const unavailable=resourceState(model,'snapshots','历史记录');if(unavailable)return unavailable;
  const rows=model.snapshots?.items||[];
  if(!rows.length)return empty('暂无历史记录');
  const key=r=>JSON.stringify([r.currency||'',r.pricing_context||'']);
  const groups=new Map();for(const row of rows){const k=key(row);if(!groups.has(k))groups.set(k,[]);groups.get(k).push(row);}
  if(!groups.has(model.historyContext))model.historyContext=groups.has(key(model.product))?key(model.product):groups.keys().next().value;
  const selected=groups.get(model.historyContext).slice().sort((a,b)=>timestamp(b.captured_at)-timestamp(a.captured_at));
  const pages=Math.max(1,Math.ceil(selected.length/6));model.historyPage=Math.max(1,Math.min(model.historyPage,pages));
  return `<section class="pi-section"><div class="pi-toolbar"><h3>公开起价</h3>${groups.size>1?`<select class="pi-select" data-pi-filter="history-context" aria-label="历史报价口径">${[...groups].map(([k,v])=>`<option value="${esc(k)}" ${k===model.historyContext?'selected':''}>${esc(v[0].currency||'币种未知')} · ${esc(contextName(v[0].pricing_context))}</option>`).join('')}</select>`:`<span class="muted">${esc(selected[0].currency||'币种未知')} · ${esc(contextName(selected[0].pricing_context))}</span>`}</div>${historyChart(selected,selected[0].currency)}${new Set(selected.map(r=>r.captured_at)).size<2?'<div class="pi-note">单次观察</div>':''}</section><div class="pi-table-wrap"><table class="pi-table"><thead><tr><th>观察时间</th><th>公开报价</th><th>库存</th><th>SKU</th></tr></thead><tbody>${selected.slice((model.historyPage-1)*6,model.historyPage*6).map(s=>`<tr><td><details class="pi-sku-details"><summary>${esc(formatDate(s.captured_at))}</summary><div class="pi-sku-extra">${facts([['快照 ID',s.id],['上架状态',labels.availabilityStatuses[s.availability_status]||s.availability_status],['来源状态',s.source_status],['解析版本',s.parser_version],['折扣',s.discount_rate==null?null:`${num(Number(s.discount_rate)*100)}%`]])}${link(s.source_url,'查看来源')}</div></details></td><td class="cell-price">${range(s)}${s.original_price_min!=null?`<span class="cell-secondary">原价 ${range(s,true)}</span>`:''}</td><td>${stock(s.stock_status)}</td><td>${num(s.variant_count)}</td></tr>`).join('')}</tbody></table></div>${pager(model.historyPage,pages,selected.length,'history')}${loadMore(model,'snapshots')}`;
}

// Description content is data, never source HTML. Only these fixed components
// and public image links are allowed to reach the DOM.
function descriptionImageUrl(value) {
  if(typeof value!=='string'||value.length>8192||/[\s\x00-\x1f\x7f]/.test(value))return null;
  const safe=safeUrl(value);if(!safe)return null;
  const url=new URL(safe),host=url.hostname.toLowerCase().replace(/\.$/,'');
  if(url.username||url.password||url.hash||!['','80','443'].includes(url.port)||!url.pathname||!host.includes('.')||!/^[-a-z0-9.]+$/.test(host)||host==='localhost'||/\.(local|localhost|internal)$/.test(host))return null;
  if(/^\d+\.\d+\.\d+\.\d+$/.test(host)){
    const [a,b,c]=host.split('.').map(Number);
    if(a===0||a===10||a===127||a>=224||(a===100&&b>=64&&b<=127)||(a===169&&b===254)||(a===172&&b>=16&&b<=31)||(a===192&&(b===168||b===0||b===2))||(a===198&&(b===18||b===19||b===51&&c===100))||(a===203&&b===0&&c===113))return null;
  }
  return safe;
}
const contentString=value=>typeof value==='string'?value:'';
const contentSpan=value=>Number.isInteger(value)&&value>=1&&value<=100?value:1;
function descriptionNode(node) {
  if(!node||typeof node!=='object')return '';
  if(node.type==='paragraph')return `<p class="pi-description">${esc(contentString(node.text))}</p>`;
  if(node.type==='heading'){
    const tag=Number.isInteger(node.level)&&node.level>=1&&node.level<=6?['h4','h4','h4','h5','h6','h6'][node.level-1]:'h4';
    return `<${tag} class="pi-description-heading">${esc(contentString(node.text))}</${tag}>`;
  }
  if(node.type==='list'){
    const items=Array.isArray(node.items)?node.items.filter(item=>typeof item==='string'):[],tag=node.ordered===true?'ol':'ul';
    return items.length?`<${tag} class="pi-description-list">${items.map(item=>`<li>${esc(item)}</li>`).join('')}</${tag}>`:'';
  }
  if(node.type==='table'){
    const rows=Array.isArray(node.rows)?node.rows.filter(Array.isArray):[];
    if(!rows.length)return '';
    return `<div class="pi-content-table-wrap" role="region" aria-label="商品介绍表格，可横向滚动" tabindex="0"><table class="pi-content-table"><tbody>${rows.map(row=>`<tr>${row.filter(cell=>cell&&typeof cell==='object').map(cell=>{
      const tag=cell.header===true?'th':'td';
      return `<${tag} rowspan="${contentSpan(cell.rowspan)}" colspan="${contentSpan(cell.colspan)}">${esc(contentString(cell.text))}</${tag}>`;
    }).join('')}</tr>`).join('')}</tbody></table></div>`;
  }
  if(node.type==='image'){
    const url=node.role==='description'?descriptionImageUrl(node.url):null;if(!url)return '';
    const dimension=key=>Number.isInteger(node[key])&&node[key]>0&&node[key]<=100000?` ${key}="${node[key]}"`:'';
    return `<figure class="pi-description-figure"><a href="${esc(url)}" target="_blank" rel="noopener noreferrer" aria-label="查看商品介绍原图"><img src="${esc(url)}" alt="${esc(contentString(node.alt)||'商品介绍图片')}" loading="lazy" decoding="async" referrerpolicy="no-referrer"${dimension('width')}${dimension('height')}><span class="product-photo-fallback" hidden>详情图暂不可用</span></a>${node.alt?`<figcaption>${esc(contentString(node.alt))}</figcaption>`:''}</figure>`;
  }
  return '';
}
function productContent(model) {
  const response=model.content,content=response?.content;
  const fallback=model.product.description?`<section class="pi-section"><h3>历史介绍文本</h3><p class="pi-description">${esc(model.product.description)}</p><div class="pi-note">仅保留旧版采集文本，不代表已取得完整介绍、参数与详情图。</div></section>`:'';
  if(!response){
    const state=resourceState(model,'content','商品介绍');
    return `${state||'<div class="pi-note" role="status">商品介绍尚未读取。</div>'}${fallback}`;
  }
  if(!response.readiness)return `<section class="pi-section"><h3>商品介绍</h3><div class="pi-note" role="status">介绍采集尚未就绪。</div></section>${fallback}`;
  if(!response.available||!content)return `<section class="pi-section"><h3>商品介绍</h3><div class="pi-note" role="status">尚未采集结构化介绍、详细参数与详情图。</div></section>${fallback}`;
  const specifications=(Array.isArray(content.sections?.specifications)?content.sections.specifications:[]).filter(row=>row&&typeof row.name==='string'&&typeof row.value==='string');
  const nodes=Array.isArray(content.sections?.description)?content.sections.description:[],body=nodes.map(descriptionNode).join('');
  const partial=content.completeness==='partial'||(Array.isArray(content.unsupported_blocks)&&content.unsupported_blocks.length>0);
  const status=partial?'部分内容':({complete:'已采集',empty:'来源未提供内容',missing:'来源未提供内容'}[content.completeness]||'覆盖情况未确认');
  const description=body||(content.description_text?`<p class="pi-description">${esc(contentString(content.description_text))}</p>`:'<div class="pi-note">本次来源未提供可展示的介绍正文或详情图。</div>');
  return `${specifications.length?`<section class="pi-section"><h3>详细参数 <small>${num(specifications.length)}</small></h3><dl class="pi-facts pi-content-specifications">${specifications.map(row=>fact(row.name,row.value)).join('')}</dl></section>`:''}<section class="pi-section"><h3>商品介绍 <small>${esc(status)}</small></h3>${partial?'<div class="pi-content-status" role="status">当前展示已获取内容，部分来源内容未提供或暂不支持解析。</div>':''}${!specifications.length?'<div class="pi-note pi-content-no-specs">本次来源未提供详细参数。</div>':''}<div class="pi-description-body">${description}</div>${content.observed_at?`<div class="pi-note">介绍采集于 ${esc(formatDate(content.observed_at))}</div>`:''}</section>${!body&&!content.description_text?fallback:''}`;
}

function info(model) {
  const p=model.product,missing=attributeFields(p).filter(([,v])=>!has(v)).map(([k])=>k);
  return `<section class="pi-section"><h3>完整名称</h3><p class="pi-description">${esc(p.title||p.platform_product_id)}</p></section><section class="pi-section">${facts([['商品内部 ID',p.id],['平台商品 ID',p.platform_product_id],['店铺',p.shop_name],['公开类目',p.original_category],['标准类目',p.normalized_category],...attributeFields(p),['首次观察',p.first_seen_at?formatDate(p.first_seen_at):null],['最近观察',p.last_seen_at?formatDate(p.last_seen_at):null],['详情采集',p.last_success_at?formatDate(p.last_success_at):'尚未采集'],['上架状态',labels.availabilityStatuses[p.availability_status]||p.availability_status],['公开销量',p.sales_count_text|| (p.sales_count!=null?num(p.sales_count):null)],['折扣',p.discount_rate==null?null:`${num(Number(p.discount_rate)*100)}%`]])}${missing.length?`<div class="pi-note">未采集：${esc(missing.join('、'))}</div>`:''}</section>${productContent(model)}<details class="pi-disclosure"><summary>采集记录</summary>${facts([['数据版本',p.data_version],['解析版本',p.parser_version],['内容标识',p.content_hash],['图片',model.images?.message|| (model.errors.images?'读取失败':null)],['介绍版本',model.content?.content?.source_version],['属性冲突',Object.keys(p.attribute_conflicts||{}).length?p.attribute_conflicts:null],['保留属性',p.retained_attributes?.length?p.retained_attributes:null]])}</details>`;
}

function reviews(model) {
  const unavailable=resourceState(model,'reviews','评论');if(unavailable)return unavailable;
  const data=model.reviews;if(!data)return empty('尚未读取评论');
  const pending=data.latest_task?.status==='running'?'正在采集':data.latest_task?.status==='retry_waiting'?'等待重试':'等待采集';
  const status={pending_schema:'评论采集尚未启用',not_captured:'尚未采集评论',pending:`评论${pending}`,partial:'已获取部分评论',partial_pending:`已获取部分评论，后续${pending}`,partial_failed:'已获取部分评论，后续采集暂未成功',failed:'评论暂未采集成功',complete:'本轮采集已完成'}[data.status]||'评论状态未确认';
  const cards=(data.items||[]).map(row=>`<article class="pi-review"><header class="pi-review-heading"><strong>${esc(row.reviewer_name||'匿名买家')}</strong><span class="pi-review-rating" aria-label="${row.rating==null?'评分未提供':`${esc(row.rating)} 分`}">${row.rating==null?'—':`${esc(row.rating)} / 5`}</span></header><div class="pi-review-meta">${[row.reviewed_at?formatDate(row.reviewed_at):'日期未提供',row.country,row.verified_purchase===true?'已验证购买':null,row.incentivized===true?'激励评论':null].filter(Boolean).map(esc).join(' · ')}</div>${row.sku_specification?`<div class="pi-review-meta">购买规格：${esc(row.sku_specification)}</div>`:''}<p class="pi-review-text">${esc(row.text||'该评价未附文字')}</p>${Array.isArray(row.images)&&row.images.length?`<div class="pi-review-images">${row.images.filter(safeUrl).map((url,i)=>`<a href="${esc(safeUrl(url))}" target="_blank" rel="noopener noreferrer"><img src="${esc(safeUrl(url))}" alt="买家图片 ${i+1}" loading="lazy" referrerpolicy="no-referrer"></a>`).join('')}</div>`:''}</article>`).join('');
  return `<section class="pi-section"><div class="pi-review-summary"><div><h3>买家评论 <small>已采集 ${esc(data.collected_count||'0')} 条</small></h3><p class="pi-note" role="status">${esc(status)}${data.source_total!=null?` · 来源显示 ${esc(data.source_total)} 条全球评价`:''}</p></div><button class="button" data-pi-retry="reviews" ${model.loading?.reviews?'disabled':''}>刷新</button></div>${cards||empty(data.status==='complete'?'来源暂无评论':'暂无已采集评论')}${loadMore(model,'reviews')}</section>`;
}

export function renderProductPanel(model) {
  return ({overview,variants,reviews,history,info}[model.tab]||overview)(model);
}
