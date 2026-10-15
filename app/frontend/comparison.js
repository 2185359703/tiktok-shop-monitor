import { request, collect, labels, money, formatDate } from './api.js';

const COLORS = ['#4b68d6', '#259985', '#d39445', '#9564bf'];
const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, character => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[character]));

function knownAmount(value) {
  if (value === null || value === undefined || typeof value === 'string' && !value.trim()) return null;
  if (typeof value !== 'number' && typeof value !== 'string') return null;
  const amount = Number(value);
  return Number.isFinite(amount) && amount >= 0 ? amount : null;
}

function knownCount(value) {
  if (value === null || value === undefined || value === '') return null;
  const count = Number(value);
  return Number.isSafeInteger(count) && count >= 0 ? count : null;
}

function timeOf(value) {
  if (typeof value !== 'string' || !value.trim()) return NaN;
  let normalized = value.trim().replace(' ', 'T');
  if (!/(?:Z|[+-]\d{2}:?\d{2})$/i.test(normalized)) normalized += 'Z';
  return Date.parse(normalized);
}

function productName(product) {
  return product.title || `商品 ${product.platform_product_id || product.id}`;
}

function pricingContext(product) {
  return product.pricing_context === 'public_listed' ? '公开标价' : product.pricing_context || '价格口径未知';
}

function sameBasis(left, right) {
  return Boolean(left.currency && left.pricing_context && right.currency && right.pricing_context &&
    left.currency === right.currency && left.pricing_context === right.pricing_context);
}

function priceCell(product) {
  const low = knownAmount(product.price_min), high = knownAmount(product.price_max);
  if (low === null) return '<span class="muted">公开价格未知</span>';
  if (!product.currency) return '<span class="muted">币种未知，暂不展示价格</span>';
  let amount, kind;
  if (high === null) {
    amount = money(low, product.currency);
    kind = '公开起价';
  } else if (high === low) {
    amount = money(low, product.currency);
    kind = '公开单价';
  } else if (high > low) {
    amount = `${money(low, product.currency)} – ${money(high, product.currency)}`;
    kind = '公开价格区间';
  } else {
    return '<span class="muted">价格区间异常，暂不对照</span>';
  }
  return `<strong>${escapeHtml(amount)}</strong><span class="cell-secondary">${escapeHtml(kind)}</span>`;
}

function startingDifference(product, baseline, index) {
  const value = knownAmount(product.price_min), base = knownAmount(baseline.price_min);
  if (!sameBasis(product, baseline)) return '<span class="muted">币种或价格口径不同</span>';
  if (value === null || base === null) return '<span class="muted">起价未知，无法计算</span>';
  const difference = value - base;
  const amount = `${difference > 0 ? '+' : difference < 0 ? '−' : ''}${money(Math.abs(difference), product.currency)}`;
  return `<strong>${escapeHtml(amount)}</strong>${index === 0 ? '<span class="cell-secondary">基准商品</span>' : ''}`;
}

function readFailure(result) {
  return `<span class="muted">读取失败</span><span class="cell-secondary">${escapeHtml(result.reason?.message || '请稍后重试。')}</span>`;
}

function valuesCell(result, key) {
  if (result.status !== 'fulfilled') return readFailure(result);
  const values = Array.isArray(result.value?.[key]) ? result.value[key].filter(value => value !== null && value !== '') : [];
  return values.length ? escapeHtml(values.map(String).join('、')) : '<span class="muted">暂无公开信息</span>';
}

function skuCountCell(product) {
  const count = knownCount(product.sku_count);
  if (!product.snapshot_id || count === null) return '<span class="muted">尚无详情观察</span>';
  return `${escapeHtml(new Intl.NumberFormat('zh-CN').format(count))}<span class="cell-secondary">报告所用快照中的规格</span>`;
}

function variantReadCell(result) {
  if (result.status !== 'fulfilled') return readFailure(result);
  const { items, complete } = result.value;
  if (!items.length) return '<span class="muted">未保存 SKU 档案</span>';
  const latest = items.filter(variant => variant.seen_in_latest_detail === true).length;
  const latestKnown = items.every(variant => typeof variant.seen_in_latest_detail === 'boolean');
  return `${escapeHtml(new Intl.NumberFormat('zh-CN').format(items.length))} 条已载入` +
    `<span class="cell-secondary">${complete ? latestKnown ? `最新详情出现 ${escapeHtml(latest)} 条` : '最新详情覆盖状态未知' : '尚有更多规格，数量未完整载入'}</span>`;
}

function historyReadCell(result) {
  if (result.status !== 'fulfilled') return readFailure(result);
  const { items, complete } = result.value;
  if (!items.length) return '<span class="muted">暂无历史快照</span>';
  return `${escapeHtml(new Intl.NumberFormat('zh-CN').format(items.length))} 次已载入` +
    `<span class="cell-secondary">${complete ? '按实际观察时间绘制' : '仅载入前 500 次，尚有更多历史'}</span>`;
}

