import { money } from './api.js';

const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num=value=>value==null?'—':new Intl.NumberFormat('zh-CN').format(value);
const icon=name=>`<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="${name==='close'?'m6 6 12 12M6 18 18 6':'M8 3H3v5M16 3h5v5M3 16v5h5M21 16v5h-5'}"/></svg>`;
const validPoints=data=>(data?.price_rating_scatter?.points||[]).filter(p=>p.rating!=null&&p.price_min!=null&&String(p.price_min).trim()&&Number.isFinite(Number(p.rating))&&Number.isFinite(Number(p.price_min))&&Number(p.rating)>=0&&Number(p.rating)<=5&&Number(p.price_min)>=0);
function niceStep(range,divisions=5){
  const raw=Math.max(range,.01)/divisions,base=10**Math.floor(Math.log10(raw));
  return ([1,2,2.5,5,10].find(step=>step*base>=raw-1e-9)||10)*base;
}
function axisTicks(min,max,step){
  const ticks=[];
  for(let i=Math.ceil((min-1e-9)/step);i*step<=max+1e-9;i++)ticks.push(Number((i*step).toFixed(6)));
  // Keep the domain ends readable when they fall between regular intervals.
  if(!ticks.length||ticks[0]>min+1e-8){if(ticks.length&&ticks[0]-min<step*.6)ticks.shift();ticks.unshift(min);}
  if(ticks.at(-1)<max-1e-8){if(ticks.length>1&&max-ticks.at(-1)<step*.6)ticks.pop();ticks.push(max);}
  return ticks;
}

