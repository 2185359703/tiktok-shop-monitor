// Report charts describe collected observations; they do not infer sales.
const BLUE = '#3765ef';
const GREEN = '#168473';
const GREY = '#dce2ed';
const SHANGHAI_OFFSET = 8 * 60 * 60 * 1000;
const HOUR = 60 * 60 * 1000;
const DAY = 24 * HOUR;
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
const count = value => Number.isFinite(Number(value)) && Number(value) >= 0 ? Math.floor(Number(value)) : 0;
const fmt = value => new Intl.NumberFormat('zh-CN').format(value);
const ratio = (part, total) => total > 0 ? Math.max(0, Math.min(100, part / total * 100)) : 0;
const percent = (part, total) => total > 0 ? `${ratio(part, total).toFixed(1)}%` : '—';

function dateValue(value) {
  if (typeof value !== 'string' || !value.trim()) return NaN;
  const raw = value.trim();
  // The API stores naive database times in UTC. Explicit offsets stay intact.
  const normalized = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(raw) ? raw : `${raw.replace(' ', 'T')}Z`;
  return Date.parse(normalized);
}

function dateLabel(value, full = false, dayOnly = false) {
  if (!Number.isFinite(value)) return '暂无时间';
  const date = new Date(value + SHANGHAI_OFFSET);
  const month = String(date.getUTCMonth() + 1).padStart(2, '0');
  const day = String(date.getUTCDate()).padStart(2, '0');
  const hour = String(date.getUTCHours()).padStart(2, '0');
  const minute = String(date.getUTCMinutes()).padStart(2, '0');
  if (dayOnly) return `${month}/${day}`;
  return full ? `${date.getUTCFullYear()}/${month}/${day} ${hour}:${minute}` : `${month}/${day} ${hour}:${minute}`;
}

function money(value, currency = 'USD') {
  if (value == null || value === '' || !Number.isFinite(Number(value))) return '—';
  try {
    return new Intl.NumberFormat('zh-CN', { style: 'currency', currency, maximumFractionDigits: 2 }).format(Number(value));
  } catch {
    return `${currency} ${new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 }).format(Number(value))}`;
  }
}

function niceScale(maximum, steps = 4) {
  if (maximum <= 0) return steps;
  const raw = maximum / steps;
  const base = 10 ** Math.floor(Math.log10(raw));
  const normalized = raw / base;
  const step = Math.max(1, (normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10) * base);
  return Math.ceil(maximum / step) * step;
}

function panelHeader(title, subtitle, extra = '') {
  return `<div class="panel-header"><div><h2 class="panel-title">${esc(title)}</h2><p class="panel-subtitle">${esc(subtitle)}</p></div>${extra}</div>`;
}

