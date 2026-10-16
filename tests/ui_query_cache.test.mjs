import test from 'node:test';
import assert from 'node:assert/strict';
import {createQueryCache,readSnapshotCache} from '../app/frontend/query-cache.js';

test('revisits show expired snapshots without extending their source freshness',()=>{
  const now=Date.parse('2026-10-16T12:00:00Z');
  const data={value:42,_meta:{status:'fresh',snapshot_at:new Date(now-61000).toISOString(),expired:false}};
  const restored=readSnapshotCache({data,savedAt:now-1000},{now});
  assert.equal(restored.fresh,false);assert.equal(restored.data.value,42);
  assert.equal(restored.data._meta.status,'stale');assert.equal(restored.data._meta.expired,false);
  assert.equal(data._meta.status,'fresh');
  assert.equal(readSnapshotCache({data,savedAt:now},{now:now+900000}).data._meta.expired,true);
});

test('pending snapshots are never restored as empty results',()=>{
  assert.deepEqual(readSnapshotCache({data:{_meta:{status:'pending'}},savedAt:Date.now()}),{data:null,fresh:false});
});

test('deduplicates normalized queries and keeps cached values isolated',async()=>{
  let calls=0;
  const cache=createQueryCache(async()=>{calls++;return {items:[{id:'1'}]};});
  const [a,b]=await Promise.all([cache.load('/items?b=2&a=1'),cache.load('/items?a=1&b=2')]);
  a.items[0].id='changed';
  assert.equal(calls,1);assert.equal(b.items[0].id,'1');assert.equal(cache.peek('/items?a=1&b=2').items[0].id,'1');
});

test('one subscriber leaving does not cancel another subscriber',async()=>{
  let finish,sharedSignal;
  const cache=createQueryCache((path,{signal})=>{sharedSignal=signal;return new Promise(resolve=>finish=resolve);});
  const left=new AbortController(),right=new AbortController();
  const a=cache.load('/items',{signal:left.signal}),b=cache.load('/items',{signal:right.signal});
  await Promise.resolve();left.abort();
  await assert.rejects(a,{name:'AbortError'});assert.equal(sharedSignal.aborted,false);
  finish({value:1});assert.deepEqual(await b,{value:1});
});

test('a cancelled slow response cannot overwrite a newer result',async()=>{
  const finishes=[];
  const cache=createQueryCache(()=>new Promise(resolve=>finishes.push(resolve)));
  const oldController=new AbortController();
  const old=cache.load('/items',{signal:oldController.signal});await Promise.resolve();oldController.abort();
  await assert.rejects(old,{name:'AbortError'});
  const current=cache.load('/items');await Promise.resolve();finishes[1]({revision:2});await current;
  finishes[0]({revision:1});await Promise.resolve();await Promise.resolve();
  assert.equal(cache.peek('/items').revision,2);
});

test('pending responses are not cached and LRU respects its entry budget',async()=>{
  const cache=createQueryCache(async path=>path==='/pending'?{_meta:{status:'pending'}}:{path},{maxEntries:2});
  await cache.load('/pending');assert.equal(cache.peek('/pending'),null);
  await cache.load('/a');await cache.load('/b');cache.peek('/a');await cache.load('/c');
  assert.equal(cache.peek('/b'),null);assert.ok(cache.peek('/a'));assert.ok(cache.peek('/c'));
});

test('refresh failure preserves the previously displayed snapshot',async()=>{
  let fail=false,time=0;
  const cache=createQueryCache(async()=>{if(fail)throw Error('offline');return {value:8};},{clock:()=>time});
  await cache.load('/items');fail=true;time=20000;
  await assert.rejects(cache.load('/items'));assert.equal(cache.peek('/items').value,8);
});