/** Both views use the same observations and domain; the large view lays out at its actual size. */
export function renderScatter(data,{expanded=false,width=380,height=232}={}){
  const points=validPoints(data),currency=data?.scope?.currency||'USD';
  if(!points.length)return '<div class="wb-empty"><strong>暂无价格与评分数据</strong></div>';
  const left=expanded?65:47,right=expanded?22:14,top=expanded?24:19,bottom=expanded?46:37,plotWidth=width-left-right,plotHeight=height-top-bottom;
  const ratings=points.map(p=>Number(p.rating)),minRating=Math.min(...ratings),maxRating=Math.max(...ratings);
  const domainRatingStep=niceStep(Math.max(.2,maxRating-minRating+.16));
  const domainMin=Math.max(0,Math.floor((minRating-.08+1e-9)/domainRatingStep)*domainRatingStep),domainMax=Math.min(5,Math.ceil((maxRating+.08-1e-9)/domainRatingStep)*domainRatingStep);
  const domainPriceStep=niceStep(Math.max(1,...points.map(p=>Number(p.price_min)))*1.08,4),priceMax=Math.ceil(Math.max(1,...points.map(p=>Number(p.price_min)))*1.08/domainPriceStep)*domainPriceStep;
  const ratingSpan=domainMax-domainMin||.2;
  // Tick density follows available room; the observation domain is identical in both views.
  const xDivisions=Math.max(2,Math.min(expanded?20:6,Math.floor(plotWidth/(expanded?48:38))));
  const yDivisions=Math.max(2,Math.min(expanded?14:6,Math.floor(plotHeight/(expanded?30:24))));
  const ratingStep=niceStep(ratingSpan,xDivisions),priceStep=niceStep(priceMax,yDivisions),fontSize=expanded?12:10;
  const xTicks=axisTicks(domainMin,domainMax,ratingStep),yTicks=axisTicks(0,priceMax,priceStep);
  const xPosition=value=>left+(value-domainMin)/ratingSpan*plotWidth,yPosition=value=>top+(1-value/priceMax)*plotHeight;
  const minorX=xTicks.slice(1).map((value,i)=>({value:(value+xTicks[i])/2,gap:xPosition(value)-xPosition(xTicks[i])})).filter(tick=>tick.gap>=28).map(tick=>`<line data-grid="minor" data-axis="x" data-value="${tick.value}" x1="${xPosition(tick.value)}" x2="${xPosition(tick.value)}" y1="${top}" y2="${height-bottom}" stroke="#f5f7fb" stroke-dasharray="2 4"/>`).join('');
  const minorY=yTicks.slice(1).map((value,i)=>({value:(value+yTicks[i])/2,gap:yPosition(yTicks[i])-yPosition(value)})).filter(tick=>tick.gap>=24).map(tick=>`<line data-grid="minor" data-axis="y" data-value="${tick.value}" x1="${left}" x2="${width-right}" y1="${yPosition(tick.value)}" y2="${yPosition(tick.value)}" stroke="#f5f7fb" stroke-dasharray="2 4"/>`).join('');
  const guides=yTicks.map(value=>{const y=yPosition(value);return `<line x1="${left}" x2="${width-right}" y1="${y}" y2="${y}" stroke="#edf0f6"/><text data-axis="y" data-value="${value}" x="${left-8}" y="${y+4}" fill="#8793a6" font-size="${fontSize}" text-anchor="end">${esc(money(value,currency).replace(/^US/,'').replace(/\.00$/,''))}</text>`;}).join('');
  const axis=xTicks.map(value=>{const x=xPosition(value);return `<line x1="${x}" x2="${x}" y1="${top}" y2="${height-bottom}" stroke="#edf0f6"/><text data-axis="x" data-value="${value}" x="${x}" y="${height-20}" text-anchor="middle" font-size="${fontSize}" fill="#8793a6">${Number(value.toFixed(2))}</text>`;}).join('');
  const unique=[...new Set(ratings)].sort((a,b)=>a-b),minGap=unique.length>1?Math.min(...unique.slice(1).map((v,i)=>v-unique[i])):ratingSpan;
  const spread=Math.min(6,minGap/ratingSpan*plotWidth*.24),offsets=[0,-spread,spread,-spread/2,spread/2],placed=[];
  const ordered=[...points].sort((a,b)=>Number(a.price_min)-Number(b.price_min)||String(a.id).localeCompare(String(b.id),'en',{numeric:true}));
  const circles=ordered.map((p,index)=>{
    const anchor=left+(Number(p.rating)-domainMin)/ratingSpan*plotWidth,y=top+(1-Number(p.price_min)/priceMax)*plotHeight;
    const radius=Math.min(5.1,3.1+Math.log10(Math.max(0,Number(p.review_count)||0)+1)*.55);
    let x=anchor,best=Infinity;
    for(const offset of offsets){const candidate=Math.max(left,Math.min(width-right,anchor+offset));let cost=Math.abs(offset)*.025;
      for(const prev of placed){if(Math.abs(prev.y-y)>prev.r+radius+1)continue;const overlap=Math.max(0,prev.r+radius+.8-Math.hypot(prev.x-candidate,prev.y-y));cost+=overlap*overlap;}
      if(cost<best){best=cost;x=candidate;}}
    placed.push({x,y,r:radius});
    const label=`${p.title||'商品 '+p.id}，评分 ${p.rating}，公开起价 ${money(p.price_min,currency)}，${p.review_count==null?'评价数未知':num(p.review_count)+' 条评价'}`;
    return `<circle class="wb-scatter-point" cx="${x.toFixed(2)}" cy="${y.toFixed(2)}" r="${radius.toFixed(2)}" fill="#3765ef" opacity=".55" stroke="white" stroke-width=".85" data-scatter-point="${esc(p.id)}" data-id="${esc(p.id)}" data-rating="${esc(p.rating)}" data-price="${esc(p.price_min)}" data-anchor-x="${anchor.toFixed(2)}" role="button" tabindex="${index===0?'0':'-1'}" aria-label="${esc(label)}"/>`;
  }).join('');
  const extent=`评分 ${Number(domainMin.toFixed(2))} 至 ${Number(domainMax.toFixed(2))}`;
  const svg=`<svg class="wb-scatter" viewBox="0 0 ${width} ${height}" data-domain-min="${domainMin}" data-domain-max="${domainMax}" data-price-min="0" data-price-max="${priceMax}" data-rating-step="${ratingStep}" data-price-step="${priceStep}" data-expanded="${expanded}" role="group" aria-label="价格与评分分布，${esc(extent)}，${points.length} 件商品">${minorX}${minorY}${guides}${axis}${circles}<text x="${left+plotWidth/2}" y="${height-3}" text-anchor="middle" fill="#8793a6" font-size="${fontSize}">评分</text></svg>`;
  if(expanded)return svg;
  const coverage=data.price_rating_scatter;
  const count=coverage.truncated?`<span class="wb-chart-sample" title="展示样本 / 全部可用商品">${num(coverage.returned_count)} / ${num(coverage.eligible_count)}</span>`:'';
  return `<div class="sc-chart-surface" data-scatter-surface>${count}<button class="sc-expand-button" data-scatter-expand aria-label="放大价格与评分图表" title="放大图表">${icon('expand')}<span>放大</span></button>${svg}</div>`;
}

