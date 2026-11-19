'use strict';
// This in-memory ledger never reaches the production run store or provider APIs.
const demoSeed=[
 ['octo','O 浏览器','desktop','queued','排队等待代理','US','FL','Doral','33178','Bolt 美东城市',''],
 ['morelogin','MoreLogin','desktop','creating','创建环境中','US','ME','Portland','04101','Bolt 美东州级',''],
 ['kameleo','Kameleo','desktop','running','人工使用中','US','MA','Worcester','01603','固定住宅代理','198.51.100.24'],
 ['duoplus','DuoPlus','phone','running','设备已启动','CA','ON','Toronto','M5V 2T6','保留设备现有代理','203.0.113.18'],
 ['geelark','GeeLark','phone','failed','代理预检失败','US','CA','San Jose','95113','城市动态代理',''],
 ['bitbrowser','BitBrowser','desktop','closing','等待清理确认','US','NY','New York','10001','固定住宅代理','198.51.100.31'],
 ['undetectable','Undetectable','desktop','closed','环境已删除','CA','BC','Vancouver','V6B 1A1','加拿大全域代理','203.0.113.36'],
 ['chrome','本地 Chrome','desktop','cleanup_failed','清理失败待重试','US','WA','Seattle','98101','直连','']
];
let demoRows=[];
function resetDemo(){
 demoRows=demoSeed.map((row,i)=>({
  id:'demo-'+(i+1),number:i+1,name:'演示窗口 '+String(i+1).padStart(2,'0'),launch:'DEMO-'+String(i+1).padStart(3,'0'),
  provider:row[0],label:row[1],kind:row[2],status:row[3],stage:row[4],os:row[2]==='phone'?'Android 15':'Windows 11',
  location:{country:row[5].toLowerCase(),state:row[6],city:row[7],zip:row[8]},source:row[9],ip:row[10],
  protocol:row[10]?'IPv4':'',latency:row[10]?28+i*7:null,error:row[3]==='failed'?'模拟：目标城市代理无可用出口':'',
  cleanup_error:row[3]==='cleanup_failed'?'模拟：厂商确认接口超时':'',keep:false,trashed:false,
  profile_id:'DEMO-PROFILE-'+(i+1),pid:0,created:'',address:'演示地址，不是用户资料'
 }));
}
async function demoRequest(path,method='GET',body={}){
 if(path==='/runs'&&method==='POST')throw Error('演示模式不会创建真实环境，请先退出演示');
 if(path==='/run-batch/open'){
  const results=[];
  for(const id of body.ids||[]){const row=demoRows.find(r=>r.id===id);
   if(!row||row.trashed||['closed','closing','cleanup_failed'].includes(row.status)||(row.kind==='desktop'&&!row.keep))results.push({id,ok:false,error:'原环境未保留或尚未收尾，无法复用'});
   else if(['running','queued','creating'].includes(row.status))results.push({id,ok:true,skipped:true});
   else {row.status='running';row.stage='模拟打开原环境';results.push({id,ok:true})}}
  return {results,message:'演示：已处理 '+results.length+' 条'};
 }
 if(path.startsWith('/run-batch/')){
  const action=path.split('/').at(-1),ids=body.ids||[];
  if(!Array.isArray(ids)||!ids.length)throw Error('请先勾选演示记录');
  for(const id of ids){const row=demoRows.find(r=>r.id===id);if(!row)throw Error('演示记录不存在');
   if(action==='trash'){row.status=row.kind==='phone'?'stopped':row.keep?'stopped':'closed';row.trashed=true;row.trashed_at=Date.now()/1000}
   else if(action==='restore'){row.trashed=false;delete row.trashed_at}
   else if(action==='purge'||action==='remove-local')demoRows=demoRows.filter(r=>r.id!==id);
   else throw Error('演示操作未实现：'+action)}
  return {count:ids.length,message:'演示：已处理 '+ids.length+' 条'};
 }
 if(path.startsWith('/runs?')){
  const params=new URLSearchParams(path.split('?')[1]),query=(params.get('query')||'').toLowerCase(),kind=params.get('kind')||'all',category=params.get('category')||'all';
  let rows=demoRows.filter(r=>(kind==='all'||r.kind===kind)&&(category==='trash'?r.trashed:category==='kept'?r.keep&&!r.trashed:!r.trashed)&&(!query||(r.name+' '+r.label+' '+r.source+' '+r.launch).toLowerCase().includes(query)));
  if(params.get('descending')==='true')rows=[...rows].reverse();
  const offset=Number(params.get('offset')||0);
  return {rows:rows.slice(offset,offset+100),total:rows.length,cursor:0,stats:{total:demoRows.length,running:demoRows.filter(r=>r.status==='running').length,pending:demoRows.filter(r=>['queued','creating'].includes(r.status)).length,failed:demoRows.filter(r=>['failed','cleanup_failed'].includes(r.status)).length}};
 }
 const match=path.match(/^\/runs\/([^/]+)(?:\/([^/?]+))?/);
 if(!match)throw Error('演示请求不支持：'+path);
 const row=demoRows.find(r=>r.id===match[1]),action=match[2]||'';
 if(!row)throw Error('演示记录不存在');
 if(action==='history')return {events:[{stage:'模拟创建',status:row.status,time:'演示时间',error:row.error||row.cleanup_error||''}],archives:[]};
 if(action==='screen')throw Error('演示记录没有真实画面');
 if(action==='v2')return {message:'演示：短连接与 V2 请求未实际发出'};
 if(action==='retry'){const next={...structuredClone(row),id:'demo-'+(demoRows.length+1),number:demoRows.length+1,launch:'DEMO-'+String(demoRows.length+1).padStart(3,'0'),status:'queued',stage:'重新取代理',error:'',cleanup_error:'',trashed:false};demoRows.push(next);return {ids:[next.id]}}
 if(action==='cancel-query')return {message:'演示：已取消'};
 if(action==='preserve'){row.keep=!!body.value;return row}
 if(action==='trash'){row.trashed=true;return row}
 if(action==='restore'){row.trashed=false;return row}
 if(action==='close'){row.status=row.kind==='phone'?'stopped':row.keep?'stopped':'closed';row.stage=row.kind==='phone'?'模拟 API 已关机':'模拟环境已清理';row.cleanup_error='';return {message:'演示记录已关闭'}}
 if(action==='recreate'){const next={...structuredClone(row),id:'demo-'+(demoRows.length+1),number:demoRows.length+1,launch:'DEMO-'+String(demoRows.length+1).padStart(3,'0'),status:'queued',stage:'重新排队',error:'',cleanup_error:''};demoRows.push(next);return {ids:[next.id],message:'演示记录已排队'}}
 if(action==='resume'){row.status='running';row.stage='模拟重新打开';return {message:'演示记录已打开'}}
 if(!action&&method==='DELETE'){demoRows=demoRows.filter(r=>r.id!==row.id);return {message:'演示记录已删除'}}
 throw Error('演示操作未实现：'+action);
}
