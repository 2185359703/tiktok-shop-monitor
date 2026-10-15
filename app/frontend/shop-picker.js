const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
const fold = value => String(value ?? '').normalize('NFKC').toLocaleLowerCase().trim();
const svg = (name, className = '') => `<svg class="shop-picker-icon ${className}" viewBox="0 0 20 20" aria-hidden="true">${{
  shop: '<path d="M3 8h14M4 8v9h12V8M3 8l1.5-5h11L17 8M8 17v-5h4v5M3 8a2.33 2.33 0 0 0 4.67 0 2.33 2.33 0 0 0 4.66 0A2.33 2.33 0 0 0 17 8"/>',
  search: '<circle cx="8.5" cy="8.5" r="5.5"/><path d="m13 13 4 4"/>',
  chevron: '<path d="m6 8 4 4 4-4"/>',
  check: '<path d="m5 10 3 3 7-7"/>',
}[name]}</svg>`;

function shopRecords(shops) {
  const unique = new Map();
  for (const shop of shops || []) {
    const id = String(shop.id ?? '');
    if (!id || unique.has(id)) continue;
    const name = String(shop.name || shop.canonical_name || shop.platform_shop_id || id);
    unique.set(id, { id, name, platformId: String(shop.platform_shop_id ?? ''), search: fold([name, shop.canonical_name, shop.platform_shop_id, id].filter(Boolean).join(' ')) });
  }
  return [...unique.values()].sort((a, b) => a.name.localeCompare(b.name, 'zh-CN', { sensitivity: 'base', numeric: true }));
}

export function renderShopPicker(shops, selectedId) {
  const id = String(selectedId ?? '');
  const selected = shopRecords(shops).find(shop => shop.id === id);
  const label = id ? selected?.name || '所选店铺' : '全部店铺';
  return `<div class="shop-picker"><button type="button" class="shop-picker-trigger" data-shop-picker-trigger data-shop-picker-value="${esc(id)}" aria-haspopup="dialog" aria-expanded="false" aria-label="店铺筛选：${esc(label)}" title="${esc(label)}">${svg('shop')}<span class="shop-picker-value">${esc(label)}</span>${svg('chevron', 'shop-picker-chevron')}</button></div>`;
}

let uninstallPicker;