/** One delegated controller works through workbench refreshes and native modal layers. */
export function installScatterInteractions({getData,getShops,onProduct}){
  const dialog=document.createElement('dialog');dialog.id='scatter-dialog';dialog.className='scatter-dialog';dialog.setAttribute('aria-labelledby','scatter-dialog-title');
  document.body.append(dialog);
  const tip=document.createElement('aside');tip.className='sc-tooltip';tip.hidden=true;tip.setAttribute('aria-label','商品信息');document.body.append(tip);
  let expandedData=null,expandedShops=[],opener=null,activePoint=null,pinned=false,hideTimer,resizeFrame;
  const source=()=>dialog.open?expandedData:getData();
  const findPoint=id=>validPoints(source()).find(p=>String(p.id)===String(id));
  function hide(){clearTimeout(hideTimer);tip.hidden=true;activePoint?.classList.remove('is-selected');activePoint?.removeAttribute('aria-describedby');activePoint=null;pinned=false;}
  function scheduleHide(){clearTimeout(hideTimer);if(!pinned)hideTimer=setTimeout(hide,180);}
  function positionTip(point){
    const rect=point.getBoundingClientRect(),bounds=tip.getBoundingClientRect(),gap=14,margin=12;
    const viewWidth=document.documentElement.clientWidth,viewHeight=window.innerHeight;
    let left=rect.right+gap,top=rect.top-bounds.height/2;
    if(left+bounds.width>viewWidth-margin)left=rect.left-bounds.width-gap;
    if(left<margin){left=(viewWidth-bounds.width)/2;top=rect.bottom+gap;if(top+bounds.height>viewHeight-margin)top=rect.top-bounds.height-gap;}
    tip.style.left=`${Math.max(margin,Math.min(left,viewWidth-bounds.width-margin))}px`;
    tip.style.top=`${Math.max(margin,Math.min(top,viewHeight-bounds.height-margin))}px`;
  }
  function show(point,{pin=false}={}){
    if(pinned&&!pin)return;
    clearTimeout(hideTimer);
    const p=findPoint(point.dataset.scatterPoint);if(!p)return;
    if(activePoint!==point||tip.hidden){
      activePoint?.classList.remove('is-selected');activePoint?.removeAttribute('aria-describedby');
      activePoint=point;point.classList.add('is-selected');point.setAttribute('aria-describedby','sc-point-info');
      const shops=dialog.open?expandedShops:getShops();
      const shop=p.shop_name||(shops||[]).find(s=>String(s.id)===String(p.shop_id));
      const shopName=typeof shop==='string'?shop:shop?.name||shop?.canonical_name||'店铺未知';
      const currency=source()?.scope?.currency||'USD';
      tip.innerHTML=`<div id="sc-point-info" role="tooltip"><div class="sc-tooltip-shop">${esc(shopName)}</div><p class="sc-tooltip-title" title="${esc(p.title)}">${esc(p.title||'商品 '+p.id)}</p><dl class="sc-tooltip-metrics"><div><dt>公开起价</dt><dd>${esc(money(p.price_min,currency))}</dd></div><div><dt>评分</dt><dd>${esc(Number(p.rating).toFixed(2))} / 5</dd></div><div><dt>评价数</dt><dd>${num(p.review_count)}</dd></div><div><dt>币种</dt><dd>${esc(currency)}</dd></div></dl></div><div class="sc-tooltip-actions"><button class="button" data-scatter-product="${esc(p.id)}">查看商品 →</button><button class="icon-button" data-scatter-dismiss aria-label="关闭商品信息">${icon('close')}</button></div>`;
    }
    pinned=pin;
    (dialog.open?dialog:document.body).append(tip);tip.hidden=false;positionTip(point);
  }
  function nearest(svg,event){
    const matrix=svg.getScreenCTM();if(!matrix)return null;
    const cursor=new DOMPoint(event.clientX,event.clientY).matrixTransform(matrix.inverse());
    const scale=Math.hypot(matrix.a,matrix.b)||1;
    let closest=null,distance=Infinity;
    for(const point of svg.querySelectorAll('[data-scatter-point]')){
      const d=Math.hypot(point.cx.baseVal.value-cursor.x,point.cy.baseVal.value-cursor.y)*scale;
      if(d<=Math.max(event.pointerType==='touch'?18:9,point.r.baseVal.value*scale+3)&&d<distance){distance=d;closest=point;}
    }
    return closest;
  }
  function drawExpanded(){
    if(!dialog.open||!expandedData)return;
    const body=dialog.querySelector('.sc-dialog-body'),style=getComputedStyle(body),selectedId=activePoint?.dataset.scatterPoint,wasPinned=pinned;
    const focusId=body.contains(document.activeElement)?document.activeElement.dataset.scatterPoint:null;
    hide();
    body.innerHTML=renderScatter(expandedData,{expanded:true,width:Math.max(240,body.clientWidth-parseFloat(style.paddingLeft)-parseFloat(style.paddingRight)),height:Math.max(150,body.clientHeight-parseFloat(style.paddingTop)-parseFloat(style.paddingBottom))});
    const points=[...body.querySelectorAll('[data-scatter-point]')];
    if(focusId){const focused=points.find(p=>p.dataset.scatterPoint===focusId);if(focused){points.forEach(p=>p.setAttribute('tabindex',p===focused?'0':'-1'));focused.focus({preventScroll:true});}}
    if(wasPinned){const point=points.find(p=>p.dataset.scatterPoint===selectedId);if(point)show(point,{pin:true});}
  }
  function open(trigger,selectedId){
    expandedData=getData();if(!validPoints(expandedData).length)return;
    expandedShops=getShops();opener=trigger;hide();
    const count=validPoints(expandedData).length,total=expandedData.price_rating_scatter?.eligible_count??count;
    dialog.innerHTML=`<header class="sc-dialog-header"><div class="sc-dialog-heading"><h2 id="scatter-dialog-title">价格与评分</h2><div class="sc-context"><span>${esc(expandedData.scope?.currency||'USD')}</span><span title="当前展示样本 / 全部价格与评分已知商品；按商品 ID 顺序取样"><strong>${num(count)}</strong> / ${num(total)} 件</span></div></div><div class="sc-actions"><button class="icon-button" data-scatter-close aria-label="关闭放大图表">${icon('close')}</button></div></header><div class="sc-dialog-body"></div><footer class="sc-dialog-footer"><span>公开起价 × 评分</span><span>点大小 · 评价数</span></footer>`;
    dialog.showModal();drawExpanded();
    dialog.querySelector('[data-scatter-close]').focus({preventScroll:true});
    if(selectedId){const point=[...dialog.querySelectorAll('[data-scatter-point]')].find(p=>p.dataset.scatterPoint===selectedId);if(point)show(point,{pin:true});}
  }
  document.addEventListener('pointermove',event=>{
    if(event.pointerType==='touch'||pinned)return;
    if(event.target.closest('.sc-tooltip')){clearTimeout(hideTimer);return;}
    const svg=event.target.closest('svg.wb-scatter');
    if(!svg||dialog.open&&!dialog.contains(svg)){scheduleHide();return;}
    const point=nearest(svg,event);if(point)show(point);else scheduleHide();
  });
  document.addEventListener('pointerout',event=>{if((event.target.closest('.sc-tooltip')||event.target.closest('svg.wb-scatter'))&&!event.relatedTarget?.closest?.('.sc-tooltip,svg.wb-scatter'))scheduleHide();});
  document.addEventListener('focusin',event=>{
    if(event.target.matches('[data-scatter-point]')){hide();show(event.target);}
    else if(!tip.contains(event.target)&&!pinned)scheduleHide();
  });
  document.addEventListener('click',event=>{
    const target=event.target;
    if(target.closest('[data-scatter-close]')){dialog.close();return;}
    if(target.closest('[data-scatter-dismiss]')){const point=activePoint;hide();point?.focus({preventScroll:true});hide();return;}
    const product=target.closest('[data-scatter-product]');
    if(product){const point=activePoint;hide();point?.focus({preventScroll:true});hide();onProduct(product.dataset.scatterProduct);return;}
    const surface=target.closest('[data-scatter-surface]'),point=target.closest('[data-scatter-point]');
    if(surface){const svg=surface.querySelector('svg.wb-scatter'),candidate=point||(target.closest('svg.wb-scatter')?nearest(svg,event):null);open(surface.querySelector('[data-scatter-expand]'),candidate?.dataset.scatterPoint);return;}
    const expandedSvg=target.closest('#scatter-dialog svg.wb-scatter');
    if(expandedSvg){const candidate=point||nearest(expandedSvg,event);if(candidate)show(candidate,{pin:true});else hide();return;}
    if(dialog.open&&target===dialog){const rect=dialog.getBoundingClientRect();if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)dialog.close();return;}
    if(!tip.contains(target))hide();
  });
  document.addEventListener('keydown',event=>{
    const point=event.target.closest('[data-scatter-point]');
    if(point&&['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Home','End'].includes(event.key)){
      event.preventDefault();const points=[...point.ownerSVGElement.querySelectorAll('[data-scatter-point]')],index=points.indexOf(point);
      const next=event.key==='Home'?0:event.key==='End'?points.length-1:(index+(['ArrowRight','ArrowDown'].includes(event.key)?1:points.length-1))%points.length;
      points.forEach((p,i)=>p.tabIndex=i===next?0:-1);hide();points[next].focus({preventScroll:true});
    }
    if(event.key==='Escape'&&!tip.hidden&&!document.getElementById('detail-dialog')?.open){event.preventDefault();hide();}
  },true);
  dialog.addEventListener('cancel',hide);
  dialog.addEventListener('close',()=>{hide();expandedData=null;expandedShops=[];document.body.append(tip);if(opener?.isConnected)opener.focus({preventScroll:true});});
  window.addEventListener('scroll',event=>{if(tip.contains(event.target))return;if(!pinned)hide();else if(activePoint?.isConnected)positionTip(activePoint);},true);
  new ResizeObserver(()=>{cancelAnimationFrame(resizeFrame);resizeFrame=requestAnimationFrame(drawExpanded);}).observe(dialog);
  window.addEventListener('resize',()=>{if(!dialog.open)hide();});
  new MutationObserver(()=>{if(activePoint&&!activePoint.isConnected)hide();}).observe(document.getElementById('page-content'),{childList:true});
  window.addEventListener('hashchange',()=>{hide();if(dialog.open)dialog.close();});
  return {hide};
}