export function renderEventTrend(report, hours = 24) {
  const requestedHours = Number.isFinite(Number(hours)) ? Math.max(1, Math.min(720, Number(hours))) : 24;
  const end = dateValue(report?.as_of);
  const suppliedStart = dateValue(report?.since);
  if (!Number.isFinite(end) || !Number.isFinite(suppliedStart) || suppliedStart > end) {
    return `<section class="panel trend-panel">${panelHeader('变化趋势', '以商品快照的观察时间统计')}<div class="empty-inline">暂无有效报表时间，等待首批观察数据</div></section>`;
  }
  const requestedStart = end - requestedHours * HOUR;
  const start = Math.max(requestedStart, suppliedStart);
  const period = requestedHours > 48 ? DAY : HOUR;
  const first = Math.floor((start + SHANGHAI_OFFSET) / period) * period - SHANGHAI_OFFSET;
  // Do not append an empty future bucket when the cutoff is exactly on a boundary.
  const last = Math.floor((Math.max(start, end - 1) + SHANGHAI_OFFSET) / period) * period - SHANGHAI_OFFSET;
  const bucketCount = Math.floor((last - first) / period) + 1;
  const buckets = Array.from({ length: bucketCount }, (_, index) => ({ time: first + index * period, count: 0 }));
  let loadedInWindow = 0;
  const aggregated = Array.isArray(report?.event_buckets);
  const events = aggregated ? report.event_buckets : Array.isArray(report?.events) ? report.events : [];
  for (const event of events) {
    const observed = aggregated ? Math.max(start, Number(event.time)) : dateValue(event.observed_at);
    if (!Number.isFinite(observed) || observed < start || observed > end) continue;
    const index = Math.min(buckets.length - 1, Math.floor((observed - first) / period));
    if (index >= 0 && index < buckets.length) {
      const amount = aggregated ? count(event.count) : 1;
      buckets[index].count += amount;
      loadedInWindow += amount;
    }
  }
  const sameWindow = Math.abs(start - suppliedStart) < 1000;
  const summaryTotal = report?.summary?.events_in_window;
  const total = sameWindow && summaryTotal != null ? count(summaryTotal) : loadedInWindow;
  const truncated = !aggregated && Boolean(report?.events_truncated);
  const headline = truncated && !sameWindow ? '窗口内已加载变化' : '窗口内变化事件';
  const windowName = requestedHours === 24 ? '最近 24 小时' : requestedHours === 168 ? '最近 7 天' : requestedHours === 720 ? '最近 30 天' : `最近 ${requestedHours} 小时`;
  const shorterWindow = suppliedStart > requestedStart + 1000;
  const width = 1080, height = 160, left = 42, right = 22, top = 12, bottom = 29;
  const plotWidth = width - left - right, plotHeight = height - top - bottom;
  const scale = niceScale(Math.max(0, ...buckets.map(bucket => bucket.count)));
  const position = bucket => {
    const bucketStart = Math.max(start, bucket.time);
    const bucketEnd = Math.min(end, bucket.time + period);
    const midpoint = (bucketStart + bucketEnd) / 2;
    const x = end > start ? left + (midpoint - start) / (end - start) * plotWidth : left + plotWidth / 2;
    return { x, y: top + plotHeight - bucket.count / scale * plotHeight };
  };
  const points = buckets.map(position);
  const line = points.map((point, index) => `${index ? 'L' : 'M'}${point.x.toFixed(2)},${point.y.toFixed(2)}`).join(' ');
  const area = `${line} L${points.at(-1).x.toFixed(2)},${top + plotHeight} L${points[0].x.toFixed(2)},${top + plotHeight} Z`;
  const grid = Array.from({ length: 5 }, (_, index) => {
    const y = top + plotHeight * index / 4;
    const value=scale*(4-index)/4;
    return `<line x1="${left}" y1="${y}" x2="${width - right}" y2="${y}" stroke="#edf0f6"/><text x="${left - 10}" y="${y + 4}" text-anchor="end" fill="#7d8797" font-size="10">${Number.isInteger(value)?fmt(value):''}</text>`;
  }).join('');
  const xLabelCount = end === start ? 1 : 5;
  const xLabels = Array.from({ length: xLabelCount }, (_, index) => {
    const fraction = xLabelCount === 1 ? 0.5 : index / (xLabelCount - 1);
    const time = start + (end - start) * fraction;
    return `<text x="${left + plotWidth * fraction}" y="${height - 9}" text-anchor="${index === 0 && xLabelCount > 1 ? 'start' : index === xLabelCount - 1 && xLabelCount > 1 ? 'end' : 'middle'}" fill="#929bab" font-size="10">${dateLabel(time, false, period === DAY)}</text>`;
  }).join('');
  const hits = buckets.map((bucket, index) => {
    const point = points[index];
    const label = `${dateLabel(Math.max(start, bucket.time), true)} 至 ${dateLabel(Math.min(end, bucket.time + period), true)}：${bucket.count} 次变化${truncated ? '（已加载样本）' : ''}`;
    return `<circle cx="${point.x.toFixed(2)}" cy="${point.y.toFixed(2)}" r="7" fill="transparent" stroke="transparent"><title>${esc(label)}</title></circle>`;
  }).join('');
  const accessibility = `${windowName}变化趋势，按${period === DAY ? '天' : '小时'}统计。${truncated ? '图表仅含已加载样本。' : ''}${fmt(loadedInWindow)}次已加载变化；截止北京时间${dateLabel(end, true)}。`;
  const note = truncated
    ? `报表区间共 ${fmt(count(summaryTotal))} 次变化，已加载 ${fmt(events.length)} 条；图中显示所选窗口的 ${fmt(loadedInWindow)} 条样本，不能据此判断完整趋势。`
    : `按${period === DAY ? '天' : '小时'}汇总已采集变化；末段截至报表时间，未完成时段不能直接与完整时段比较。`;
  return `<section class="panel trend-panel">${panelHeader('变化趋势', '价格、库存与规格变化 · 北京时间', `<span class="trend-window">${esc(windowName)}</span>`)}<div class="trend-topline"><div class="trend-summary"><span class="trend-count">${esc(headline)}</span><strong class="trend-total">${fmt(total)}<small> 次</small></strong></div><div class="trend-legend"><i class="legend-dot" style="background:${BLUE}"></i><span>${truncated ? '已加载变化样本' : '变化事件'}</span></div></div><div class="trend-chart"><svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(accessibility)}"><title>${esc(accessibility)}</title>${grid}<path d="${area}" fill="#eaf0ff"/><path d="${line}" fill="none" stroke="${BLUE}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/>${hits}${xLabels}</svg></div><div class="trend-axis"><span>${dateLabel(start, true)}</span><span>截至 ${dateLabel(end, true)}</span></div><p class="chart-footnote trend-note">${esc(note)}${shorterWindow ? ' 当前报表覆盖时间短于所选窗口。' : ''}</p></section>`;
}

