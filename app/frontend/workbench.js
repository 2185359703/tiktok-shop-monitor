import { money, formatDate, labels, safeUrl } from './api.js';
import { renderScatter as scatter } from './scatter-chart.js';
import { renderShopPicker } from './shop-picker.js';

const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num=v=>v==null?'—':new Intl.NumberFormat('zh-CN').format(v);
const percent=v=>v==null?'—':`${Number(v).toFixed(1)}%`;
const palette=['#3765ef','#168473','#8ea5ec','#dcaa63','#a7b8d0','#a385bb'];
const stockNames=labels.stockStatuses;
export const workbenchTabs={market:'店铺与价格',attributes:'服装结构',stock:'库存与折扣',competition:'竞品关系',quality:'数据质量'};
const panelNames={price:'价格分布',shops:'店铺价格带',categories:'类目结构',scatter:'价格与评分',discount:'折扣分布',stock:'库存结构',matrix:'颜色 × 尺码',colors:'颜色分布',sizes:'尺码分布',materials:'面料分布',styles:'风格分布',seasons:'季节分布',brands:'品牌分布',coverage:'字段完整度',freshness:'详情新鲜度',history:'历史覆盖',events:'变化事件',matches:'已确认匹配'};
const visible=(state,key)=>!state.hiddenPanels.has(key);
const tinyEmpty=(title,description)=>`<div class="wb-empty" title="${esc(description)}"><strong>${esc(title)}</strong></div>`;
function card(state,key,title,subtitle,content,note='',wide=false) {
  if(!visible(state,key))return '';
  const detail=[subtitle,note].filter(Boolean).join('\n');
  return `<section class="panel wb-card ${wide?'wide':''}"><div class="wb-card-header"><h2 class="wb-card-title">${esc(title)}</h2><button class="wb-inline-button" data-wb-inspect="${key}" data-wb-detail="${esc(detail)}" title="${esc(detail)}" aria-label="查看${esc(title)}统计口径">ⓘ</button></div><div class="wb-chart">${content}</div></section>`;
}
function barList(rows,{labelKey='value',valueKey='count',action='',dimension='',limit=8,format=number=>num(number),percentages=false}={}) {
  const subset=(rows||[]).slice(0,limit),max=Math.max(1,...subset.map(r=>Number(r[valueKey]||0)));
  if(!subset.length)return tinyEmpty('暂无可统计的观察','字段补全后会自动出现对应分布。');
  return `<div class="wb-bar-list">${subset.map((r,i)=>`<div class="wb-bar-row"><button class="wb-bar-label" ${action?`data-wb-drill="${esc(action)}" data-value="${esc(r[labelKey])}" data-key="${esc(r.id||'')}" data-dimension="${esc(dimension)}"`: 'disabled'} title="${esc(r[labelKey])}">${esc(r.display_label??(r[labelKey]==='unknown'?'未知':r[labelKey]))}</button><div class="wb-bar-track"><span class="wb-bar-fill" style="width:${Math.min(100,Number(r[valueKey]||0)/max*100)}%;background:${palette[i%palette.length]}"></span></div><span class="wb-bar-value">${esc(format(r[valueKey]))}${percentages?'%':''}</span></div>`).join('')}${(rows||[]).length>limit?`<small class="wb-list-limit">展示前 ${limit} 项</small>`:''}</div>`;
}
function histogram(data) {
  const bins=data.price_histogram||[],max=Math.max(1,...bins.map(b=>b.count));
  if(!data.summary?.price_known_products)return tinyEmpty('公开价格尚未观察','缺失报价不会显示为 0，也不会进入价格分布。');
  const width=370,height=202,left=30,right=9,top=22,bottom=34,plot=height-top-bottom;
  const guides=[0,.5,1].map(f=>{const y=top+(1-f)*plot;return `<line x1="${left}" x2="${width-right}" y1="${y}" y2="${y}" stroke="#edf0f6"/><text x="${left-7}" y="${y+4}" text-anchor="end" font-size="10" fill="#8490a5">${Math.round(max*f)}</text>`;}).join('');
  return `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="公开起价区间商品数量"><title>${esc(bins.map(b=>`${b.label} ${b.count}件`).join('，'))}</title>${guides}${bins.map((b,i)=>{const slot=(width-left-right)/bins.length,x=left+(i+.5)*slot,h=b.count/max*plot;return `<g data-wb-drill="${b.key==='unknown'?'price-unknown':'price'}" data-low="${esc(b.lower??'')}" data-high="${esc(b.upper??'')}" role="button" tabindex="0" aria-label="查看${esc(b.label)}商品"><rect x="${x-17}" y="${top+plot-h}" width="34" height="${h>0?Math.max(2,h):0}" rx="3" fill="${palette[0]}" opacity="${.5+(i%3)*.2}"><title>${esc(b.label)}：${num(b.count)} 件，点击查看</title></rect><text x="${x}" y="${Math.max(14,top+plot-h-7)}" text-anchor="middle" fill="#62708a" font-size="11">${b.count}</text><text x="${x}" y="${height-11}" text-anchor="middle" fill="#8490a5" font-size="9">${esc(b.label)}</text></g>`;}).join('')}</svg>`;
}
function donut(rows,{labelKey='label',countKey='count',action=''}={}) {
  const total=(rows||[]).reduce((sum,r)=>sum+Number(r[countKey]||0),0);
  if(!total)return tinyEmpty('暂无库存观察','首次有效观察完成后显示真实库存结构。');
  const circumference=2*Math.PI*62;let offset=0;
  const arcs=rows.map((r,i)=>{const length=r[countKey]/total*circumference,start=offset;offset+=length;return `<circle cx="90" cy="90" r="62" stroke="${palette[i%palette.length]}" stroke-width="18" fill="none" stroke-dasharray="${length} ${circumference}" stroke-dashoffset="${-start}" transform="rotate(-90 90 90)"><title>${esc(r[labelKey])} ${num(r[countKey])} · ${(r[countKey]/total*100).toFixed(1)}%</title></circle>`;}).join('');
  return `<div class="wb-donut-layout"><svg class="wb-donut" viewBox="0 0 180 180" role="img" aria-label="${esc(rows.map(r=>`${r[labelKey]} ${r[countKey]}`).join('，'))}">${arcs}<text x="90" y="88" text-anchor="middle" fill="#26324a" font-size="24" font-weight="600">${num(total)}</text><text x="90" y="108" text-anchor="middle" fill="#8793a8" font-size="10">当前商品样本</text></svg><div class="wb-legend">${rows.map((r,i)=>`<button class="wb-legend-row" ${action?`data-wb-drill="${action}" data-value="${esc(r.status||'')}"`: 'disabled'}><span class="wb-legend-dot" style="background:${palette[i%palette.length]}"></span><span>${esc(r[labelKey])}</span><strong>${num(r[countKey])}</strong><small>${(r[countKey]/total*100).toFixed(1)}%</small></button>`).join('')}</div></div>`;
}

