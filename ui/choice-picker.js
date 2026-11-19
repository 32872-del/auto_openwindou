'use strict';
// All catalog backed values keep stable provider IDs. Labels are only for display.
const CHOICE_KEYS=new Set(['systems','android_brands','android_models','android_versions','android_model','android_version',
 'device_brands','device_models','device_versions','device_ids','config_ids','fingerprint_ids','fingerprint_template','chrome_versions']);
const choiceLabels=new Map();
function choiceEnabled(f){
 if(!CHOICE_KEYS.has(f.key))return false;
 if(f.key==='chrome_versions')return ['kameleo','morelogin','hub'].includes(state.key);
 return true;
}
function choiceMany(f){return !['android_model','android_version','fingerprint_template'].includes(f.key)}
function choiceSummary(key,selected,empty='尚未选择'){
 if(!selected.length)return empty;
 const options=key==='systems'?new Map((state.catalog.find(p=>p.key===state.key)?.fields.find(f=>f.key==='systems')?.options||[])):null;
 const labels=selected.map(id=>choiceLabels.get(state.key+':'+key+':'+id)||options?.get(id)).filter(Boolean);
 return '已选 '+selected.length+' 项'+(labels.length?' · '+labels.slice(0,3).join('、')+(labels.length>3?' 等':''):' · 点击查看名称');
}
function choiceField(f,value){
 const selected=Array.isArray(value)?value:(value?[value]:[]);
 const summary=choiceSummary(f.key,selected,'尚未选择'+(f.key==='chrome_versions'?'（遵循厂商默认规则）':''));
 return '<div class="wide-field choice-field" data-choice-container="'+esc(f.key)+'"><span class="field-name">'+esc(f.label)+help(f.hint)+'</span>'+
  '<button type="button" class="choice-trigger" data-choice-field="'+esc(f.key)+'" aria-label="选择'+esc(f.label)+'">'+
  '<span>'+esc(summary)+'</span><b aria-hidden="true">⌄</b></button>'+
  (f.key==='systems'?'<small>点此调整可用系统范围；当前仅显示已选项。实际可创建系统还取决于厂商指纹库和已安装内核。</small>':'')+'</div>';
}
function bindChoiceFields(){
 $$('[data-choice-field]').forEach(button=>button.onclick=e=>act(()=>openChoicePicker(button.dataset.choiceField),e.target));
}
function refreshChoiceSummary(key){
 const label=$('[data-choice-field="'+key+'"] span');
 if(!label)return;
 const value=state.doc.settings[key],selected=Array.isArray(value)?value:(value?[value]:[]);
 label.textContent=choiceSummary(key,selected);
}
function uniqueChoices(items){
 const result=new Map();
 for(const item of items||[]){const id=String(item.id??'');if(id&&!result.has(id))result.set(id,{id,label:String(item.label??id),detail:String(item.detail??'')})}
 return [...result.values()];
}
async function openChoicePicker(key){
 const provider=state.key,editId=state.editId,field=state.catalog.find(p=>p.key===provider).fields.find(f=>f.key===key);
 const many=choiceMany(field),saved=state.doc.settings[key],selected=new Set(Array.isArray(saved)?saved:(saved?[saved]:[]));
 let rows=[],lastResult='',request=0,systems=[],filterVersion='';
 const cloud=key.startsWith('device_'),android=key.startsWith('android_');
 openDialog('选择'+field.label,'<div class="catalog-dialog choice-dialog"><div class="flex" id="choice-filters"></div>'+
  '<div class="flex"><input id="choice-search" type="search" placeholder="搜索名称、型号或版本" aria-label="搜索目录">'+
  '<button id="choice-refresh" type="button">刷新列表</button></div>'+
  '<p class="note" id="choice-note" role="status">正在读取目录…</p><div id="choice-list" class="device-list catalog-list"></div>'+
  '<div class="choice-footer"><span id="choice-count">已选 0 项</span><button id="choice-select-visible" type="button">全选筛选结果</button>'+
  '<button id="choice-clear-visible" type="button">取消筛选结果</button><button id="choice-cancel" type="button">取消</button>'+
  '<button id="choice-apply" class="primary" type="button">应用选择</button></div></div>',true);
 const host=$('#choice-list'),note=$('#choice-note');
 const visible=()=>{const q=$('#choice-search').value.trim().toLowerCase();return rows.filter(r=>(r.label+' '+r.detail+' '+r.id).toLowerCase().includes(q))};
 const render=()=>{
  const shown=visible(),known=new Set(rows.map(r=>r.id));
  host.innerHTML=shown.map(r=>'<label class="choice-row"><input type="'+(many?'checkbox':'radio')+'" name="choice-item" value="'+esc(r.id)+'" '+(selected.has(r.id)?'checked':'')+'><span><strong>'+esc(r.label)+'</strong><small>'+esc(r.detail||'')+'</small></span></label>').join('')+
   [...selected].filter(id=>!known.has(id)).map(id=>'<label class="choice-row choice-stale"><input type="'+(many?'checkbox':'radio')+'" name="choice-item" value="'+esc(id)+'" checked><span><strong>当前目录未返回：'+esc(id)+'</strong><small>仍保留已选值；请核对筛选、账号或刷新结果</small></span></label>').join('')+
   (!shown.length&&!selected.size?'<p class="catalog-empty">该筛选下没有可用项</p>':'');
  $('#choice-count').textContent='已选 '+selected.size+' 项 · 当前显示 '+shown.length+' 项';
  host.querySelectorAll('input').forEach(input=>input.onchange=()=>{if(!many)selected.clear();if(input.checked)selected.add(input.value);else selected.delete(input.value);render()});
 };
 const load=async(refresh=false)=>{
  const seq=++request;note.textContent='正在读取目录…';$('#choice-refresh').disabled=true;
  try{
   const settings=structuredClone(state.doc.settings);let result,items=[];
   if(key==='systems')result={items:field.options.map(([id,label])=>({id,label})),source:'厂商支持的系统选项',total:field.options.length};
   else if(android){
    result=await api('/providers/'+provider+'/models','POST',{settings,refresh});
    const models=result.models||[];
    if(key==='android_brands')items=[...new Set(models.map(m=>m.brand))].map(id=>({id,label:id}));
    else if(key==='android_models'||key==='android_model')items=models.filter(m=>!settings.android_brands.length||settings.android_brands.includes(m.brand)).map(m=>({id:m.value,label:m.brand+' · '+m.value,detail:(m.versions||[]).length?'Android '+m.versions.join('、'):'系统版本由厂商决定'}));
    else {const subset=key==='android_version'&&settings.android_model?models.filter(m=>m.value===settings.android_model):settings.android_models.length?models.filter(m=>settings.android_models.includes(m.value)):models;
     items=[...new Set(subset.flatMap(m=>m.versions||[]))].map(id=>({id,label:'Android '+id}));}
   }else if(cloud){
    if(key==='device_ids'){
     result=await api('/providers/'+provider+'/probe','POST',{settings});items=(result.devices||[]).map(d=>({id:d.id,label:d.name||d.id,detail:[d.brand,d.model,d.system,d.status].filter(Boolean).join(' · ')}));result.source='当前已有设备实时状态';result.total=items.length;
    }else{
     const versions=await api('/providers/'+provider+'/cloud/systems','POST',{settings});
     systems=versions.items||[];
     if(key==='device_versions'){result={items:systems,source:'厂商系统目录',total:systems.length}}
     else{
      const version=$('#choice-version')?.value||settings.device_versions[0]||systems[0]?.id||'';
      if(!version)throw Error('厂商未返回可用系统，请检查连接与账号权限');
      result=await api('/providers/'+provider+'/cloud/models','POST',{settings,version,refresh});
      const models=result.items||[];
      items=key==='device_brands'?[...new Set(models.map(m=>m.brand))].map(id=>({id,label:id})):models.filter(m=>!settings.device_brands.length||settings.device_brands.includes(m.brand)).map(m=>({id:m.id,label:m.brand+' · '+m.label,detail:'型号 ID：'+m.id}));
     }
    }
   }else{
    const section=({config_ids:'configs',fingerprint_ids:'fingerprints',fingerprint_template:'templates',chrome_versions:'versions'})[key];
    if(!section)throw Error('该字段没有已接入的厂商目录');
    const filters=section==='fingerprints'?{system:settings.fixed_system,product:settings.browser_product,version:$('#choice-version')?.value||''}:{};
    result=await api('/providers/'+provider+'/catalog/'+section,'POST',{settings,filters,refresh});
   }
   if(!host.isConnected||seq!==request||editId!==state.editId)return;
   rows=uniqueChoices(items.length?items:result.items||[]);
   rows.forEach(row=>choiceLabels.set(provider+':'+key+':'+row.id,row.label));
   note.textContent=(result.source||'厂商目录')+' · '+(result.total??rows.length)+' 项'+(result.partial?'（厂商只返回部分结果）':'')+'。'+catalogCacheNote(result)+(result.note?'。'+result.note:'');
   render();
  }catch(error){if(host.isConnected&&seq===request)note.textContent='读取失败，已选值仍保留：'+error.message;throw error}
  finally{if(host.isConnected&&seq===request)$('#choice-refresh').disabled=false}
 };
 if(cloud&&key!=='device_ids'&&key!=='device_versions'){
  const settings=structuredClone(state.doc.settings);
  try{const result=await api('/providers/'+provider+'/cloud/systems','POST',{settings});systems=result.items||[];
   $('#choice-filters').innerHTML='<label>系统版本<select id="choice-version">'+systems.map(v=>'<option value="'+esc(v.id)+'">'+esc(v.label)+'</option>').join('')+'</select></label>';
   if(settings.device_versions.length&&systems.some(v=>v.id===settings.device_versions[0]))$('#choice-version').value=settings.device_versions[0];
   $('#choice-version').onchange=()=>act(()=>load());
  }catch(error){note.textContent='系统目录读取失败：'+error.message}
 }
 if(key==='fingerprint_ids'){
  $('#choice-filters').innerHTML='<label>浏览器主版本<select id="choice-version"><option value="">当前产品全部可见版本</option></select></label>';
  try{const data=await api('/providers/'+provider+'/catalog/versions','POST',{settings:structuredClone(state.doc.settings)});
   $('#choice-version').innerHTML+=[...new Set((data.items||[]).map(v=>v.id))].map(v=>'<option value="'+esc(v)+'">'+esc(v)+'</option>').join('');
   $('#choice-version').value=state.doc.settings.chrome_versions[0]||'';
  }catch(error){note.textContent='内核目录未读取：'+error.message}
  $('#choice-version').onchange=()=>act(()=>load());
 }
 $('#choice-search').oninput=render;$('#choice-refresh').onclick=e=>act(()=>load(true),e.target);
 $('#choice-select-visible').hidden=!many;$('#choice-clear-visible').hidden=!many;
 $('#choice-select-visible').onclick=()=>{visible().forEach(r=>selected.add(r.id));render()};
 $('#choice-clear-visible').onclick=()=>{visible().forEach(r=>selected.delete(r.id));render()};
 $('#choice-cancel').onclick=closeDialog;
 $('#choice-apply').onclick=()=>{
  if(editId!==state.editId){closeDialog();return}
  state.doc.settings[key]=many?[...selected]:[...selected][0]||'';
  if(key==='fingerprint_template')state.doc.settings.fingerprint_mode=selected.size?'fixed':'random';
  if(key==='config_ids'&&selected.size)state.doc.settings.fp_source='pool';
  if(key==='fingerprint_ids'&&selected.size)state.doc.settings.system_mode='fixed';
  markDirty();closeDialog();renderProvider();
 };
 await act(load);
}