export function renderStockDonut(report) {
  const products = Array.isArray(report?.products) ? report.products : [];
  const states = [
    { label: '有货', key: 'sku_in_stock', color: GREEN },
    { label: '缺货', key: 'sku_out_of_stock', color: BLUE },
    { label: '未知 / 部分', key: 'sku_unknown', color: GREY },
  ].map(state => ({ ...state, value: report?.sku_totals ? count(report.sku_totals[state.key]) : products.reduce((sum, row) => sum + count(row[state.key]), 0) }));
  const total = states.reduce((sum, state) => sum + state.value, 0);
  const known = states[0].value + states[1].value;
  const radius = 68, circumference = 2 * Math.PI * radius;
  let accumulated = 0;
  const arcs = states.map(state => {
    const length = total ? state.value / total * circumference : 0;
    const result = length ? `<circle cx="100" cy="100" r="${radius}" fill="none" stroke="${state.color}" stroke-width="19" stroke-dasharray="${length.toFixed(3)} ${circumference.toFixed(3)}" stroke-dashoffset="${(-accumulated).toFixed(3)}" transform="rotate(-90 100 100)"><title>${esc(state.label)}：${fmt(state.value)} 个 SKU，${percent(state.value, total)}</title></circle>` : '';
    accumulated += length;
    return result;
  }).join('');
  const label = total ? `已观察 ${fmt(total)} 个 SKU：${states.map(state => `${state.label}${fmt(state.value)}个`).join('，')}。` : '暂无已观察 SKU 库存样本。';
  return `<section class="panel donut-panel">${panelHeader('SKU 库存状态', '当前有效快照中的规格库存')}<div class="donut-layout"><svg class="donut-svg" viewBox="0 0 200 200" role="img" aria-label="${esc(label)}"><title>${esc(label)}</title><circle cx="100" cy="100" r="${radius}" fill="none" stroke="#f0f2f7" stroke-width="19"/>${arcs}<text class="donut-center-value" x="100" y="98" text-anchor="middle" fill="#273247" font-size="26" font-weight="600">${fmt(total)}</text><text class="donut-center-label" x="100" y="120" text-anchor="middle" fill="#929bab" font-size="11">${total ? 'SKU 样本' : '等待采集'}</text></svg><div class="donut-legend">${states.map(state => `<div class="legend-row"><span><i class="legend-dot" style="background:${state.color}"></i>${esc(state.label)}</span><strong>${fmt(state.value)}</strong><small>${percent(state.value, total)}</small></div>`).join('')}<div class="donut-share"><span>状态明确</span><strong>${percent(known, total)}</strong></div></div></div><p class="chart-footnote">${total ? `已知状态 ${fmt(known)} 个，未知及部分状态 ${fmt(states[2].value)} 个；未知不计为缺货。` : '暂无 SKU 库存样本；商品库存状态不代替 SKU 观察。'}</p></section>`;
}