function heatmap(matrix) {
  if(!matrix?.cells?.length)return tinyEmpty('规格组合尚未补全','有效详情需同时提供颜色与尺码，格子表示实际观察到的 SKU 数。');
  const rows=matrix.colors||[],cols=matrix.sizes||[],index=new Map(matrix.cells.map(c=>[`${c.color}\u0000${c.size}`,c]));
  const max=Math.max(1,...matrix.cells.map(c=>c.count));
  return `<div class="wb-heatmap" role="region" aria-label="颜色尺码规格矩阵"><table><thead><tr><th>颜色 / 尺码</th>${cols.map(s=>`<th>${esc(s)}</th>`).join('')}</tr></thead><tbody>${rows.map(color=>`<tr><th class="wb-matrix-label" title="${esc(color)}">${esc(color)}</th>${cols.map(size=>{const cell=index.get(`${color}\u0000${size}`);return `<td><span class="wb-matrix-cell" style="background:${cell?`rgba(55,101,239,${.07+cell.count/max*.7})`:'#f6f8fb'};color:${cell&&cell.count/max>.65?'white':'#51617e'}" title="${esc(cell?`${color} / ${size}：${cell.count} 个SKU，有货${cell.in_stock}、缺货${cell.out_of_stock}、未知${cell.unknown}`:'未观察到该组合')}">${cell?num(cell.count):'—'}</span></td>`;}).join('')}</tr>`).join('')}</tbody></table></div>`;
}
function coverageTable(rows) {
  return `<div class="wb-field-table">${(rows||[]).map(r=>`<div class="wb-field-row"><span>${esc(r.label)}</span><div class="wb-bar-track"><span class="wb-bar-fill" style="width:${Number(r.coverage_pct||0)}%;background:${Number(r.coverage_pct||0)<30?'#c5cfe5':'#3765ef'}"></span></div><strong>${percent(r.coverage_pct)}</strong><small>${num(r.known_count)} / ${num(r.total_count)}</small></div>`).join('')}</div>`;
}
function eventChart(data) {
  const rows=data.event_trend_daily||[];
  if(!rows.length)return tinyEmpty('当前窗口尚无变化序列','初次采集建立基线，后续实际变化才会进入趋势。');
  const max=Math.max(1,...rows.map(r=>r.count)),width=370,height=176,left=24,right=14,top=16,bottom=27;
  const times=rows.map(r=>Date.parse(r.date+'T00:00:00+08:00')),first=times[0],last=times.at(-1);
  const points=rows.map((r,i)=>[first===last?width/2:left+(times[i]-first)/(last-first)*(width-left-right),top+(1-r.count/max)*(height-top-bottom)]);
  return `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="每日实际变化事件数量"><line x1="${left}" x2="${width-right}" y1="${height-bottom}" y2="${height-bottom}" stroke="#edf0f6"/><polyline points="${points.map(p=>p.join(',')).join(' ')}" stroke="#3765ef" stroke-width="2" fill="none"/>${points.map(([x,y],i)=>`<circle cx="${x}" cy="${y}" r="4" fill="#3765ef"><title>${esc(rows[i].date)}：${num(rows[i].count)} 次变化</title></circle>`).join('')}<text x="${left}" y="${height-8}" fill="#8995a8" font-size="10">${esc(rows[0].date)}</text><text x="${width-right}" y="${height-8}" text-anchor="end" fill="#8995a8" font-size="10">${esc(rows.at(-1).date)}</text></svg>`;
}
function attrCard(state,data,key,title,attribute) {
  const d=data.attribute_distributions?.[key];
  const content=d?.items?.length?barList(d.items,{action:'attribute',dimension:attribute,limit:10}):tinyEmpty(`${title}字段尚未补全`,`${num(d?.known_products||0)} / ${num(d?.total_products||data.summary.products)} 件商品有可用观察。`);
  return card(state,key,title,`按商品计数 · 已知 ${num(d?.known_products)} / ${num(d?.total_products)} 件`,content,`多标签商品会进入多个分组${d?.truncated?'；当前仅展示排名靠前的标签':''}。点击标签查看实际商品。`);
}
function metrics(data) {
  const s=data.summary,h=data.history_summary;
  const values=[['分析商品',num(s.products),`${num(s.shops)} 家店铺`],['公开起价中位数',money(s.price_stats?.median,data.scope.currency),`${num(s.price_known_products)} 件有报价`],['详情覆盖',percent(s.detail_coverage_pct),`${num(s.detail_products)} / ${num(s.products)} 件`],['历史可对比商品',num(h?.products_with_two_observations),`${num(h?.snapshot_count)} 次快照`]];
  return `<div class="wb-metric-grid">${values.map(([k,v,n])=>`<div class="panel wb-metric"><span>${k}</span><strong class="wb-metric-value">${v}</strong><small class="wb-metric-caption">${n}</small></div>`).join('')}</div>`;
}
function matchingCards(state,data) {
  const m=state.matchesSummary;
  if(!m||m.schema_ready===false||m.pending_schema)return card(state,'matches','已确认同款与规格关系','关系需由用户核对后确认',tinyEmpty('尚无可用的已确认关系','进入竞品匹配页面确认同款和精确 SKU，记录将持久保存。'));
  const confirmed=(state.matchItems||[]).filter(p=>p.status==='confirmed');
  const sku=confirmed.flatMap(p=>(p.sku_matches||[]).filter(s=>s.status==='confirmed').map(s=>({...s,relation:p}))),valid=sku.filter(s=>s.comparison?.comparable);
  const table=valid.length?`<div class="table-wrap"><table><thead><tr><th>同款 / SKU</th><th>基准报价</th><th>另一报价</th><th>价差</th></tr></thead><tbody>${valid.slice(0,15).map(s=>`<tr><td><button class="row-link" data-action="product" data-id="${esc(s.relation.left_product_id)}">${esc(s.relation.left_product?.title||'商品 '+s.relation.left_product_id)}</button></td><td>${money(s.comparison.left_price,s.comparison.currency)}</td><td>${money(s.comparison.right_price,s.comparison.currency)}</td><td>${money(s.comparison.difference_right_minus_left,s.comparison.currency)}</td></tr>`).join('')}</tbody></table></div>`:tinyEmpty('尚无可计算的同规格价差','同款关系确认后，还需确认具体颜色、尺码和完整规格对应的 SKU。');
  return `<div class="wb-relation-summary"><span class="wb-scope-tag">全市场</span><span><strong>${num(m.confirmed_product_pairs)}</strong> 对同款</span><span><strong>${num(m.confirmed_sku_pairs)}</strong> 对 SKU</span><a class="button" href="#matches">管理匹配 →</a></div>`+card(state,'matches','同规格公开价差','全市场已确认关系；不随工作台商品筛选改变。只计算当前规格、币种和价格口径均一致的 SKU。',table,`${num(valid.length)} 对已载入可比规格；${num(sku.length-valid.length)} 对暂不满足报价或规格口径。`,true);
}
export function renderWorkbench(state,{shops=[],icon=()=>''}={}) {
  const d=state.data,f=state.filters,categories=state.categoryOptions.length?state.categoryOptions:d?.categories||[],shopOptions=shops.length?shops:d?.shops||[];
  const selectedShop=shopOptions.find(s=>String(s.id)===String(f.shop_id)),scopeLabel=f.shop_id?selectedShop?.name||selectedShop?.canonical_name||`店铺 ${f.shop_id}`:'全部店铺';
  const filters=`<div class="wb-toolbar"><label class="wb-search">${icon('search')}<input id="wb-query" data-wb-filter="q" value="${esc(f.q||'')}" placeholder="搜索商品或品牌" aria-label="工作台商品搜索" maxlength="200" autocomplete="off"></label>${renderShopPicker(shopOptions,f.shop_id)}<select data-wb-filter="category" aria-label="工作台类目筛选"><option value="">全部公开类目</option>${categories.map(c=>`<option value="${esc(c.category)}" ${f.category===c.category?'selected':''}>${esc(c.category==='unknown'?'类目未知':c.category)}</option>`).join('')}</select>${f.q||f.category||f.shop_id?'<button class="wb-inline-button" data-wb-action="clear">清除筛选</button>':''}<div class="wb-customize"><details><summary class="button">${icon('columns')}图表设置</summary><div class="wb-customize-menu"><span>选择要显示的模块</span>${Object.entries(panelNames).map(([key,title])=>`<label><input type="checkbox" data-wb-panel="${key}" ${visible(state,key)?'checked':''}>${title}</label>`).join('')}</div></details></div></div>`;
  const tabs=`<div class="wb-tabs" role="group" aria-label="工作台分析主题">${Object.entries(workbenchTabs).map(([key,title])=>`<button class="wb-tab ${state.tab===key?'active':''}" data-wb-tab="${key}" aria-pressed="${state.tab===key}">${title}</button>`).join('')}</div>`;
  if(!d){
    if(state.error)return `${filters}<div class="wb-scope-wait" role="status"><strong>${esc(scopeLabel)}</strong><span>${state.error.message?.includes('统计仍在排队')?'统计仍在排队，可稍后重试':'数据读取失败'}</span><button class="button" data-wb-action="retry">重试</button></div>${tabs}`;
    const skeleton=`<div class="wb-skeleton-grid" aria-hidden="true">${Array.from({length:4},()=>'<div class="wb-skeleton-card"><span class="wb-skeleton-line"></span><span class="wb-skeleton-line"></span></div>').join('')}</div><div class="wb-skeleton-charts" aria-hidden="true">${Array.from({length:3},()=>'<div class="wb-skeleton-card"><span class="wb-skeleton-line"></span></div>').join('')}</div>`;
    return `${filters}<div class="wb-scope-wait" role="status" data-wb-status="loading"><span class="loader"></span><strong>${esc(scopeLabel)}</strong><span>正在统计…</span></div>${tabs}${skeleton}`;
  }
  const context=`<div class="wb-context" data-applied-shop="${esc(d.scope.shop_id||'')}"><span class="wb-coverage-pill">${esc(scopeLabel)} · ${num(d.summary.products)} 件商品</span><span>${esc(d.scope.currency)} · ${d._meta?.status==='stale'?'上次统计的公开报价':'当前最新公开报价'}</span><span>更新于 ${esc(formatDate(d.generated_at))}</span>${d._meta?.expired?'<span class="badge amber">缓存已过期，等待刷新</span>':''}${state.loading?'<span>正在更新…</span>':''}</div>`;
  let cards='';
  if(state.tab==='market') {
    const shopRows=(d.shops||[]).filter(s=>s.price_median!=null).sort((a,b)=>Number(b.price_median)-Number(a.price_median)).map(s=>({...s,value:s.name,count:Number(s.price_median)}));
    cards=card(state,'price','公开起价分布','价格下限 · 点击价格带查看商品',histogram(d),`${num(d.summary.price_known_products)} 件报价已知；中位数按完整筛选范围计算。`)
      +card(state,'shops','店铺价格带对照','公开起价中位数 · 非销量排名',barList(shopRows,{action:'shop-name',format:v=>money(v,d.scope.currency)}),'样本结构与采样深度会影响店铺间比较。')
      +card(state,'categories','公开类目结构','按已采集商品数量',barList((d.categories||[]).map(c=>({value:c.category,display_label:c.category==='unknown'?'类目未知':c.category.split('>').at(-1).trim(),count:c.count})),{action:'category'}),'点击类目，下钻到当前筛选范围的商品。')
      +card(state,'scatter','价格与评分','横轴评分 · 纵轴起价 · 点大小为评价数',scatter(d),`${num(d.price_rating_scatter?.returned_count)} / ${num(d.price_rating_scatter?.eligible_count)} 个可用样本${d.price_rating_scatter?.truncated?'，点位为限定样本':''}；不推算销量。`)
      +card(state,'discount','当前折扣深度','已知折扣率的公开观察',barList((d.discount_buckets||[]).map(b=>({value:b.label,count:b.count}))),`只有 ${num(d.summary.discount_known_products)} 件商品折扣状态已知，未知不视为零折扣。`)
      +card(state,'events','变化事件趋势','按实际观察时间 · 统一顶部时间窗口',eventChart(d),`${num(d.summary.events_in_window)} 次窗口内变化；首次发现时间不代表上架时间。`);
  }else if(state.tab==='attributes') {
    cards=attrCard(state,d,'colors','服装颜色','color')+attrCard(state,d,'sizes','公开尺码','size')+attrCard(state,d,'materials','面料与材质','material')+attrCard(state,d,'styles','服装风格','style')+attrCard(state,d,'seasons','适用季节','season')+attrCard(state,d,'brands','品牌结构','brand')+card(state,'matrix','颜色 × 尺码规格矩阵','数字是实际 SKU 数，— 表示未观察',heatmap(d.color_size_matrix),`${num(d.color_size_matrix?.shown_variants)} / ${num(d.color_size_matrix?.eligible_variants)} 个有完整规格的 SKU${d.color_size_matrix?.truncated?'，仅展示排名靠前的颜色与尺码':''}。原始尺码体系保留，不默认跨品牌相同。`,true);
  }else if(state.tab==='stock') {
    cards=card(state,'stock','商品库存结构','当前商品状态 · 未知独立展示',donut((d.stock_distribution||[]).map(s=>({...s,label:stockNames[s.status]||s.status})),{action:'stock'}),'部分有货不等于全部规格都有库存，点击状态查看商品。')
      +card(state,'shops','店铺 SKU 缺货率','只以库存状态明确的本次规格为分母',barList((d.shops||[]).filter(s=>s.sku_stockout_pct!=null).map(s=>({...s,value:s.name,count:Number(s.sku_stockout_pct)})),{action:'shop-name',format:v=>`${Number(v).toFixed(1)}%`}),'旧 SKU 保留供历史追溯，不会被误计为本次观察的缺货。')
      +card(state,'discount','当前折扣深度','不同折扣分组的商品数量',barList((d.discount_buckets||[]).map(b=>({value:b.label,count:b.count}))),`${num(d.summary.discount_known_products)} 件折扣状态已知。`)
      +card(state,'matrix','规格库存热力图','颜色 × 尺码 · 悬停查看有货与缺货数量',heatmap(d.color_size_matrix),`${num(d.color_size_matrix?.shown_variants)} / ${num(d.color_size_matrix?.eligible_variants)} 个完整规格 SKU 已展示；${num(d.color_size_matrix?.missing_attributes_variants)} 个缺颜色或尺码。未观察组合不判缺货。`,true);
  }else if(state.tab==='competition')cards=matchingCards(state,d);
  else {
    const h=d.history_summary,span=h?.span_hours==null?'—':`${Number(h.span_hours).toFixed(1)} 小时`;
    cards=card(state,'coverage','数据字段完整度','已知值 / 当前筛选范围商品数',coverageTable(d.collection_quality?.field_coverage),'字段不足会限制对应分析，未知值始终保留。',true)
      +card(state,'freshness','详情采集新鲜度','从最近一次成功详情观察计算',barList((d.collection_quality?.freshness_buckets||[]).map(b=>({value:b.label,count:b.count}))),'未采集详情与过期观察分别统计。')
      +card(state,'history','历史数据覆盖','有实际快照的商品才进入历史统计',`<div class="wb-history-stats"><div><span>快照总数</span><strong>${num(h?.snapshot_count)}</strong></div><div><span>至少两次观察</span><strong>${num(h?.products_with_two_observations)}</strong></div><div><span>实际覆盖跨度</span><strong>${span}</strong></div><div><span>最早 / 最近观察</span><small>${esc(formatDate(h?.earliest_capture_at))}<br>${esc(formatDate(h?.latest_capture_at))}</small></div></div>`,h?.trend_ready?'可以查看已形成的历史观察；采样间隔以实际时间为准。':'正在建立历史基线，尚不足以证明长期趋势。')
      +card(state,'events','变化类型分布','仅统计窗口内实际事件',barList((d.events_by_type||[]).map(e=>({value:labels.eventTypes[e.event_type]||e.event_type,count:e.count}))),`${num(d.summary.events_in_window)} 次实际变化，初始采集只建立基线。`);
  }
  return `${filters}${context}${metrics(d)}${tabs}${state.error?`<div class="wb-alert">${esc(state.error.message)}；当前显示上次成功读取的数据。</div>`:''}${state.tab==='competition'?cards:`<div class="wb-grid">${cards||tinyEmpty('此主题的模块已隐藏','在图表设置中选择需要显示的模块。')}</div>`}<div class="wb-footer-actions"><button class="button" data-wb-drill="all">查看商品 →</button></div>`;
}
export function renderDrillProducts(result,title='商品明细') {
  const rows=result.items||[];
  const fullTitle=String(title||'商品明细'),shortTitle=fullTitle.split(/\s*[›>]\s*/).filter(Boolean).at(-1)||fullTitle;
  const page=result.page||1,pages=Math.max(page,result.total_pages||page+(result.has_more?1:0));
  const pagination=pages>1?`<div class="pagination"><button class="button" data-wb-drill-page="prev" ${page<=1?'disabled':''}>上一页</button><span aria-label="第 ${page} 页，共 ${pages} 页">${page} / ${pages}</span><button class="button" data-wb-drill-page="next" ${page>=pages?'disabled':''}>下一页</button></div>`:'';
  return `<section class="catalog-list-view">
    <div class="catalog-dialog-top"><span class="catalog-context">商品明细</span><button class="icon-button" data-action="close-dialog" aria-label="关闭商品明细"><svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"/></svg></button></div>
    <div class="catalog-heading"><h2 class="catalog-title" id="dialog-title" title="${esc(fullTitle)}">${esc(shortTitle)}</h2><span class="catalog-count"><strong>${num(result.total)}</strong> 件 · ${esc(result.scope?.currency||'USD')}</span></div>
    <div class="catalog-list-body">${rows.length?`<div class="table-wrap wb-drill-table"><table><thead><tr><th>商品 / 店铺</th><th>公开报价</th><th>评分</th><th>库存</th><th></th></tr></thead><tbody>${rows.map(p=>`<tr><td><div class="table-main">${productPhoto(p)}<div><button class="product-title" data-action="product" data-id="${esc(p.id)}" title="${esc(p.title)}">${esc(p.title||p.platform_product_id)}</button><span class="cell-secondary">${esc(p.shop_name)}</span></div></div></td><td class="cell-price">${money(p.price_min,p.currency)}${p.price_kind==='starting_price'?'<span class="cell-secondary">起价</span>':p.price_kind==='range'?`<span class="cell-secondary">至 ${money(p.price_max,p.currency)}</span>`:''}</td><td>${p.rating==null?'—':esc(p.rating)}</td><td>${esc(stockNames[p.stock_status]||'未知')}</td><td><button class="row-link" data-action="product" data-id="${esc(p.id)}">详情 →</button></td></tr>`).join('')}</tbody></table></div>`:'<div class="pi-empty">没有匹配的商品</div>'}</div>
    <div class="catalog-list-footer"><span>${pages>1?'本页 ':''}${num(rows.length)} 件商品</span>${pagination}</div>
  </section>`;
}
export function productPhoto(product,{large=false}={}) {
  const url=safeUrl(product.thumbnail_url||product.image_url);
  return url?`<span class="product-photo${large?' large':''}"><img src="${esc(url)}" alt="${esc(product.title||'商品图片')}" loading="lazy" referrerpolicy="no-referrer"><span class="product-photo-fallback" hidden>图片暂不可用</span></span>`:'<span class="product-photo empty" aria-label="图片尚未采集"><span class="product-photo-fallback">待采图</span></span>';
}
