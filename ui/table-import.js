'use strict';
const pasteDraft={text:'',preview:null};
let previewGeneration=0;
const fixedPasteExample='alice@example.com  ANTHONY  HALL  999 N IOWA ST  OLATHE  KS  66061  9135550110';
const fivePasteExample='alice@example.com\t123 Example St\tBoston\tMA\t02108';
const canadaPasteExample='alex@example.com Alex  Roy Martin  123 Example Ave  Toronto  ON  M3N 2P8  4165550110';
function openPaste(returnToDraft=false){
 if(!returnToDraft){pasteDraft.text='';pasteDraft.preview=null;previewGeneration++}
 openDialog('粘贴邮箱资料',`<div class="stack"><p><strong>八列：邮箱 → 名 → 姓 → 地址 → 城市 → 州/省 → 邮编 → 电话</strong></p><p>也支持原五列：邮箱 → 地址 → 城市 → 州/省 → 邮编。</p><p class="note">一行一条，不要表头。从 Excel 复制时按 Tab 分列，同一单元格里的多词城市、姓名和地址不会拆成多个字段。纯文本中，邮箱后可用一个空格。多词姓名请用 Tab 或两个以上空格分列。全用单个空格时，程序根据州/省目录确定城市。州/省填大写两位代码。美国四位邮编自动补零（4743 → 04743）。加拿大邮编可写 M3N 2P8 或 M3N2P8。八列的姓名和电话都要填写。</p><pre class="paste-format-example">八列示例：\n${esc(fixedPasteExample)}\n\n加拿大八列示例：\n${esc(canadaPasteExample)}\n\n五列示例：\n${esc(fivePasteExample)}</pre><div class="flex"><button id="copy-paste-example">复制八列示例</button><button id="read-clipboard">读取最新剪贴板</button><small>也可在下框 Ctrl+V</small></div><textarea id="paste-text" rows="7" spellcheck="false" placeholder="${esc(fixedPasteExample)}">${esc(pasteDraft.text)}</textarea><button id="preview-paste">校验格式并预览</button><div id="paste-error" class="result-box error hidden" role="alert" style="white-space:pre-wrap"></div><div id="paste-preview"></div><div class="import-bottom"><small id="paste-count">每批 1～100 行，同批邮箱不能重复。先校验，再确认打开。</small><button class="primary" id="use-paste" disabled>带入创建 / 打开</button></div></div>`,true);
 $('#paste-text').oninput=e=>{pasteDraft.text=e.target.value;previewGeneration++;pasteDraft.preview=null;$('#use-paste').disabled=true;$('#paste-preview').innerHTML='';$('#paste-count').textContent='内容有更改，请重新校验';$('#paste-error').classList.add('hidden')};
 $('#copy-paste-example').onclick=e=>act(()=>copy(fixedPasteExample),e.target);
 $('#read-clipboard').onclick=e=>act(async()=>{let text;try{if(window.pywebview?.api?.read_clipboard){const result=await window.pywebview.api.read_clipboard();if(result.error)throw Error(result.error);text=result.text}else text=await navigator.clipboard.readText()}catch{throw Error('未能读取剪贴板，请点文本框按 Ctrl+V')}if(!text?.trim())throw Error('请先复制五列或八列邮箱资料');$('#paste-text').value=text;$('#paste-text').dispatchEvent(new Event('input'));await previewPaste()},e.target);
 $('#preview-paste').onclick=e=>act(previewPaste,e.target);
 $('#use-paste').onclick=e=>{if(pasteDraft.preview?.valid)act(()=>openCreate(structuredClone(pasteDraft.preview.rows.map(row=>row.item))),e.target)};
 if(pasteDraft.preview)renderPaste(pasteDraft.preview);
}
async function previewPaste(){
 const generation=++previewGeneration;$('#use-paste').disabled=true;$('#paste-error').classList.add('hidden');
 try{const result=await api('/import/preview','POST',{email_mode:true,text:pasteDraft.text});if(generation!==previewGeneration||!$('#paste-preview'))return;pasteDraft.preview=result;renderPaste(result)}
 catch(e){if(generation===previewGeneration&&$('#paste-error')){pasteDraft.preview=null;$('#paste-preview').innerHTML='';$('#paste-error').textContent=e.message;$('#paste-error').classList.remove('hidden');$('#paste-count').textContent='本批未导入，请按上方五列或八列示例修正后重新校验'}throw e}
}
function renderPaste(result){
 const errors=result.rows.filter(row=>row.error);
 $('#paste-preview').innerHTML=`<p class="note">列顺序已固定，不能调整。下一步可选择电脑浏览器或云手机；同一邮箱按类型分别绑定，选择后确认创建或复用。</p><div class="import-grid"><table><thead><tr><th>原始行</th>${result.columns.map(key=>`<th>${esc(result.fields[key])}</th>`).join('')}<th>国家 / 地区</th><th>已有绑定参考</th><th>格式检查</th></tr></thead><tbody>${result.rows.map(row=>`<tr class="${row.error?'import-invalid':''}"><td>${row.line}</td>${row.cells.map(value=>`<td>${esc(value)||'空'}</td>`).join('')}<td>${esc(row.location?.country_label||'未确定')}</td><td>${esc(row.account?.label||'待确认')}<small>${esc(row.account?.label_provider||'')}</small></td><td>${row.error?`<span class="error">${esc(row.error)}</span>`:'可导入'}</td></tr>`).join('')}</tbody></table></div>`;
 $('#paste-count').textContent=`共 ${result.rows.length} 行，${errors.length} 行需要修正。${errors.length?'全部修正后才能带入。':'请核对资料后继续。'}`;
 if(errors.length){$('#paste-error').textContent=result.format_hint+'\n示例：'+result.example;$('#paste-error').classList.remove('hidden')}
 $('#use-paste').disabled=!result.valid;
}