export function renderShopRanking(report) {
  const shops = (Array.isArray(report?.shops) ? report.shops : []).filter(shop => count(shop.sampled_products) > 0).sort((a, b) => count(b.sampled_products) - count(a.sampled_products) || String(a.name ?? '').localeCompare(String(b.name ?? ''), 'zh-CN')).slice(0, 4);
  const total = count(report?.summary?.products);
  return `<section class="panel ranking-panel">${panelHeader('店铺样本排行', '按已采集商品数量排序', '<a class="text-link" href="#shops">全部店铺 →</a>')}<div class="ranking-list">${shops.map((shop, index) => `<div class="ranking-row"><div class="ranking-top"><span class="rank-number">${String(index + 1).padStart(2, '0')}</span><span class="ranking-name" title="${esc(shop.name || shop.platform_shop_id)}">${esc(shop.name || shop.platform_shop_id || '未命名店铺')}</span><strong>${fmt(count(shop.sampled_products))}<small> 件</small></strong></div><div class="ranking-track"><div class="ranking-fill" style="width:${ratio(count(shop.sampled_products), total).toFixed(2)}%;background:${BLUE}"></div></div><span class="ranking-share">占已采集商品 ${percent(count(shop.sampled_products), total)}</span></div>`).join('') || '<div class="empty-inline">暂无已有商品采样的店铺</div>'}</div><p class="chart-footnote">仅比较已采集样本数量，不代表全店商品规模或销量。</p></section>`;
}

export function renderPriceDistribution(report) {
  const bins = Array.isArray(report?.price_histogram) ? report.price_histogram : [];
  const stats = report?.summary?.starting_price_stats;
  const total = bins.reduce((sum, bin) => sum + count(bin.count), 0);
  const width = 340, height = 174, left = 30, right = 8, top = 16, bottom = 31;
  const plotWidth = width - left - right, plotHeight = height - top - bottom;
  const scale = niceScale(Math.max(0, ...bins.map(bin => count(bin.count))));
  const grid = Array.from({ length: 5 }, (_, index) => {
    const y = top + plotHeight * index / 4;
    return `<line x1="${left}" y1="${y}" x2="${width - right}" y2="${y}" stroke="#edf0f6"/><text x="${left - 8}" y="${y + 3}" text-anchor="end" fill="#929bab" font-size="9">${fmt(scale * (4 - index) / 4)}</text>`;
  }).join('');
  const bars = bins.map((bin, index) => {
    const slot = plotWidth / bins.length;
    const barWidth = Math.min(44, slot * 0.55);
    const barHeight = count(bin.count) / scale * plotHeight;
    const x = left + slot * (index + 0.5);
    const y = top + plotHeight - barHeight;
    return `<rect x="${(x - barWidth / 2).toFixed(2)}" y="${y.toFixed(2)}" width="${barWidth.toFixed(2)}" height="${barHeight.toFixed(2)}" rx="3" fill="${BLUE}" opacity="${index % 2 ? '0.65' : '0.9'}"><title>${esc(bin.label)}：${fmt(count(bin.count))} 件商品</title></rect>${count(bin.count) ? `<text x="${x.toFixed(2)}" y="${Math.max(10, y - 7).toFixed(2)}" text-anchor="middle" fill="#68758c" font-size="9">${fmt(count(bin.count))}</text>` : ''}<text x="${x.toFixed(2)}" y="${height - 12}" text-anchor="middle" fill="#929bab" font-size="9">${esc(bin.label)}</text>`;
  }).join('');
  const accessible = total ? `公开起价分布：${bins.map(bin => `${bin.label} ${fmt(count(bin.count))}件`).join('，')}。` : '暂无已知公开起价样本。';
  return `<section class="panel distribution-panel">${panelHeader('公开起价分布', '按商品价格下限统计 · 非成交价')}<div class="distribution-meta"><div class="distribution-stat"><span>中位起价</span><strong>${esc(money(stats?.median, report?.currency))}</strong></div><div class="distribution-stat"><span>最低</span><strong>${esc(money(stats?.min, report?.currency))}</strong></div><div class="distribution-stat"><span>最高</span><strong>${esc(money(stats?.max, report?.currency))}</strong></div></div><div class="distribution-chart"><svg class="distribution-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(accessible)}"><title>${esc(accessible)}</title>${grid}${bars}${total ? '' : `<text x="${width / 2}" y="${top + plotHeight / 2}" text-anchor="middle" fill="#929bab" font-size="12">暂无已知价格样本</text>`}</svg></div><p class="chart-footnote">${fmt(count(stats?.known_count))} 件已知起价样本；From 起价与区间下限不等于统一单价。</p></section>`;
}

