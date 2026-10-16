/** Bounded session memory. No localStorage, credentials, or report bodies. */
export function createQueryCache(fetcher, {maxEntries = 50, maxBytes = 8 * 1024 * 1024, clock = Date.now} = {}) {
  const entries = new Map(), flights = new Map();
  let bytes = 0;
  const clone = value => structuredClone(value);
  const keyOf = path => { const [base, query = ''] = path.split('?'); const params = new URLSearchParams(query); params.sort(); return `${base}?${params}`; };
  const forget = key => { const old = entries.get(key); if(old)bytes -= old.bytes; entries.delete(key); };
  function peek(path, maxAge = 120000) {
    const key = keyOf(path), entry = entries.get(key);
    if(!entry || clock() - entry.time > maxAge)return null;
    entries.delete(key); entries.set(key, entry);
    return clone(entry.data);
  }
  function store(key, data) {
    if(data?._meta?.status === 'pending')return;
    const size = new TextEncoder().encode(JSON.stringify(data)).length;
    if(size > maxBytes)return;
    forget(key); entries.set(key, {data: clone(data), bytes: size, time: clock()}); bytes += size;
    while(entries.size > maxEntries || bytes > maxBytes)forget(entries.keys().next().value);
  }
  function load(path, {signal, maxAge = 15000, force = false, timeout = 8000} = {}) {
    if(signal?.aborted)return Promise.reject(signal.reason || new DOMException('Aborted', 'AbortError'));
    const key = keyOf(path), cached = !force && peek(path, maxAge);
    if(cached && cached._meta?.status !== 'stale')return Promise.resolve(cached);
    let flight = flights.get(key);
    if(!flight) {
      const controller = new AbortController();
      flight = {controller, users: 0}; flights.set(key, flight);
      flight.promise = Promise.resolve().then(()=>fetcher(path, {signal:controller.signal, timeout})).then(data=>{if(!controller.signal.aborted)store(key,data);return data;})
        .finally(()=>{if(flights.get(key)===flight)flights.delete(key);});
      flight.promise.catch(()=>{});
    }
    ++flight.users;
    return new Promise((resolve,reject)=>{
      let done=false;
      const finish=(fn,value)=>{if(done)return;done=true;signal?.removeEventListener('abort',abort);--flight.users;fn(value);};
      const abort=()=>{finish(reject,signal.reason||new DOMException('Aborted','AbortError'));if(!flight.users){flights.delete(key);flight.controller.abort();}};
      signal?.addEventListener('abort',abort,{once:true});
      flight.promise.then(data=>finish(resolve,clone(data)),error=>finish(reject,error));
    });
  }
  function invalidate(prefix = '') {
    for(const key of entries.keys())if(key.startsWith(prefix))forget(key);
    for(const [key,flight] of flights)if(key.startsWith(prefix)){flights.delete(key);flight.controller.abort();}
  }
  return {peek, load, invalidate};
}

export function abortableDelay(ms, signal) {
  return new Promise((resolve,reject)=>{
    if(signal?.aborted){reject(signal.reason);return;}
    const abort=()=>{clearTimeout(timer);reject(signal.reason||new DOMException('Aborted','AbortError'));};
    const timer=setTimeout(()=>{signal?.removeEventListener('abort',abort);resolve();},ms);
    signal?.addEventListener('abort',abort,{once:true});
  });
}

export function readSnapshotCache(entry, {now=Date.now(), maxAge=120000, freshFor=60000, staleFor=900000}={}) {
  if(!entry?.data || entry.data._meta?.status==='pending')return {data:null,fresh:false};
  const meta=entry.data._meta;
  const stamp=meta?Date.parse(meta.snapshot_at||meta.generated_at):NaN;
  const generated=meta?Date.parse(meta.generated_at||meta.snapshot_at):NaN;
  const fresh=now-entry.savedAt<maxAge&&(!meta||(meta.status==='fresh'&&Number.isFinite(stamp)&&now-stamp<freshFor));
  if(fresh||!meta)return {data:entry.data,fresh};
  return {fresh:false,data:{...entry.data,_meta:{...meta,status:'stale',
    expired:!!meta.expired||!Number.isFinite(generated)||now-generated>=staleFor}}};
}