export function installShopPicker({ getShops, getValue, onChange }) {
  uninstallPicker?.();
  let trigger = null;
  let popup = null;
  let search = null;
  let list = null;
  let scrollArea = null;
  let shops = [];
  let matches = [];
  let activeIndex = -1;
  let frame = null;
  let openedValue = '';
  const listeners = [];
  const listen = (target, type, handler, options) => {
    target.addEventListener(type, handler, options);
    listeners.push(() => target.removeEventListener(type, handler, options));
  };
  const selectedValue = () => String(getValue() ?? '');

  function close(restoreFocus = false) {
    const previousTrigger = trigger;
    if (frame != null) cancelAnimationFrame(frame);
    frame = null;
    popup?.remove();
    popup = search = list = scrollArea = null;
    trigger = null;
    activeIndex = -1;
    previousTrigger?.setAttribute('aria-expanded', 'false');
    previousTrigger?.removeAttribute('aria-controls');
    if (restoreFocus && previousTrigger?.isConnected) previousTrigger.focus({ preventScroll: true });
  }

  function position() {
    if (!popup || !trigger) return;
    if (!trigger.isConnected) return close();
    const viewport = window.visualViewport;
    const leftEdge = (viewport?.offsetLeft || 0) + 12;
    const topEdge = (viewport?.offsetTop || 0) + 12;
    const viewportWidth = viewport?.width || window.innerWidth;
    const viewportHeight = viewport?.height || window.innerHeight;
    const rightEdge = leftEdge + viewportWidth - 24;
    const bottomEdge = topEdge + viewportHeight - 24;
    const rect = trigger.getBoundingClientRect();
    const width = Math.min(300, viewportWidth - 24);
    const below = bottomEdge - rect.bottom - 6;
    const above = rect.top - topEdge - 6;
    const placeBelow = below >= 280 || below >= above;
    const availableHeight = Math.min(360, Math.max(120, placeBelow ? below : above), viewportHeight - 24);
    popup.style.width = `${width}px`;
    popup.style.maxHeight = `${availableHeight}px`;
    popup.style.left = `${Math.max(leftEdge, Math.min(rect.left, rightEdge - width))}px`;
    const height = popup.getBoundingClientRect().height;
    const preferredTop = placeBelow ? rect.bottom + 6 : rect.top - height - 6;
    popup.style.top = `${Math.max(topEdge, Math.min(preferredTop, bottomEdge - height))}px`;
  }

  function schedulePosition() {
    if (!popup || frame != null) return;
    frame = requestAnimationFrame(() => { frame = null; position(); });
  }

  function setActive(index, reveal = true) {
    if (!popup) return;
    const options = [...list.querySelectorAll('[data-shop-picker-option]')];
    activeIndex = Math.max(-1, Math.min(index, options.length - 1));
    options.forEach((option, i) => option.classList.toggle('is-active', i === activeIndex));
    const active = options[activeIndex];
    if (active) search.setAttribute('aria-activedescendant', active.id);
    else search.removeAttribute('aria-activedescendant');
    if (reveal && active && scrollArea.contains(active)) {
      const top = active.offsetTop;
      const bottom = top + active.offsetHeight;
      if (top < scrollArea.scrollTop) scrollArea.scrollTop = top;
      else if (bottom > scrollArea.scrollTop + scrollArea.clientHeight) scrollArea.scrollTop = bottom - scrollArea.clientHeight;
    }
  }

  function option(shop, index, all = false) {
    const selected = shop.id === selectedValue();
    const platformTitle = shop.platformId ? ` · ${shop.platformId}` : '';
    return `<button type="button" role="option" tabindex="-1" id="shop-picker-option-${index}" class="shop-picker-option${all ? ' shop-picker-all' : ''}" data-shop-picker-option="${esc(shop.id)}" aria-selected="${selected}" title="${esc(shop.name + platformTitle)}">${all ? svg('shop') : ''}<span class="shop-picker-option-name">${esc(shop.name)}</span>${all ? `<span class="shop-picker-count" aria-hidden="true">${shops.length}</span>` : ''}${selected ? svg('check', 'shop-picker-check') : '<span class="shop-picker-check-space" aria-hidden="true"></span>'}</button>`;
  }

  function renderOptions() {
    const query = fold(search.value);
    matches = query ? shops.filter(shop => shop.search.includes(query)) : shops;
    list.innerHTML = `${option({ id: '', name: '全部店铺' }, 0, true)}<div class="shop-picker-options">${matches.length ? matches.map((shop, index) => option(shop, index + 1)).join('') : '<div class="shop-picker-empty" role="status">未找到店铺</div>'}</div>`;
    scrollArea = list.querySelector('.shop-picker-options');
    const selectedIndex = matches.findIndex(shop => shop.id === selectedValue());
    position();
    setActive(query ? (matches.length ? 1 : -1) : selectedIndex + 1);
  }

  function open(button, direction) {
    close();
    trigger = button;
    openedValue = selectedValue();
    shops = shopRecords(getShops());
    popup = document.createElement('div');
    popup.id = 'shop-picker-popover';
    popup.className = 'shop-picker-popover';
    popup.setAttribute('role', 'dialog');
    popup.setAttribute('aria-label', '选择店铺');
    popup.innerHTML = `<div class="shop-picker-search">${svg('search')}<input type="search" class="shop-picker-search-input" role="combobox" aria-label="搜索店铺名称或ID" aria-autocomplete="list" aria-expanded="true" aria-controls="shop-picker-list" placeholder="搜索店铺名称或 ID" maxlength="200" autocomplete="off" spellcheck="false"></div><div class="shop-picker-list" id="shop-picker-list" role="listbox" aria-label="店铺"></div>`;
    document.body.appendChild(popup);
    trigger.setAttribute('aria-expanded', 'true');
    trigger.setAttribute('aria-controls', popup.id);
    search = popup.querySelector('input');
    list = popup.querySelector('.shop-picker-list');
    renderOptions();
    if (direction === 'last' && !selectedValue()) setActive(matches.length);
    search.focus({ preventScroll: true });
  }

  function choose(id) {
    const changed = id !== selectedValue();
    const selectedName = id ? shops.find(shop => shop.id === id)?.name || '所选店铺' : '全部店铺';
    const previousTrigger = trigger;
    if (previousTrigger) {
      previousTrigger.dataset.shopPickerValue = id;
      previousTrigger.querySelector('.shop-picker-value').textContent = selectedName;
      previousTrigger.setAttribute('aria-label', `店铺筛选：${selectedName}`);
      previousTrigger.title = selectedName;
    }
    close(true);
    if (changed) {
      onChange(id);
      // The workbench may rerender synchronously when its filter changes.
      queueMicrotask(() => {
        if (!previousTrigger?.isConnected && document.activeElement === document.body) {
          document.querySelector('[data-shop-picker-trigger]')?.focus({ preventScroll: true });
        }
      });
    }
  }

  listen(document, 'click', event => {
    const target = event.target instanceof Element ? event.target : null;
    const button = target?.closest('[data-shop-picker-trigger]');
    if (button) {
      if (popup && trigger === button) close(true);
      else open(button);
      return;
    }
    const item = target?.closest('[data-shop-picker-option]');
    if (item && popup?.contains(item)) choose(item.dataset.shopPickerOption);
  });
  listen(document, 'pointerdown', event => {
    if (popup && !popup.contains(event.target) && !trigger.contains(event.target)) close();
  });
  listen(document, 'input', event => {
    if (event.target === search) renderOptions();
  });
  listen(document, 'pointermove', event => {
    if (!popup || event.pointerType === 'touch') return;
    const target = event.target instanceof Element ? event.target : null;
    const item = target?.closest('[data-shop-picker-option]');
    if (item && list.contains(item)) setActive([...list.querySelectorAll('[data-shop-picker-option]')].indexOf(item), false);
  });
  listen(document, 'keydown', event => {
    if (event.isComposing) return;
    const target = event.target instanceof Element ? event.target : null;
    if (!popup) {
      const button = target?.closest('[data-shop-picker-trigger]');
      if (button && ['ArrowDown', 'ArrowUp'].includes(event.key)) {
        event.preventDefault();
        event.stopPropagation();
        open(button, event.key === 'ArrowUp' ? 'last' : 'first');
      }
      return;
    }
    if (!popup.contains(target) && target !== trigger) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      close(true);
    } else if (event.key === 'Tab') {
      close(true);
    } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      event.stopPropagation();
      const count = matches.length + 1;
      const step = event.key === 'ArrowDown' ? 1 : -1;
      setActive(activeIndex < 0 ? (step > 0 ? 0 : count - 1) : (activeIndex + step + count) % count);
    } else if (event.key === 'Enter') {
      event.preventDefault();
      event.stopPropagation();
      if (activeIndex >= 0) choose(activeIndex === 0 ? '' : matches[activeIndex - 1].id);
    }
  }, true);
  listen(document, 'focusin', event => {
    if (popup && !popup.contains(event.target) && event.target !== trigger) close();
  });
  listen(window, 'resize', schedulePosition);
  listen(window, 'scroll', schedulePosition, true);
  listen(window, 'hashchange', () => close());
  if (window.visualViewport) {
    listen(window.visualViewport, 'resize', schedulePosition);
    listen(window.visualViewport, 'scroll', schedulePosition);
  }
  const observer = new MutationObserver(() => {
    if (!popup) return;
    if (selectedValue() !== openedValue) return close();
    if (trigger?.isConnected) return;
    const replacement = document.querySelector('[data-shop-picker-trigger]');
    if (!replacement) return close();
    trigger = replacement;
    trigger.setAttribute('aria-expanded', 'true');
    trigger.setAttribute('aria-controls', popup.id);
    const activeId = activeIndex < 0 ? null : activeIndex === 0 ? '' : matches[activeIndex - 1]?.id;
    const previousScroll = scrollArea.scrollTop;
    shops = shopRecords(getShops());
    renderOptions();
    setActive(activeId === null ? -1 : activeId === '' ? 0 : matches.findIndex(shop => shop.id === activeId) + 1, false);
    scrollArea.scrollTop = previousScroll;
    position();
  });
  observer.observe(document.body, { childList: true, subtree: true });
  uninstallPicker = () => {
    close();
    listeners.forEach(remove => remove());
    observer.disconnect();
    uninstallPicker = undefined;
  };
  return uninstallPicker;
}