export function renderSampleMetrics(report) {
  const summary = report?.summary;
  const products = count(summary?.products);
  const details = count(summary?.historical_detail_products);
  const observed = count(summary?.observed_skus);
  const known = count(summary?.known_stock_skus);
  const kinds = summary?.price_kinds || {};
  const priceKinds = [
    { key: 'exact', label: '单价', color: BLUE },
    { key: 'range', label: '区间', color: '#819ef4' },
    { key: 'starting_price', label: 'From 起价', color: '#b6c7f9' },
    { key: 'unknown', label: '未知', color: GREY },
  ];
  const knownPrices = count(kinds.exact) + count(kinds.range) + count(kinds.starting_price);
  const bar = (part, total, color) => `<div class="metric-bar" role="img" aria-label="${esc(total ? `${fmt(part)} / ${fmt(total)}，${percent(part, total)}` : '暂无样本')}"><span style="width:${ratio(part, total).toFixed(2)}%;background:${color}"></span></div>`;
  return `<div class="metric-chart-grid"><section class="panel metric-chart"><div class="metric-top"><h2 class="panel-title">详情快照覆盖</h2><span class="metric-indicator">商品</span></div><strong class="metric-value">${percent(details, products)}</strong>${bar(details, products, BLUE)}<p class="metric-note">${products ? `${fmt(details)} / ${fmt(products)} 件已保存有效详情快照` : '暂无商品采样'}</p></section><section class="panel metric-chart"><div class="metric-top"><h2 class="panel-title">SKU 状态明确率</h2><span class="metric-indicator">库存</span></div><strong class="metric-value">${percent(known, observed)}</strong>${bar(known, observed, GREEN)}<p class="metric-note">${observed ? `${fmt(known)} / ${fmt(observed)} 个 SKU 状态明确` : '暂无 SKU 库存观察'}</p></section><section class="panel metric-chart"><div class="metric-top"><h2 class="panel-title">公开价格覆盖</h2><span class="metric-indicator">价格</span></div><strong class="metric-value">${percent(knownPrices, products)}</strong><div class="metric-bar metric-stacked" role="img" aria-label="${esc(products ? priceKinds.map(kind => `${kind.label} ${fmt(count(kinds[kind.key]))}件`).join('，') : '暂无价格样本')}">${priceKinds.map(kind => `<span style="width:${ratio(count(kinds[kind.key]), products).toFixed(2)}%;background:${kind.color}" title="${esc(kind.label)}：${fmt(count(kinds[kind.key]))} 件"></span>`).join('')}</div><p class="metric-note">${products ? `单价 ${fmt(count(kinds.exact))} · 区间 ${fmt(count(kinds.range))} · 起价 ${fmt(count(kinds.starting_price))} · 未知 ${fmt(count(kinds.unknown))}` : '暂无商品价格样本'}</p></section></div>`;
}