function renderTable(entries) {
  const baseline = entries[0].product;
  const rows = [
    ['名称', entry => escapeHtml(productName(entry.product))],
    ['店铺', entry => escapeHtml(entry.product.shop_name || '店铺名称未知')],
    ['公开类目', entry => escapeHtml(entry.product.category_basis === 'platform_category' &&
      entry.product.category !== 'unknown' ? entry.product.category : '暂无公开类目')],
    ['当前公开价', entry => priceCell(entry.product)],
    ['币种 / 价格口径', entry => escapeHtml(`${entry.product.currency || '币种未知'} · ${pricingContext(entry.product)}`)],
    ['公开起价差', (entry, index) => startingDifference(entry.product, baseline, index)],
    ['库存', entry => escapeHtml(labels.stockStatuses[entry.product.stock_status] || '库存未知')],
    ['最近观察', entry => escapeHtml(formatDate(entry.product.observed_at))],
    ['SKU 数量', entry => skuCountCell(entry.product)],
    ['最新档案颜色', entry => valuesCell(entry.detail, 'colors')],
    ['最新档案尺码', entry => valuesCell(entry.detail, 'sizes')],
    ['SKU 档案', entry => variantReadCell(entry.variants)],
    ['历史快照', entry => historyReadCell(entry.snapshots)],
  ];
  const headers = entries.map((entry, index) => `<th scope="col"><div class="comparison-product">` +
    `<span class="comparison-color" style="background:${COLORS[index]}"></span>` +
    `<span>商品 ${index + 1}${index === 0 ? ' · 基准' : ''}</span>` +
    `<button class="product-title wrap-title" data-action="product" data-id="${escapeHtml(entry.product.id)}">${escapeHtml(productName(entry.product))}</button>` +
    `<button class="row-link" data-action="product" data-id="${escapeHtml(entry.product.id)}">查看单品 →</button></div></th>`).join('');
  return `<div class="table-wrap"><table class="comparison-table"><caption class="sr-only">所选商品的公开观察对照，首件商品为起价基准</caption>` +
    `<thead><tr><th scope="col">对照项</th>${headers}</tr></thead><tbody>` +
    rows.map(([label, render]) => `<tr><th scope="row">${escapeHtml(label)}</th>${entries.map((entry, index) => `<td>${render(entry, index)}</td>`).join('')}</tr>`).join('') +
    '</tbody></table></div>';
}

function sampleSeries(entry, index) {
  if (entry.snapshots.status !== 'fulfilled') {
    return { entry, index, points: [], note: '历史读取失败，未绘制该商品。' };
  }
  const rows = entry.snapshots.value.items;
  if (!rows.length) return { entry, index, points: [], note: '暂无历史快照。' };
  const points = [];
  let failed = 0, otherBasis = 0, unknown = 0;
  rows.forEach(snapshot => {
    if (snapshot.source_status !== 200) { failed += 1; return; }
    if (!sameBasis(snapshot, entry.product)) { otherBasis += 1; return; }
    const value = knownAmount(snapshot.price_min), time = timeOf(snapshot.captured_at);
    if (value === null || !Number.isFinite(time)) { unknown += 1; return; }
    points.push({ value, time, snapshot });
  });
  points.sort((left, right) => left.time - right.time);
  const notes = [];
  if (!points.length) notes.push('暂无与当前价格口径一致的有效起价历史。');
  if (failed) notes.push(`${failed} 次未成功的来源观察未绘制。`);
  if (otherBasis) notes.push(`${otherBasis} 次币种或价格口径不同的观察未绘制。`);
  if (unknown) notes.push(`${unknown} 次价格或观察时间未知的快照未绘制。`);
  if (!entry.snapshots.value.complete) notes.push('仅读取前 500 次快照，尚有更多历史。');
  return { entry, index, points, note: notes.join('') };
}

