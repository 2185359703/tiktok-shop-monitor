import {collectionErrorMessage} from './collection-view.js';

export function collectionPlanPatch(original, draft) {
  const patch = {};
  if (draft.name.trim() !== original.name) patch.name = draft.name.trim();
  const before = new Set(original.resource_ids.map(String));
  const after = new Set(draft.resource_ids.map(String));
  if (before.size !== after.size || [...after].some(id => !before.has(id))) patch.resource_ids = [...after];
  if (original.schedule_mode === 'interval' && draft.interval_seconds !== original.interval_seconds) patch.interval_seconds = draft.interval_seconds;
  if (draft.enabled !== original.enabled) patch.enabled = draft.enabled;
  return patch;
}

export function collectionPageCursors(model, direction) {
  const cursors = [...model.cursors];
  if (direction === 'next' && model.next) cursors.push(model.next);
  if (direction === 'prev' && cursors.length > 1) cursors.pop();
  return cursors;
}

export function acceptCollectionPage(model, response, cursors) {
  model.items = response.items || [];
  model.next = response.next_before_id || null;
  model.cursors = [...cursors];
  model.loaded = true;
  model.error = null;
}

/** Candidate pages and selected scopes have independent lifetimes. */
export function createCollectionResourcePicker({read,taskType,selectedIds=[],maximum=100,onChange=()=>{}}) {
  const selections = new Map();
  const state = {taskType,query:'',resultQuery:'',items:[],next:null,cursors:[null],loaded:false,loading:false,error:null,resolving:false,selectionError:null,maximum,selected:new Map()};
  let listController = null, selectionController = null, stopped = false;
  const placeholders = ids => new Map(ids.map(id => [String(id),{id:String(id),name:null,eligible:null}]));
  state.selected = placeholders(selectedIds);
  selections.set(taskType,state.selected);
  const changed = () => {if(!stopped)onChange(state);};
  function stop() {stopped=true;listController?.abort();selectionController?.abort();state.loading=false;state.resolving=false;}
  async function load({query=state.query,direction=null}={}) {
    if(stopped)return;
    const reset=query!==state.resultQuery;
    state.query=query;
    const cursors=reset ? [null] : collectionPageCursors(state,direction);
    listController?.abort();const current=new AbortController();listController=current;
    const kind=state.taskType;state.loading=true;state.error=null;changed();
    try {
      const response=await read({taskType:kind,query,beforeId:cursors.at(-1),signal:current.signal});
      if(stopped||current.signal.aborted||listController!==current||state.taskType!==kind)return;
      acceptCollectionPage(state,response,cursors);
      state.resultQuery=query;
      for(const row of state.items)if(state.selected.has(String(row.id)))state.selected.set(String(row.id),row);
    }catch(error){if(!current.signal.aborted&&!stopped)state.error=collectionErrorMessage(error);}
    finally{if(listController===current&&!stopped){state.loading=false;changed();}}
  }
  async function resolveSelected() {
    selectionController?.abort();
    if(stopped||!state.selected.size){state.resolving=false;changed();return;}
    const current=new AbortController();selectionController=current;
    const kind=state.taskType, ids=[...state.selected.keys()];state.resolving=true;state.selectionError=null;changed();
    try {
      const response=await read({taskType:kind,ids,signal:current.signal});
      if(stopped||current.signal.aborted||selectionController!==current||state.taskType!==kind)return;
      const known=new Map(response.items.map(row=>[String(row.id),row]));
      for(const id of ids)if(state.selected.has(id))state.selected.set(id,known.get(id)||{id,name:null,eligible:false,eligibility_reason:'resource_missing'});
    }catch(error){if(!current.signal.aborted&&!stopped)state.selectionError=collectionErrorMessage(error);}
    finally{if(selectionController===current&&!stopped){state.resolving=false;changed();}}
  }
  function select(row,checked) {
    const id=String(row.id);
    if(!checked){state.selected.delete(id);changed();return true;}
    if(row.eligible!==true)return false;
    if(!state.selected.has(id)&&state.selected.size>=maximum){state.selectionError=`最多选择 ${maximum} 个资源。`;changed();return false;}
    state.selectionError=null;state.selected.set(id,row);changed();return true;
  }
  return {
    state,load,resolveSelected,select,stop,
    remove(id){state.selected.delete(String(id));state.selectionError=null;changed();},
    replace(ids){
      if(ids.length>maximum)throw Error(`最多选择 ${maximum} 个资源。`);
      selectionController?.abort();
      const previous=state.selected;state.selected=new Map(ids.map(value=>{const id=String(value);return [id,previous.get(id)||{id,name:null,eligible:null}];}));
      selections.set(state.taskType,state.selected);return resolveSelected();
    },
    setType(kind,{fetch=true}={}){
      if(kind===state.taskType||stopped)return;
      listController?.abort();selectionController?.abort();
      state.taskType=kind;state.query='';state.resultQuery='';state.items=[];state.next=null;state.cursors=[null];state.loaded=false;state.error=null;state.selectionError=null;
      state.selected=selections.get(kind)||new Map();selections.set(kind,state.selected);changed();
      if(fetch){load();resolveSelected();}
    },
  };
}
