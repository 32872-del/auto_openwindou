'use strict';
let activeProgramPrompt=null;
const shownProgramFailures=new Set();
async function requestProgram(error){
 if(activeProgramPrompt)await activeProgramPrompt;
 const provider=error.provider,label=error.label||provider;
 const promise=new Promise(resolve=>{
  const overlay=document.createElement('div');overlay.className='proxy-paste-backdrop';
  overlay.innerHTML=`<section class="proxy-paste-card" role="dialog" aria-modal="true" aria-label="指定浏览器程序"><h2>请选择 ${esc(label)} 的 EXE 位置</h2><p>自动探测没有找到可绑定的程序。请选择这家浏览器的主程序，保存后继续本次操作。</p><p class="note">${esc(error.message)}</p><label>主程序完整路径<input data-program-path placeholder="例如 C:\\Program Files\\浏览器\\浏览器.exe" spellcheck="false"></label><div class="flex"><button data-program-choose>选择 EXE 文件</button></div><p data-program-error class="error" role="alert"></p><div class="dialog-footer"><button data-program-cancel>暂不打开</button><button data-program-save class="primary">绑定并继续</button></div></section>`;
  document.body.appendChild(overlay);
  const input=overlay.querySelector('[data-program-path]'),output=overlay.querySelector('[data-program-error]'),save=overlay.querySelector('[data-program-save]');
  let done=false,busy=false;
  const finish=ok=>{if(done)return;done=true;overlay.remove();resolve(ok)};
  overlay.querySelector('[data-program-cancel]').onclick=()=>{if(!busy)finish(false)};
  overlay.addEventListener('keydown',e=>{if(e.key==='Escape'){e.stopPropagation();if(!busy)finish(false)}});
  overlay.querySelector('[data-program-choose]').onclick=async()=>{try{if(!window.pywebview?.api?.select_program){output.textContent='请把该浏览器主程序的完整 EXE 路径粘贴到上方';input.focus();return}const selected=await window.pywebview.api.select_program();if(selected.path)input.value=selected.path}catch(e){output.textContent=e.message}};
  save.onclick=async()=>{if(busy)return;busy=true;save.disabled=true;output.textContent='正在核验程序…';try{const result=await api('/providers/'+encodeURIComponent(provider)+'/program-binding','POST',{path:input.value});toast(result.message);finish(true)}catch(e){output.textContent=e.message}finally{busy=false;save.disabled=false}};
  input.focus();
 });
 activeProgramPrompt=promise;
 try{return await promise}finally{if(activeProgramPrompt===promise)activeProgramPrompt=null}
}
async function checkProgramRequests(rows){
 if(activeProgramPrompt||state.demo)return;
 const row=rows.find(r=>r.status==='failed'&&r.program_required&&!shownProgramFailures.has(r.id+':'+r.launch));
 if(!row)return;
 const related=rows.filter(r=>r.status==='failed'&&r.program_required?.provider===row.program_required.provider);
 related.forEach(r=>shownProgramFailures.add(r.id+':'+r.launch));
 if(await requestProgram(row.program_required)){
  for(const r of related){try{await api('/runs/'+r.id+'/retry','POST',{})}catch(e){toast(e.message)}}
  if(state.page==='work')await updateRunPage();
 }
}