function renderHistory(entries) {
  const series = entries.map(sampleSeries);
  const notes = series.filter(item => item.note).map(item => `<p class="comparison-note"><strong>商品 ${item.index + 1}：</strong>${escapeHtml(item.note)}</p>`).join('');
  if (entries.some(entry => !sameBasis(entry.product, entries[0].product))) {
    return '<div class="empty-inline">所选商品的币种或价格口径不同，无法叠加价格轨迹。可打开单品查看各自历史。</div>' + notes;
  }
  const drawable = series.filter(item => item.points.length);
  if (!drawable.length) return '<div class="empty-inline">暂无可对照的公开起价历史。</div>' + notes;
  if (drawable.length === 1) {
    return `<div class="empty-inline">仅商品 ${drawable[0].index + 1} 有有效公开起价历史，暂无法叠加对照。可打开单品查看。</div>` + notes;
  }
  const points = drawable.flatMap(item => item.points);
  const minTime = Math.min(...points.map(point => point.time)), maxTime = Math.max(...points.map(point => point.time));
  const minValue = Math.min(...points.map(point => point.value)), maxValue = Math.max(...points.map(point => point.value));
  const padding = minValue === maxValue ? Math.max(1, maxValue * .08) : (maxValue - minValue) * .12;
  const floor = Math.max(0, minValue - padding), ceiling = maxValue + padding;
  const left = 74, right = 704, top = 24, bottom = 212;
  const x = time => minTime === maxTime ? (left + right) / 2 : left + (time - minTime) / (maxTime - minTime) * (right - left);
  const y = value => bottom - (value - floor) / (ceiling - floor) * (bottom - top);
  const coordinate = value => Number(value.toFixed(2));
  const currency = entries[0].product.currency;
  const ticks = Array.from({ length: 4 }, (_, index) => {
    const value = floor + (ceiling - floor) * index / 3, position = coordinate(y(value));
    return `<line x1="${left}" x2="${right}" y1="${position}" y2="${position}" stroke="#e8edf3" stroke-dasharray="3 4"/>` +
      `<text x="${left - 9}" y="${coordinate(position + 4)}" text-anchor="end" font-size="10" fill="#8592a3">${escapeHtml(money(value, currency))}</text>`;
  }).join('');
  const paths = drawable.map(item => {
    const color = COLORS[item.index];
    const positions = item.points.map(point => `${coordinate(x(point.time))},${coordinate(y(point.value))}`).join(' ');
    const line = item.points.length > 1 ? `<polyline points="${positions}" fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round"/>` : '';
    const dots = item.points.map(point => `<circle cx="${coordinate(x(point.time))}" cy="${coordinate(y(point.value))}" r="3.5" fill="${color}" stroke="white" stroke-width="1" tabindex="0">` +
      `<title>${escapeHtml(productName(item.entry.product))} · ${escapeHtml(formatDate(point.snapshot.captured_at))} · 公开起价 ${escapeHtml(money(point.value, currency))}</title></circle>`).join('');
    return `<g>${line}${dots}</g>`;
  }).join('');
  const description = `所选商品的公开价格下限历史，共 ${points.length} 个实际采样点。横轴为实际观察时间，纵轴币种 ${currency}。`;
  return `<div class="comparison-chart"><svg viewBox="0 0 730 250" role="img" aria-label="${escapeHtml(description)}"><title>${escapeHtml(description)}</title>` +
    `${ticks}${paths}<text x="${left}" y="239" font-size="10" fill="#8592a3">${escapeHtml(formatDate(new Date(minTime)))}</text>` +
    `<text x="${right}" y="239" text-anchor="end" font-size="10" fill="#8592a3">${escapeHtml(formatDate(new Date(maxTime)))}</text></svg></div>` +
    `<div class="comparison-legend">${drawable.map(item => `<span><i class="comparison-color" style="background:${COLORS[item.index]}"></i>${escapeHtml(productName(item.entry.product))}</span>`).join('')}</div>` +
    '<p class="comparison-note">圆点表示实际观察；横轴保留采样间隔，直线仅连接采样点，不代表期间持续价格。</p>' + notes;
}

/** Read-only comparison of 2–4 selected report products. Report observations stay intact. */
export async function buildComparison(products, { signal } = {}) {
  if (!Array.isArray(products) || products.length < 2 || products.length > 4 ||
      products.some(product => !product || typeof product.id !== 'string' || !/^\d+$/.test(product.id)) ||
      new Set(products.map(product => product.id)).size !== products.length) {
    return '<h2 id="dialog-title">商品对照</h2><p class="comparison-note">请选择 2–4 件不同的商品进行对照。</p>';
  }
  const entries = await Promise.all(products.map(async product => {
    const path = `products/${encodeURIComponent(product.id)}`;
    const [detail, snapshots, variants] = await Promise.allSettled([
      request(path, { signal }),
      collect(`${path}/snapshots`, {}, { signal, maxPages: 5 }),
      collect(`${path}/variants`, {}, { signal, maxPages: 5 }),
    ]);
    return { product, detail, snapshots, variants };
  }));
  if (signal?.aborted) throw signal.reason || new DOMException('读取已取消。', 'AbortError');
  return '<h2 id="dialog-title">商品对照</h2>' +
    `<p class="detail-subtitle">${entries.length} 件已选商品 · 首件为起价基准</p>` +
    '<p class="comparison-note">公开起价差仅对照价格下限。颜色、尺码及规格需另行确认，不自动视为同规格商品。</p>' +
    renderTable(entries) +
    '<div class="detail-section"><h3>公开起价轨迹</h3>' + renderHistory(entries) + '</div>' +
    '<p class="comparison-note">名称、类目、公开价、库存和 SKU 数量来自当前报告观察；颜色与尺码来自本次读取的最新档案。</p>';
}
