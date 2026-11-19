"""有界调度；创建记录、进程身份和清理结果分别落盘。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import threading
import time
import re
import traceback
import uuid
import random
import psutil
from . import providers, proxy
from .catalog import BY_KEY
from .table_import import validate_item
from copy import deepcopy
from .selection import allocate
from .cloud_allocation import allocate_devices, use_all_idle
from .cleanup import process_state, close_browser, close_adapter, recover_cloud, close_owned_viewer
from .resource_state import confirmed_absent, unresolved_creation, UNCERTAIN_MESSAGE

# 一批最多 100 条，每条均可进入启动；不再读取旧版的同时创建数量。
START_WORKERS = 100
MONITOR_SECONDS = 3


def alive(pid, created):
    # 无权限不能当作进程已退出并触发删除。
    return process_state(pid, created) != 'exited'


def _failed_proxy_preflight(row):
    return row['status']=='failed' and bool(row.get('proxy_last_error')) and confirmed_absent(row)


def _allow_state_fallback(row):
    if row['kind'] == 'desktop':
        return True
    if '_allow_city_fallback' in row:
        return row['_allow_city_fallback'] is True
    # 兼容旧云手机记录。普通创建没有原始行号，邮箱绑定始终沿用粘贴资料。
    return not (row.get('source_row') or row.get('account_email') or (row.get('input_data') or {}).get('email'))


def _city_proxy_unavailable(error):
    """只有明确城市资源/地区不匹配才提示切换；网络、鉴权错误不能冒充缺城市。"""
    diagnostics=getattr(error,'diagnostics',{})
    history=diagnostics.get('proxy_history') or []
    message=str(error)+' '+ ' '.join(str(entry.get('error','')) for entry in history)
    if diagnostics.get('failure_type') in ('gateway_dns','socks_connection_refused') or any(
            entry.get('failure_type') in ('gateway_dns','socks_connection_refused') for entry in history):
        return False
    if re.search(r'(?i)\b(?:401|403|407)\b|authentication|unauthorized|forbidden|invalid credentials|余额不足|insufficient balance|CERTIFICATE_VERIFY_FAILED|WRONG_VERSION_NUMBER|timed? ?out|timeout|connection refused|name resolution|DNS|无法解析',message):
        return False
    return isinstance(error,proxy.CityUnavailableError) or proxy._city_unavailable(message) or '出口地区不符：city' in message or 'CityUnavailableError' in message


def _cloud_proxy_location(row):
    from .location import normalize
    fields = ('country', 'state', 'city', 'zip')
    pasted = row.get('input_data') or {}
    saved = row.get('_location') or row.get('location') or {}
    # 当前账本会同时保存原始粘贴资料和调度快照。重新分配始终优先完整的原始地址。
    if pasted.get('city') and (pasted.get('zip') or pasted.get('state')):
        saved = pasted
    location = normalize(saved)
    return {key: location[key] for key in fields}


def _octo_cloud_delete_candidate(row):
    """本地停止被拒绝的失败记录，可由原 API 账号尝试删除已知 UUID。"""
    return (row.get('provider')=='octo' and row['kind']=='desktop' and
            row['status']=='cleanup_failed' and row.get('profile_id') and
            (row.get('_start_failed') or not row.get('keep')) and
            (row.get('_octo_stop_unavailable') or re.match(
                r'^(?:BrowserError: )?octo\.stop \[(?:connection_failed|login_required|unauthorized|unauthenticated|forbidden|not_found)(?:,|\])',
                row.get('cleanup_error') or '')) and
            (not row.get('pid') or (row.get('created') and process_state(row['pid'],row['created'])=='exited')))


class Engine:
    def __init__(self, store):
        self.store=store
        providers.initialize(store.root)
        from server.python.browser_s.desktop_launch_bridge_module_s.coordinator import DesktopLaunchCoordinator
        class CurrentDesktopCoordinator(DesktopLaunchCoordinator):
            @staticmethod
            def _background_protocol_active():return False
        self.desktop=CurrentDesktopCoordinator()
        self.pool=ThreadPoolExecutor(max_workers=START_WORKERS,thread_name_prefix='窗口启动')
        self.cleanup_pool=ThreadPoolExecutor(max_workers=2,thread_name_prefix='窗口清理')
        self.lock=threading.RLock();self.sessions={};self.cancel={};self.closing=set();self.stop=threading.Event();self._last_cloud_status_check={};self._next_trash_purge=0
        self.owner=store.resource_owner()
        from .diagnostics import Diagnostics
        self.diagnostics=Diagnostics(store,self.update)
        self.cleanup_pool.submit(self._recover)
        self.monitor=threading.Thread(target=self._monitor,name='进程监测',daemon=True)
        self.monitor.start()

    def submit(self, data, *, frozen=None, refresh_proxy=False):
        """一次校验整批，再原子保存邮箱绑定；只为尚未启动的环境排队。"""
        from .email_accounts import email_key, account_action, row_email
        from vendor_runtime.legacy_core.environment_identity import new_launch_id
        with self.lock:
            if self.stop.is_set():raise ValueError('软件正在退出，不能再创建窗口')
            if not isinstance(data.get('kind'),str) or not isinstance(data.get('provider'),str):raise ValueError('请选择环境类型和厂商')
            kind='phone' if data['kind']=='app' else data['kind']
            if data['provider']!='random':self.store.check_kind(kind,data['provider'])
            elif kind!='desktop':raise ValueError('只能在电脑浏览器内部按比例随机')
            if 'network_mode' in data:
                if kind!='phone' or data['network_mode'] not in ('inherit','keep','wifi','mobile'):
                    raise ValueError('请为云手机选择有效的联网模式')
            if 'cloud_device_scope' in data and (kind!='phone' or data['cloud_device_scope'] not in ('inherit','all_idle')):
                raise ValueError('请为云手机选择有效的设备范围')
            email_mode=data.get('email_mode',False)
            if not isinstance(email_mode,bool):raise ValueError('邮箱创建模式需要开关值')
            for flag in ('website_test','tls_test'):
                if flag in data and not isinstance(data[flag],bool):raise ValueError(('TLS 检测' if flag=='tls_test' else '网站检测')+'需要开关值')
            if 'cleanup' in data and not isinstance(data['cleanup'],bool):raise ValueError('关闭后清理需要开关值')
            imported=data.get('items')
            if imported is not None and (not isinstance(imported,list) or not 1<=len(imported)<=100):
                raise ValueError('请带入 1～100 条表格数据')
            if 'allow_city_fallback' in data and (
                    kind!='phone' or imported is None or not isinstance(data['allow_city_fallback'], bool)):
                raise ValueError('云手机粘贴任务的同州 / 省回退选项需要开关值')
            if email_mode and imported is None:raise ValueError('请先粘贴并识别邮箱资料')
            items=[validate_item(item,row_number=i+1) for i,item in enumerate(imported)] if imported is not None else None
            existing={};seen={};new_items=[];order=[]
            if items is not None:
                email_index=self.store.email_runs_index()
                for item in items:
                    email=item['email'];key=email_key(email)
                    if email_mode and not email:raise ValueError(f'第 {item["source_row"]} 行：按邮箱创建需要邮箱')
                    if email:
                        if key in seen:raise ValueError(f'邮箱 {email} 在第 {seen[key]}、{item["source_row"]} 行重复，请保留一条')
                        seen[key]=item['source_row']
                        row=self.store.find_email_run(email,index=email_index,kind=kind)
                    else:row=None
                    if row:
                        info=account_action(row,self.owner)
                        if row['id'] in getattr(self,'closing',set()):raise ValueError('邮箱环境正在关闭，请稍后再打开')
                        if row['provider']=='ruyi' and (data.get('website_test',row.get('_website_test')) or data.get('tls_test',row.get('_tls_test'))):
                            raise ValueError('原邮箱环境使用如意 Firefox，不能执行 Chromium 自动检测')
                        row=deepcopy(row);row['account_email']=row_email(row);row['name']=row['account_email']
                        row['keep']=row['kind']=='desktop';row['_plan']['preserve']=row['keep']
                        row.setdefault('_website_test',False);row.setdefault('_tls_test',False)
                        action=info['action']
                        if action!='existing':
                            row.update(status='queued',stage='排队打开邮箱固定环境',_reuse=action=='resume' and row['kind']=='desktop',
                                _refresh_proxy=refresh_proxy or _failed_proxy_preflight(row),
                                _closed=False,_deleted=False,_cleanup_complete=False,_start_failed=False,_start_requested=False,_start_rejected=False,
                                _cleanup_attempts=0,error='',cleanup_error='',cleanup_operation='',connection_phase='已断开',
                                launch=new_launch_id(),archive_launch='',tls_status='',tls_error='',tls_capture_id='',tls_file='',tls_captured_at='',
                                detection_status='',detection_error='')
                            for flag in ('website_test','tls_test'):
                                if row['kind']!='desktop':row['_'+flag]=False
                                elif flag in data:row['_'+flag]=data[flag]
                            if row['kind']!='desktop':
                                if 'allow_city_fallback' in data:
                                    row['_allow_city_fallback']=data['allow_city_fallback']
                                row.update(chrome_egress={},device_egress={},network_report={},vendor_proxy={},preflight_ip='',ip='')
                                row['planned_device']=row.get('profile_id') or row['planned_device']
                                row['_cloud_resume_unstarted']=not row.get('_cloud_owned')
                        existing[row['id']]=(row,action!='existing')
                        order.append(row['id'])
                    else:
                        new_items.append(item);order.append(None)
            else:
                new_items=items
            prepared=[]
            from .system_assignment import validate_assignments
            validate_assignments(data,new_items or [])
            if items is None or new_items:
                prepared=self._prepare_new({**data,**({'items':new_items} if items is not None else {})},frozen=frozen,refresh_proxy=refresh_proxy)
            for row in prepared:
                row['_refresh_proxy']=refresh_proxy
                email=row['input_data']['email']
                if email:
                    row.update(account_email=email,name=email,keep=row['kind']=='desktop')
                    row['_plan']['preserve']=row['keep']
            pending=iter(row['id'] for row in prepared)
            result=[identity if identity is not None else next(pending) for identity in order] if order else [row['id'] for row in prepared]
            records=prepared+[row for row,_ in existing.values()]
            self.store.put_runs(records)
            for identity in [row['id'] for row in prepared]+[row['id'] for row,queue in existing.values() if queue]:
                self.cancel[identity]=threading.Event()
                self.pool.submit(self._start,identity)
            return result

    def _prepare_new(self, data, *, frozen=None, refresh_proxy=False):
        if self.stop.is_set():raise ValueError('软件正在退出，不能再创建窗口')
        if not isinstance(data.get('kind'),str) or not isinstance(data.get('provider'),str):raise ValueError('请选择环境类型和厂商')
        kind,key=('phone' if data['kind']=='app' else data['kind']),data['provider']
        weights={}
        if key=='random':
            if kind!='desktop':raise ValueError('只能在电脑浏览器内部按比例随机')
            weights=self.store.read('配置/随机比例.json',{'weights':{}})['weights']
            weights={k:v for k,v in weights.items() if v>0}
            if not weights:raise ValueError('请先在环境方案设置电脑浏览器随机比例')
            for k in weights:self.store.check_kind(kind,k)
        else:self.store.check_kind(kind,key)
        imported=data.get('items')
        if imported is not None and (not isinstance(imported,list) or not imported):raise ValueError('请先预览并带入至少一条表格数据')
        count=len(imported) if imported is not None else data.get('count',1)
        if isinstance(count,bool) or not isinstance(count,int) or not 1<=count<=100:raise ValueError('一次创建数量应为 1～100')
        name=data.get('name','窗口')
        if not isinstance(name,str) or not name.strip() or len(name.strip())>80:raise ValueError('名称前缀长度应为 1～80')
        name=name.strip()
        location=data.get('location',{})
        if not isinstance(location,dict) or set(location)-{'country','state','city','zip'}:raise ValueError('地区信息格式不正确')
        if not isinstance(data.get('proxy_test',False),bool):raise ValueError('代理检测应为开关值')
        if data.get('ip_family','any') not in ('any','ipv4','ipv6','dual'):raise ValueError('代理IP类型应为不限、IPv4、IPv6或双栈')
        # 先校验整批再写入/排队，不能前几行已创建才发现后面的错误。
        items=[validate_item(item,row_number=i+1) for i,item in enumerate(imported)] if imported is not None else [validate_item({'name':f'{name}-{i+1:03d}',**location}) for i in range(count)]
        fallback=validate_item({'name':name,**location})
        candidates=list(weights) if weights else [key]
        settings=self.store.system();prepared={}
        proxy_document=deepcopy(frozen['_proxy_document'] if frozen and not refresh_proxy and '_proxy_document' in frozen else self.store.proxies())
        for candidate in candidates:
            config=deepcopy(frozen['_config'] if frozen else self.store.provider(candidate)['settings'])
            plan=deepcopy(frozen['_plan'] if frozen else self.store.plan(kind,candidate)['settings'])
            if kind!='desktop':
                if data.get('cloud_device_scope')=='all_idle' or (imported is not None and not frozen):use_all_idle(config)
                if data.get('network_mode','inherit')!='inherit':
                    config['network_mode']=data['network_mode']
                if config['device_selection']=='selected' and not config['device_ids']:raise ValueError('请先扫描并勾选本次允许使用的云手机')
                if not config['api_key']:raise ValueError('请先保存云手机 API 密钥')
            elif candidate not in ('chrome','ruyi'):providers.manager_identity(candidate,config,self.store)
            plan['proxy_request']={k:deepcopy(data[k]) for k in ('proxy_mode','proxy_sources') if k in data}
            if not frozen:plan['proxy_sources'],plan['proxy_origin']=proxy.resolve_selection(proxy_document,data,plan)
            target=data.get('target','')
            if not isinstance(target,str):raise ValueError('启动目标需要文本')
            if target.strip() and kind=='desktop':plan['url']=validate_item({'name':name,'url':target})['url']
            clean=data.get('cleanup')
            if clean is not None and not isinstance(clean,bool):raise ValueError('关闭后清理需要开关值')
            if clean is not None:plan['preserve']=not clean
            elif kind=='desktop':plan['preserve']=True
            if kind!='desktop':plan['preserve']=False
            prepared[candidate]=(config,plan)
        website_test=data.get('website_test',False)
        if not isinstance(website_test,bool):raise ValueError('网站检测需要开关值')
        if website_test and kind!='desktop':raise ValueError('云手机没有本地网页检测通道；请关闭“网站检测”，文件与软件准备不受影响')
        if website_test and 'ruyi' in candidates:raise ValueError('如意使用 Firefox 通道，当前两站自动存档仅支持 Chromium；请关闭网站检测或从本次随机范围移除如意')
        tls_test=data.get('tls_test',False)
        if not isinstance(tls_test,bool):raise ValueError('TLS 检测需要开关值')
        if tls_test and kind!='desktop':raise ValueError('TLS 自动检测需要电脑浏览器的网页查询通道')
        if tls_test and 'ruyi' in candidates:raise ValueError('如意使用 Firefox 通道，TLS 自动检测仅支持 Chromium；请关闭 TLS 检测或从本次随机范围移除如意')
        chosen_providers=allocate(weights,count) if weights else [key]*count
        locations=[]
        from .location import normalize as normalize_location
        for i,item in enumerate(items):
            actual={field:item[field] if imported is not None else fallback[field] for field in ('country','state','city','zip')}
            normalized=normalize_location(actual)
            actual={field:normalized[field] for field in ('country','state','city','zip')}
            try:proxy.validate_template_location(proxy_document,prepared[chosen_providers[i]][1]['proxy_sources'],actual)
            except ValueError as exc:raise ValueError(f'第 {i+1} 条：{exc}') from exc
            locations.append(actual)
        system_plans={};device_plans={}
        from .system_assignment import validate_assignments, row_config
        assigned_systems=validate_assignments(data,items)
        for candidate,(config,plan) in prepared.items():
            amount=chosen_providers.count(candidate)
            if not amount:continue
            if kind=='desktop':
                system_plans[candidate]=iter([assigned_systems[item['email'].casefold()] for item in items] if assigned_systems is not None else
                    [frozen['planned_system']]*amount if frozen else allocate(providers.system_weights(config),amount,mode=config.get('system_mode','weighted')))
            else:
                device_plans[candidate]=iter(allocate_devices(self.store,candidate,config,amount))
        result=[];records=[]
        for i,item in enumerate(items):
            chosen=chosen_providers[i]
            config,plan=prepared[chosen]
            identity=uuid.uuid4().hex
            # 表格是独立记录，不能把另一国家的全局城市/州补进某行。
            actual_location=locations[i]
            row_plan=deepcopy(plan)
            if item['url']:row_plan['url']=item['url']
            from vendor_runtime.legacy_core.environment_identity import new_launch_id
            row={'id':identity,'name':item['name'] or item['email'] or f'{name}-{i+1:03d}','launch':new_launch_id(),
                 'kind':kind,'provider':chosen,'label':BY_KEY[chosen]['name'],'status':'queued','stage':'排队中','keep':bool(plan['preserve']),
                 'ip':'','source':'待取得代理' if row_plan['proxy_sources'] else ('设备现有代理（未检测）' if kind=='phone' else '直连'),'latency':None,'error':'','profile_id':'','pid':0,'created':0,
                 'location':actual_location,'address':item['address'],'note':item['note'],'source_row':item['source_row'] if imported is not None else None,
                 'requested_ip_family':data.get('ip_family','any'),'creation_state':'not_requested',
                 'url':row_plan['url'] if kind!='app' else '',
                 'input_data':item,'planned_system':next(system_plans[chosen]) if kind=='desktop' else 'Android',
                 'planned_device':next(device_plans[chosen]) if kind!='desktop' else '',
                 'proxy_origin':row_plan['proxy_origin'],
                 '_owner':self.owner,'_config':deepcopy(config),'_plan':row_plan,'_location':actual_location,'_proxy_document':deepcopy(proxy_document),'_proxy_settings':deepcopy(frozen.get('_proxy_settings',settings) if frozen else settings),'_proxy_test':True,'_ip_family':data.get('ip_family','any'),'_website_test':website_test,'_tls_test':tls_test}
            if assigned_systems is not None:
                row['_config']=row_config(config,chosen,row['planned_system'])
            if kind=='phone':
                row['network_mode']=config.get('network_mode','keep')
                row['_allow_city_fallback']=data.get('allow_city_fallback', imported is None)
            records.append(row);result.append(identity)
        return records

    def recreate(self,identity,*,retry=False,refresh_proxy=False):
        row=self.store.get_run(identity)
        if retry and (row['status']!='failed' or identity in self.sessions):raise ValueError('仅已完成清理的失败行可以重试；清理失败请先重试关闭')
        location=_cloud_proxy_location(row) if row['kind']=='phone' else row.get('_location',{})
        item={**row.get('input_data',{}),'name':row.get('input_data',{}).get('name','') if row.get('account_email') else row['name'],'address':row.get('address',''),'note':row.get('note',''),
              'url':row.get('url',''),'source_row':row.get('source_row') or 1,**location}
        if '_proxy_document' not in row and row['_plan'].get('proxy_sources'):
            raise ValueError('这条旧记录没有保存代理来源快照，不能保证按原配置重试；请从创建窗口重新确认配置')
        result=self.submit({'kind':row['kind'],'provider':row['provider'],'items':[item],
                            **({'allow_city_fallback':_allow_state_fallback(row)} if row['kind']=='phone' else {}),
                            'proxy_test':True,'ip_family':row.get('_ip_family','any'),'website_test':row.get('_website_test',False),'tls_test':row.get('_tls_test',False),
                            **row['_plan'].get('proxy_request',{}),'cleanup':not row['keep']},frozen=row,refresh_proxy=refresh_proxy)
        if result[0]!=identity:self.update(result[0],recreated_from=identity)
        return result

    def _finish_before_action(self,identities,timeout=180,*,allow_octo_cloud_delete=False):
        for identity in identities:
            row=self.store.get_run(identity)
            if unresolved_creation(row):raise ValueError(UNCERTAIN_MESSAGE)
            if allow_octo_cloud_delete and _octo_cloud_delete_candidate(row):continue
            if row['status']=='cleanup_failed' and confirmed_absent(row) and identity not in self.sessions:
                self.update(identity,status='failed',stage='未创建环境，可删除或重试',
                    _cleanup_complete=True,cleanup_error='',cleanup_operation='close')
                continue
            if row['status'] in ('queued','creating','running','closing','cleanup_failed') or (
                    row['status'] in ('failed','cancelled') and row.get('profile_id') and not row.get('_cleanup_complete')):
                self.close_run(identity,for_action=True)
        deadline=time.monotonic()+timeout
        while True:
            rows=[self.store.get_run(identity) for identity in identities]
            failed=[row for row in rows if row['status']=='cleanup_failed' and row['id'] not in self.closing
                    and not (allow_octo_cloud_delete and _octo_cloud_delete_candidate(row))]
            if failed:raise ValueError('清理未完成，请先处理“'+failed[0]['name']+'”的清理错误')
            pending=[row for row in rows if row['id'] in self.closing or row['id'] in getattr(self,'_starting',set()) or row['status'] in ('queued','creating','running','closing') or
                     (row['status'] in ('failed','cancelled') and row.get('profile_id') and not row.get('_cleanup_complete'))]
            if not pending:return rows
            if time.monotonic()>=deadline:raise ValueError('等待关闭超时；未删除记录或启动新任务，请查看当前状态')
            time.sleep(.25)

    @contextmanager
    def _record_action(self, identities):
        with self.lock:
            if not hasattr(self,'_actions'):self._actions=set()
            if self._actions.intersection(identities):raise ValueError('该记录正在重试或删除，请等待本次操作完成')
            self._actions.update(identities)
        try:yield
        finally:
            with self.lock:self._actions.difference_update(identities)

    def retry_run(self,identity):
        with self._record_action([identity]):
            row=self._finish_before_action([identity])[0]
            if row.get('trashed'):raise ValueError('请先从回收站恢复，再重试')
            if row.get('profile_id') and not row.get('_deleted') and (row['kind']!='desktop' or row.get('keep')):
                return self._queue_resume(identity,refresh_proxy=True)['ids']
            return self.recreate(identity,retry=row['status']=='failed',refresh_proxy=True)

    def open_runs(self,identities):
        if not isinstance(identities,list) or not identities or len(identities)>100 or any(not isinstance(v,str) or not v for v in identities):
            raise ValueError('请选择 1～100 条要打开的记录')
        results=[]
        for identity in dict.fromkeys(identities):
            try:
                result=self.resume(identity)
                results.append({'id':identity,'ok':True,**result})
            except Exception as exc:
                results.append({'id':identity,'ok':False,'error':str(exc)})
        opened=sum(r['ok'] and not r.get('skipped') for r in results)
        skipped=sum(bool(r.get('skipped')) for r in results)
        failed=sum(not r['ok'] for r in results)
        return {'results':results,'count':opened,'skipped':skipped,'failed':failed,
                'message':f'已请求打开 {opened} 条，已在运行或排队 {skipped} 条，无法打开 {failed} 条'}

    def trash_runs(self,identities):
        if not isinstance(identities,list) or not identities or len(identities)>100 or any(not isinstance(value,str) or not value for value in identities):
            raise ValueError('请选择 1～100 条要删除的记录')
        identities=list(dict.fromkeys(identities))
        with self._record_action(identities):
            rows=[row for identity in identities if (row:=self.store.find_run(identity)) is not None]
            identities=[row['id'] for row in rows]
            if any(row.get('provider')=='octo' for row in rows):
                self._finish_before_action(identities,allow_octo_cloud_delete=True)
            else:self._finish_before_action(identities)
            removed=0
            for identity in identities:
                row=self.store.get_run(identity)
                cloud_delete=bool(_octo_cloud_delete_candidate(row))
                failed=row['status'] in ('failed','cancelled') or cloud_delete
                if failed and row['kind']=='desktop' and row.get('profile_id') and not row.get('_deleted'):
                    self._delete_retained(identity,row,octo_cloud_delete=cloud_delete)
                self.store.archive_run(identity)
                if failed:
                    self.store.delete_record(identity);removed+=1
        return {'count':len(identities),'message':f'已删除失败记录 {removed} 条，其余 {len(identities)-removed} 条已移入回收站'}

    def _delete_retained(self,identity,row,*,octo_cloud_delete=False):
        browser=None
        try:
            self.update(identity,cleanup_error='',cleanup_attempt_id=uuid.uuid4().hex,cleanup_operation='delete',
                        _octo_stop_unavailable=octo_cloud_delete)
            if row.get('_owner')!=self.owner:
                raise ValueError('保留环境归属无法确认，未删除厂商资料；可选择“仅移除本地记录”，远端环境仍保留')
            browser=providers.build_browser(row['provider'],row['_config'])
            close_browser(browser,{**row,'keep':False},lambda **values:self.update(identity,**values),octo_cloud_delete=octo_cloud_delete)
            closed=dict(status='failed',stage='O 云端环境已删除',_cleanup_complete=True,_closed=True,pid=0,created=0) if octo_cloud_delete else {}
            self.update(identity,keep=False,_deleted=True,creation_state='deleted',cleanup_error='',**closed)
            if octo_cloud_delete:self._release_local_tracking(identity,row)
        except Exception as exc:
            self.update(identity,cleanup_error=f'{type(exc).__name__}: {exc}')
            raise
        finally:
            if browser:close_adapter(browser)

    def remove_local_records(self,identities):
        if not isinstance(identities,list) or not identities or len(identities)>100 or any(not isinstance(v,str) or not v for v in identities):
            raise ValueError('请选择 1～100 条记录')
        identities=list(dict.fromkeys(identities))
        with self._record_action(identities):
            deadline=time.monotonic()+30
            while True:
                with self.lock:
                    rows=[row for identity in identities if (row:=self.store.find_run(identity)) is not None]
                    waiting=False
                    for row in rows:
                        identity=row['id']
                        if row['status'] in ('queued','creating') or identity in getattr(self,'_starting',set()):
                            raise ValueError('启动任务仍在运行，请等待启动结束后再移除本地记录')
                        if row['kind']=='desktop' and row['provider']=='octo':
                            # 只等待本程序已经派出的工作。旧 PID、登录状态和厂商网络均不影响本地移除。
                            task=self.diagnostics.cancel_window(identity)
                            if identity in self.closing or task.get('active') is True:waiting=True
                        elif (row['status'] in ('running','closing') or identity in self.closing or
                              (row['kind']!='desktop' and identity in self.sessions) or
                              process_state(row.get('pid'),row.get('created'))!='exited'):
                            raise ValueError('该任务或本地进程仍在运行，暂不能仅移除记录')
                    if not waiting:
                        for row in rows:
                            self.store.delete_record(row['id'],local_only=True)
                            self._release_local_tracking(row['id'],row)
                        break
                if time.monotonic()>=deadline:raise ValueError('已派出的清理或检测任务仍在执行，完成后可强制移除本地记录')
                time.sleep(.1)
        return {'count':len(rows),'message':f'已移除 {len(rows)} 条本地记录及邮箱绑定。此操作没有请求厂商停止或删除环境'}

    def _release_local_tracking(self,identity,row):
        with self.lock:
            session=self.sessions.pop(identity,None)
            self.cancel.pop(identity,None)
            self._last_cloud_status_check.pop(identity,None)
            try:
                if session and session.get('browser'):close_adapter(session['browser'])
            except Exception as exc:self.store.log(str(exc),run=identity,operation='释放本地连接')
            if row['kind']=='desktop' and row.get('profile_id') and row.get('provider')=='octo':
                try:self.desktop.release(row['provider'],row['profile_id'])
                except Exception as exc:self.store.log(str(exc),run=identity,operation='释放本地桌面句柄')

    def restore_runs(self,identities):
        if not isinstance(identities,list) or not identities or len(identities)>100 or any(not isinstance(value,str) or not value for value in identities):
            raise ValueError('请选择 1～100 条要恢复的记录')
        identities=list(dict.fromkeys(identities))
        for identity in identities:self.store.archive_run(identity,restore=True)
        return {'count':len(identities),'message':f'已恢复 {len(identities)} 条'}

    def purge_runs(self,identities):
        if not isinstance(identities,list) or not identities or len(identities)>100 or any(not isinstance(value,str) or not value for value in identities):
            raise ValueError('请选择 1～100 条要彻底删除的回收记录')
        identities=list(dict.fromkeys(identities))
        with self._record_action(identities):
            identities=[identity for identity in identities if self.store.find_run(identity) is not None]
            for identity in identities:
                row=self.store.get_run(identity)
                if not row.get('trashed'):raise ValueError('仅回收站记录可彻底删除')
                if row['status'] in ('queued','creating','running','closing','cleanup_failed'):
                    raise ValueError('仍有未清理的环境，未删除记录')
                if unresolved_creation(row):raise ValueError(UNCERTAIN_MESSAGE)
                if row['kind']=='desktop' and row.get('profile_id') and not row.get('_deleted'):
                    self._delete_retained(identity,row)
                self.store.delete_record(identity)
        return {'count':len(identities),'message':f'已彻底删除 {len(identities)} 条'}

    def resume(self,identity):
        with self._record_action([identity]):
            return self._queue_resume(identity)

    def _queue_resume(self,identity,*,refresh_proxy=False):
        from vendor_runtime.legacy_core.environment_identity import new_launch_id
        with self.lock:
            if self.stop.is_set():raise ValueError('软件正在退出，不能重新启动')
            row=self.store.get_run(identity)
            if row.get('trashed'):raise ValueError('请先从回收站恢复，再打开')
            if row.get('_owner')!=self.owner:raise ValueError('环境归属无法确认，请先核实原电脑或软件实例')
            if identity in self.closing or row['status'] in ('closing','cleanup_failed'):
                raise ValueError('环境正在关闭或清理未完成，请处理后再打开')
            if row['status'] in ('queued','creating'):
                return {'ids':[identity],'message':'已在启动队列，不重复创建','skipped':True}
            if row['status']=='running':
                if row['kind']=='desktop':return {'ids':[identity],'message':'窗口已运行，不重复创建','skipped':True}
                if identity not in getattr(self,'_viewer_opening',set()):
                    if not hasattr(self,'_viewer_opening'):self._viewer_opening=set()
                    self._viewer_opening.add(identity)
                    self.pool.submit(self._show_cloud_window,identity)
                return {'ids':[identity],'message':'正在唤起原云手机操作窗口'}
            if row.get('_deleted') or row['status']=='closed':raise ValueError('原环境已清理删除，无法复用；请重新创建')
            if row['status'] not in ('stopped','failed','cancelled'):raise ValueError('当前记录不可打开')
            if row.get('profile_id') and row['status']!='stopped' and not row.get('_cleanup_complete'):
                raise ValueError('原环境清理未完成，请先重试关闭')
            if row['kind']=='desktop':
                if not row.get('keep') or not row.get('profile_id'):
                    raise ValueError('原浏览器环境未保留或尚未创建，不能复用；可使用重试')
                if '_proxy' not in row:raise ValueError('原环境缺少代理快照，请核对原配置')
            else:
                device=row.get('profile_id') or row.get('planned_device')
                if not device:raise ValueError('原记录缺少云手机编号，无法复用')
                if device in self.store.reserved_cloud_devices(row['provider'],exclude=identity):
                    raise ValueError('原云手机已绑定其他邮箱或正在被其他任务使用，未重复启动')
                row['planned_device']=device
                row.update(chrome_egress={},device_egress={},network_report={},vendor_proxy={},preflight_ip='',ip='')
            row.update(status='queued',stage='排队打开原环境',_reuse=row['kind']=='desktop',
                _refresh_proxy=refresh_proxy or _failed_proxy_preflight(row),
                _closed=False,_deleted=False,_cleanup_complete=False,_start_failed=False,_start_requested=False,_start_rejected=False,
                _cloud_resume_unstarted=not row.get('_cloud_owned'),_viewer_owned=False,
                pid=0,created=0,hwnd=0,debug_port=0,viewer_status='',viewer_error='',
                error='',cleanup_error='',cleanup_operation='',_cleanup_attempts=0,connection_phase='已断开',
                launch=new_launch_id(),archive_launch='',tls_status='',tls_error='',tls_capture_id='',tls_file='',tls_captured_at='',
                detection_status='',detection_error='')
            self.cancel[identity]=threading.Event()
            self.store.put_run(row)
            self.pool.submit(self._start,identity)
        return {'ids':[identity],'message':'已保留原环境，排队重新启动'}

    def _bind_cloud_window(self,identity,window):
        info=window.describe()
        if not info.get('pid') or not info.get('hwnd') or not info.get('created'):
            raise RuntimeError('未取得云手机操作窗口，不能标记画面已打开')
        self.update(identity,pid=info['pid'],hwnd=info['hwnd'],created=info['created'],desktop=info.get('desktop',''),
                    _viewer_owned=True,viewer_status='控制页已打开',viewer_error='')

    def _show_cloud_window(self,identity):
        try:
            # 与关闭互斥；关闭先设置取消标记，查看器返回后会立即收尾。
            session=self.sessions.get(identity) or {};cloud=session.get('cloud')
            if not cloud or not cloud.device:raise ValueError('原设备会话已断开，请先关闭该记录，再打开原设备')
            self.update(identity,viewer_status='正在打开控制页',viewer_error='')
            if not cloud.window:
                cloud.window=cloud.provider.open_window(cloud.device.id,cloud.device.name)
            if self.cancel[identity].is_set():return
            cloud.window.show()
            self._bind_cloud_window(identity,cloud.window)
        except Exception as exc:
            self.update(identity,viewer_status='控制页打开失败',viewer_error=str(exc))
            self.store.log(str(exc),run=identity,operation='打开云手机画面')
        finally:
            with self.lock:self._viewer_opening.discard(identity)
            if self.cancel.get(identity) and self.cancel[identity].is_set():self.close_run(identity)

    def update(self, identity, **values):
        with self.lock:
            row=self.store.get_run(identity);row.update(values);self.store.put_run(row)
        return row

    @staticmethod
    def _compare_egress(report, row):
        from .cloud_egress_compare import compare_guest_egress
        if report['status'] == 'mismatch' and row.get('_proxy'):
            try:
                report['proxy_comparison'] = compare_guest_egress(report, row['_proxy'], assigned_at=row.get('proxy_assigned_at'))
            except Exception:
                report['proxy_comparison'] = {'status': 'inconclusive', 'probes': [], 'summary': '原代理复测未完成'}

    def _compare_chrome_routes(self, reading, row, cloud, checked_at):
        from urllib.parse import urlsplit
        from server.python.cloud_phone_s.providers.duoplus.egress import measure_guest, SITES
        self._compare_egress(reading, row)
        result = reading.get('proxy_comparison') or {
            'status': 'inconclusive', 'probes': [], 'summary': '沿用设备代理，没有本机任务代理可供对照'}
        comparison = {**result, 'page_host': reading['page_host'], 'checked_at': checked_at}
        if reading['page_host'] == 'ip111.cn':
            visible = {(p['site'], p['host']) for p in reading['probes'] if p.get('ip')}
            sites = [(name, url) for name, url in SITES if (name, urlsplit(url).hostname) in visible]
            if sites:
                def check():
                    if self.cancel[row['id']].is_set():
                        raise InterruptedError('手机正在关闭')
                try:
                    shell = measure_guest(cloud.provider.api, cloud.device.id, reading['expected_ip'], check,
                                          selected_sites=sites)
                except Exception:
                    shell = {'method': 'android_https', 'status': 'unavailable', 'probes': [],
                             'error': '这次设备系统线路复测未完成'}
                comparison['shell_recheck'] = {**shell, 'checked_at': time.time()}
        return comparison

    def _read_phone_page(self, identity, *, starting=False):
        from server.python.cloud_phone_s.providers.duoplus.egress import read_chrome
        from server.python.cloud_phone_s.duoplus import DuoPlusError
        from server.python.cloud_phone_s.framework.contracts import CloudPhoneError
        from .cloud_egress_compare import merge_chrome_read
        required_status = 'creating' if starting else 'running'
        with self.lock:
            row = self.store.get_run(identity)
            cloud = (self.sessions.get(identity) or {}).get('cloud')
            if (row['provider'] != 'duoplus' or row['status'] != required_status or not cloud or
                    not cloud.device or cloud.device.id != row.get('profile_id') or identity in self.closing):
                raise ValueError('请在本程序正在运行的 DuoPlus 云手机中打开 Chrome 检测页')
        checked_at = time.time()
        expected = (row.get('vendor_proxy') or {}).get('ip') or ''
        reading = {}
        failure = ''
        try:
            nodes = cloud.provider.api.elements(cloud.device.id, mark_web_content=True)
            reading = read_chrome(nodes, expected)
            expected = reading['expected_ip']
            if reading['status'] == 'unavailable':
                failure = reading['error']
        except (DuoPlusError, CloudPhoneError):
            failure = '无法读取当前手机画面，请确认设备已开机且 Chrome 检测页位于前台'
        except ValueError as exc:
            failure = str(exc)
        comparison = None
        if reading.get('status') == 'mismatch' and not starting:
            comparison = self._compare_chrome_routes(reading, row, cloud, checked_at)
        with self.lock:
            latest = self.store.get_run(identity)
            if (latest['status'] != required_status or latest.get('launch') != row.get('launch') or
                    (self.sessions.get(identity) or {}).get('cloud') is not cloud or
                    latest.get('profile_id') != cloud.device.id or identity in self.closing):
                raise ValueError('手机会话已变化，请重新读取当前设备')
            previous = latest.get('chrome_egress') or {}
            if previous.get('launch') != row['launch'] or previous.get('expected_ip') != expected:
                previous = {}
            report = merge_chrome_read(previous, reading,
                launch=row['launch'], expected=expected, checked_at=checked_at, failure=failure, automatic=starting)
            if report['status'] == 'mismatch':
                prior = previous.get('proxy_comparison') or {}
                newer_same_page = any(p.get('page_host') == reading.get('page_host') and
                                      p.get('checked_at', 0) > checked_at for p in previous.get('probes', []))
                if newer_same_page or (prior.get('shell_recheck') and prior.get('page_host') != reading.get('page_host')):
                    comparison = prior
                if comparison:
                    report['proxy_comparison'] = comparison
            self.update(identity, chrome_egress=report)
        return report

    def read_chrome_egress(self, identity):
        report = self._read_phone_page(identity)
        message = ('手机 Chrome 两个检测页的出口一致' if report['status'] == 'verified'
                   else report['error'] or report['last_read_error'] or '尚未核实手机 Chrome 出口')
        return {'chrome_egress': report, 'message': message}

    def _record_vendor_proxy(self, identity, report, location, level, *, retained, candidate=False, allow_state_fallback=True):
        """按厂商返回的实际出口核对地区，保留现场供窗口详情查看。"""
        values={'vendor_candidate':report} if candidate else {
            'vendor_proxy':report, 'ip':report['ip'], 'geo':report['geo'],
            'geo_verified':False, 'proxy_level':level}
        if retained:
            values['source']='设备现有代理（厂商检测）'
            values['protocol']='IPv6' if ':' in report['ip'] else 'IPv4'
        try:
            try:
                proxy.check_location(report,location,level)
            except ValueError as city_error:
                if not allow_state_fallback or level!='city' or not location.get('state'):
                    raise
                # 两次检测可能给出不同城市；只接纳厂商仍能确认同国同州的出口。
                proxy.check_location(report,location,'state')
                level='state'
                if not candidate:
                    values.update(proxy_fallback=True,proxy_fallback_reason=str(city_error),
                                  proxy_warning='厂商检测的出口按同州 / 省核验')
        except ValueError as exc:
            self.update(identity,**values,proxy_last_error=str(exc))
            raise ValueError('厂商侧代理'+str(exc)) from exc
        if not candidate:
            values.update(geo_verified=bool(report.get('geo_verified')),proxy_level=level)
        self.update(identity,**values)

    def _reserve_cloud_slot(self, identity, cloud, wait_seconds):
        deadline=time.monotonic()+wait_seconds
        last_reported=None
        provider=cloud.provider.key
        while True:
            if self.cancel[identity].is_set():raise InterruptedError('已取消等待运行名额')
            limit=self.store.cloud_run_limit(provider)
            with self.lock:
                active=sum(1 for s in self.sessions.values() if s.get('cloud') and s['cloud'].provider.key==provider)
                if active<limit:
                    self.sessions[identity]={'cloud':cloud}
                    return
            if last_reported!=(active,limit):
                self.update(identity,stage=f'等待云手机运行名额（已占 {active} 台，厂商方案上限 {limit} 台）')
                last_reported=(active,limit)
            remaining=deadline-time.monotonic()
            if remaining<=0:
                raise ValueError(f'等待云手机运行名额超时，当前已占 {active} 台，上限 {limit} 台。可关闭已有窗口或调高厂商方案的“最多同时运行”')
            self.cancel[identity].wait(min(.5,remaining))

    def _start(self, identity):
        row=self.store.get_run(identity);key=row['provider'];config=row['_config'];plan=row['_plan'];browser=None;cloud=None;fingerprint=None
        with self.lock:
            if not hasattr(self,'_starting'):self._starting=set()
            self._starting.add(identity)
        try:
            if self.cancel[identity].is_set():raise InterruptedError('已取消排队')
            # 旧出口只能作为历史，不能在本次检测失败后仍显示为当前有效出口。
            self.update(identity,ip='',ipv4='',ipv6='',ip_family='',protocol='',latency=None,
                source='待检测代理' if plan['proxy_sources'] else ('直连' if row['kind']=='desktop' else '设备现有代理（未检测）'),proxy_warning='',proxy_last_error='',
                probe_errors={},geo={},exit_geos=[],geo_verified=False,vendor_proxy={},vendor_candidate={},device_egress={},chrome_egress={},network_report={},preflight_ip='',geo_note='',proxy_level='',proxy_attempts=0,
                proxy_fallback=False,proxy_fallback_reason='',proxy_history=[],proxy_region_strategy='',
                proxy_city_unavailable=False,proxy_city_hint='')
            if row['kind']=='desktop' and key not in ('chrome','ruyi'):
                from .programs import ensure_ready
                self.update(identity,status='creating',stage='检查浏览器客户端',program_required=None)
                manager=ensure_ready(key,config,self.store,cancelled=self.cancel[identity].is_set,
                    on_stage=lambda stage:self.update(identity,stage=stage))
                if manager:self.desktop.configure(key,manager_path=manager[0],manager_sha256=manager[1])
                self.update(identity,_config=config)
            if row['kind']=='desktop' and key=='octo':
                self.update(identity,stage='检查 O 浏览器登录会话')
                browser=providers.build_browser(key,config)
                browser.ensure_logged_in()
            if row['kind']=='desktop' and key=='undetectable' and not row.get('_reuse'):
                # U 指纹库要先确认有匹配配置，再消耗本行代理提取/换 IP 请求。
                browser=providers.build_browser(key,config)
                fingerprint=providers.browser_fingerprint(browser,config,system=row.get('planned_system'))
            location = row['_location'] if row['kind']=='desktop' else _cloud_proxy_location(row)
            allow_state_fallback = _allow_state_fallback(row)
            region_policy = {}
            if row['kind'] != 'desktop':
                region_policy = dict(_location=location, location=location, allow_city_fallback=allow_state_fallback)
            self.update(identity,status='creating',stage='取得代理',**region_policy)
            refresh=bool(row.get('_refresh_proxy') or row.get('_proxy_update_pending'))
            measured={}
            if row.get('_reuse') and not refresh:
                if '_proxy' not in row:raise ValueError('旧保留记录缺少原代理信息，无法保证代理显示正确；请重新创建')
                selected=row['_proxy']
                if selected:
                    self.update(identity,stage='检测原代理')
                    try:
                        level='city' if location.get('city') else 'state' if location.get('state') else 'country'
                        if selected.get('geo_level')=='state' and location.get('state'):level='state'
                        measured=proxy.measure_location(selected,self.store.system(),location,level,ip_family=row.get('_ip_family','any'))
                        measured['proxy_level']=level
                    except Exception as exc:
                        if self.cancel[identity].is_set():raise InterruptedError('已取消代理检测') from exc
                        self.store.log('原代理不可用，重新取得代理：'+proxy.safe_error(exc,selected),
                            run=identity,name=row['name'],provider=key,operation='重取代理')
                        refresh=True
            if not row.get('_reuse') or refresh:
                # 保持该行原先选择的来源；重试读取这些来源的最新账号/模板，绝不偷换默认来源。
                if row.get('_reuse') and row.get('_proxy') and not plan['proxy_sources']:
                    raise ValueError('原环境使用了代理，但旧记录缺少代理来源；未改用直连，请核对原来源配置')
                document=self.store.proxies() if refresh else row.get('_proxy_document',self.store.proxies())
                settings=self.store.system() if refresh else row.get('_proxy_settings',self.store.system())
                if refresh and plan['proxy_sources']:
                    proxy.resolve_selection(document,{'proxy_mode':'selected','proxy_sources':plan['proxy_sources']},{})
                if row.get('_reuse') and refresh and plan['proxy_sources']:
                    providers.check_proxy_update(key)
                def report_proxy(label,attempt,error):
                    self.update(identity,stage=f'检测{label} {attempt}/5',proxy_last_error=error,proxy_attempts=attempt)
                    if error:self.store.log(error,run=identity,name=row['name'],provider=key,operation=f'{label} {attempt}/5')
                selected,measured=proxy.acquire(document,plan['proxy_sources'],location,settings,
                    ip_family=row.get('_ip_family','any'),cancelled=self.cancel[identity].is_set,on_attempt=report_proxy,
                    allow_state_fallback=allow_state_fallback)
                if row['kind'] != 'desktop' and selected:
                    self.update(identity,proxy_assigned_at=time.time())
                self.update(identity,_proxy_document=deepcopy(document),_proxy_settings=deepcopy(settings))
                if measured:
                    self.store.log('代理检测成功：'+str(measured.get('source',''))+'；出口 '+str(measured.get('ip','')),
                        run=identity,name=row['name'],provider=key,operation='代理检测')
                else:self.store.log('沿用设备现有代理' if row['kind']!='desktop' else '本次直连',run=identity,name=row['name'],provider=key,operation='代理检测')
            if row['kind']=='desktop':
                if browser is None:browser=providers.build_browser(key,config)
                from server.python.browser_s.browser_task_setting import CreateBrowserSetting, StartBrowserSetting, ProxySetting
                if not row.get('_reuse') and fingerprint is None:
                    fingerprint=providers.browser_fingerprint(browser,config,system=row.get('planned_system'))
                if self.cancel[identity].is_set():raise InterruptedError('创建前已取消')
                setting=None if not selected else ProxySetting(**{k:selected[k] for k in ('protocol','host','port','username','password')})
                if row.get('_reuse') and refresh and selected:
                    self.update(identity,stage='更新原环境代理',_proxy_update_pending=True)
                    providers.update_browser_proxy(browser,row['profile_id'],setting)
                self.update(identity,_proxy=selected,_proxy_update_pending=False,proxy_warning=selected.get('geo_warning','') if selected else '')
                if measured:self.update(identity,**measured)
                self.update(identity,stage='打开原环境' if row.get('_reuse') else '创建环境')
                if row.get('_reuse'):
                    profile_id=row['profile_id']
                else:
                    # 必须先落盘请求意图；超时/中断/响应落盘失败均不得解释为远端不存在。
                    self.update(identity,creation_state='requesting',creation_attempt_id=uuid.uuid4().hex)
                    profile_id=browser.create_browser(CreateBrowserSetting(row['name'],fingerprint,proxy=setting,group=config.get('group',''))).browser_id
                    if not profile_id:raise RuntimeError('创建响应缺少环境 ID，远端创建结果待确认')
                with self.lock:self.sessions[identity]={'browser':browser,'profile_id':profile_id}
                self.update(identity,profile_id=profile_id,creation_state='created',_start_requested=False,os=row.get('os') if row.get('_reuse') else f'{fingerprint.os} {fingerprint.os_version}',stage='启动浏览器')
                if self.cancel[identity].is_set():raise InterruptedError('已取消，清理刚创建的环境')
                flags=['--disable-backgrounding-occluded-windows','--no-first-run','--disable-save-password-bubble']
                if key in ('chrome','ruyi'):flags.append(plan['url'])
                if key=='morelogin':
                    browser.update_browser(profile_id,{'startupParams':flags})
                if not getattr(browser,'supports_start_flags',True):flags=[]
                self.update(identity,_start_requested=True)
                with self.desktop.start_scope(key,profile_id) as lease:
                    started=browser.start_browser(StartBrowserSetting(profile_id,flags=tuple(flags)))
                hwnd,pid,stamp=self._bind_window(lease,started)
                self.update(identity,pid=pid,created=stamp,hwnd=hwnd,desktop=lease.desktop_name if lease else '',debug_port=started.debug_port or 0,query_protocol='firefox' if key=='ruyi' else 'cdp')
                with self.lock:self.sessions[identity]['lease']=lease
                # 普通目标页只导航一次；进程监测不会使用此查询通道。
                if key not in ('chrome','ruyi') and plan['url']!='about:blank':
                    from .page_query import navigate
                    navigate(started.debug_port,plan['url'])
            else:
                self.update(identity,_proxy=selected,proxy_warning=selected.get('geo_warning','') if selected else '')
                has_region=any(location.get(k) for k in ('country','state','city'))
                if measured:
                    preflight={**measured,'preflight_ip':measured['ip'],'geo_verified':False}
                    if has_region:
                        preflight.update(ip='',geo={})
                    self.update(identity,**preflight)
                from server.python.cloud_phone_s.framework.lifecycle import DeviceRun
                provider=providers.build_cloud(key,config,self.store.root)
                # 只保存复制简称所需的设备字段，不把厂商详情中的设备标识写入窗口账本。
                try:
                    if key=='duoplus':
                        detail=provider.api.request('/api/v1/cloudPhone/info',{'image_id':row['planned_device']})
                        hardware=detail.get('device') or {}
                        summary={'device_brand':str(hardware.get('brand') or hardware.get('manufacturer') or ''),
                                 'device_model':str(hardware.get('model') or ''),'device_system':str(detail.get('os') or '')}
                    else:
                        detail=provider.device(row['planned_device']).public()
                        summary={'device_brand':detail['brand'],'device_model':detail['model'],'device_system':detail['system']}
                    self.update(identity,**summary)
                except Exception as exc:self.store.log('设备简称信息读取失败：'+str(exc),run=identity,provider=key)
                mode=config.get('network_mode','keep')
                follow_proxy_region=key=='duoplus' and selected is not None and has_region
                policy=dict(idleWaitSeconds=config['idle_wait'],ipEnvironment=config.get('ip_environment',False) or follow_proxy_region,
                    networkMode=mode,
                    useTaskProxy=selected is not None,renamePattern=config['rename_pattern'].replace('{','${'),
                    closeAfterRun=True,openWindow=True,apps=[],files=[],deviceBrands=config.get('device_brands',[]),
                    deviceModels=config.get('device_models',[]),deviceVersions=config.get('device_versions',[]))
                self.update(identity,network_mode=mode)
                def verify_cloud_proxy(report, *, candidate=False):
                    level=measured.get('proxy_level') or ('city' if location.get('city') else 'state' if location.get('state') else 'country')
                    if not allow_state_fallback and location.get('city'):
                        level='city'
                    self._record_vendor_proxy(identity,report,location,level,retained=selected is None,candidate=candidate,
                                              allow_state_fallback=allow_state_fallback)

                verify_device = key=='duoplus' and (has_region or selected is not None)
                def verify_guest(device_id, check, switch):
                    from server.python.cloud_phone_s.providers.duoplus.egress import measure_guest
                    current = self.store.get_run(identity)
                    expected = (current.get('vendor_proxy') or {}).get('ip')
                    report = measure_guest(provider.api, device_id, expected, check)
                    report['checked_at'] = time.time()
                    if switch:
                        report['proxy_switch'] = switch
                        if switch != 'confirmed':
                            note = '厂商拒绝启用任务代理' if switch == 'rejected' else '厂商未确认代理开关状态'
                            report['error'] = '；'.join(filter(None, [report['error'], note]))
                    self._compare_egress(report, current)
                    self.update(identity, device_egress=report)
                    if report['status'] == 'mismatch':
                        raise ValueError('设备内出口不一致：' + '、'.join(
                            p['site'] + ' ' + p['ip'] for p in report['probes'] if p.get('ip')))
                    if switch == 'rejected':
                        raise ValueError('厂商拒绝启用本次任务代理')
                    check()
                    self._read_phone_page(identity, starting=True)

                cloud=DeviceRun(provider,policy,[row['planned_device']],on_proxy_checked=verify_cloud_proxy if has_region or verify_device else None,
                    on_candidate_checked=(lambda report:verify_cloud_proxy(report,candidate=True)) if has_region and selected is not None else None,
                    on_guest_check=verify_guest if verify_device else None,
                    on_network_checked=lambda report:self.update(identity,network_report={**report,'checked_at':time.time()}),
                    stop_requested=self.cancel[identity].is_set,on_stage=lambda s:self.update(identity,stage=s),
                    on_ownership=lambda device,started:self.update(identity,profile_id=device.id,_cloud_owned=True,_cloud_started=started,_cloud_resume_unstarted=False))
                self._reserve_cloud_slot(identity,cloud,config['idle_wait'])
                raw=None if not selected else {**selected,'type':selected['protocol']}
                cloud.prepare(owner=identity,project=row['name'],record=identity[:6],proxy=raw)
                self._bind_cloud_window(identity,cloud.window)
                self.update(identity,profile_id=cloud.device.id,os=self.store.get_run(identity).get('device_system') or 'Android')
            if self.cancel[identity].is_set():raise InterruptedError('启动期间已取消')
            self.update(identity,status='running',stage='运行中')
            self.store.log('浏览器已启动' if row['kind']=='desktop' else '云手机已通过厂商接口启动',run=identity,name=row['name'],provider=key,operation='启动')
            if row.get('_website_test') or row.get('_tls_test'):
                try:self.diagnostics.collect(identity)
                except Exception as exc:
                    if row.get('_website_test'):self.update(identity,detection_status='检测失败',detection_error=str(exc))
                    if row.get('_tls_test'):self.update(identity,tls_status='检测失败',tls_error=str(exc))
        except Exception as exc:
            from .programs import ProgramRequiredError
            cancelled=self.cancel[identity].is_set()
            if isinstance(exc,proxy.ProxyDetectionError):self.update(identity,**exc.diagnostics)
            if not cancelled and row['kind']!='desktop' and _cloud_proxy_location(row).get('city') and not _allow_state_fallback(row) and _city_proxy_unavailable(exc):
                self.update(identity,proxy_city_unavailable=True,
                    proxy_city_hint='未取得目标城市的可用代理；建议改为“允许退避到州 / 省”后重试。本次没有自动退避。')
            current=self.store.get_run(identity)
            if current.get('creation_state')=='requesting':
                rejected=(getattr(exc,'provider','')=='octo' and getattr(exc,'operation','')=='create' and
                    getattr(exc,'status_code',None) in (400,401,403,422) and
                    getattr(exc,'code','') in ('invalid_parameters','unauthorized','forbidden'))
                self.update(identity,creation_state='rejected' if rejected else 'uncertain')
            if (key=='octo' and getattr(exc,'operation','')=='start' and
                    (getattr(exc,'code','') in ('login_required','subscription_inactive') or
                     (getattr(exc,'code','') in ('unauthorized','unauthenticated') and getattr(exc,'status_code',None)==401))):
                self.update(identity,_start_rejected=True)
            failed_stage=(getattr(cloud,'failed_stage','') if cloud else '') or self.store.get_run(identity).get('stage','启动')
            self.update(identity,status='cancelled' if cancelled else 'failed',stage='已取消' if cancelled else '启动失败',error='' if cancelled else f'{type(exc).__name__}: {exc}',_start_failed=not cancelled,_failed_stage=failed_stage,
                program_required=exc.public() if isinstance(exc,ProgramRequiredError) and not cancelled else None)
            self.store.log(str(exc),run=identity,name=row['name'],provider=key,operation='启动失败',stage=failed_stage,traceback=traceback.format_exc())
            if identity in self.sessions:
                self._close(identity,failed=not cancelled)
            else:
                if cloud:cloud.provider.close()
                if browser:close_adapter(browser)
                if self.store.get_run(identity).get('profile_id'):self._close(identity,failed=not cancelled)
                elif unresolved_creation(self.store.get_run(identity)):
                    self.update(identity,_cleanup_complete=False,cleanup_error=UNCERTAIN_MESSAGE)
                else:self.update(identity,_cleanup_complete=True)
        finally:
            with self.lock:self._starting.discard(identity)

    @staticmethod
    def _bind_window(lease,started):
        from server.python.browser_s.browser_module_s.browser_window import _window_candidates,get_window_process_id,browser_process_ids_by_port
        deadline=time.monotonic()+25
        while time.monotonic()<deadline:
            # 当前桌面可能同时有厂商管理器窗口。只按本次调试端口的进程族绑定，
            # 不能把父进程中的管理器误当成新浏览器并在收尾时等待它退出。
            pids=browser_process_ids_by_port(started.debug_port,include_parents=False)
            raw_pid=started.raw.get('pid')
            if not pids and raw_pid and psutil.pid_exists(int(raw_pid)):pids.add(int(raw_pid))
            choices=_window_candidates(pids,desktop_handle=lease.desktop_handle if lease else None)
            if choices:
                hwnd=choices[0][2];pid=get_window_process_id(hwnd)
                return hwnd,pid,psutil.Process(pid).create_time()
            time.sleep(.25)
        raise RuntimeError('25秒内未找到本次浏览器在当前桌面上的窗口；环境已记录并进入清理')

    def close_run(self, identity,*,for_action=False):
        self.diagnostics.cancel_window(identity)
        with self.lock:
            row=self.store.find_run(identity)
            if row is None:return
            if not for_action and identity in getattr(self,'_actions',set()):return
            if identity in self.closing:return
            if unresolved_creation(row):raise ValueError(UNCERTAIN_MESSAGE)
            if row['status'] in ('closed','stopped') or (row['status'] in ('failed','cancelled') and (confirmed_absent(row) or row.get('_cleanup_complete'))):return
            if identity in self.cancel:self.cancel[identity].set()
            if identity in getattr(self,'_viewer_opening',set()):
                self.update(identity,stage='等待控制页退出并关机');return
            if row['status'] in ('queued','creating'):
                self.update(identity,stage='等待当前启动阶段退出并清理');return
            self.update(identity,status='closing',stage='等待关闭并清理')
        self.cleanup_pool.submit(self._close,identity)

    def _close(self, identity, failed=False, *, background=False):
        with self.lock:
            if identity in self.closing:return
            if background and identity in getattr(self,'_actions',set()):return
            row=self.store.find_run(identity)
            if row is None:return
            if row['status'] in ('closed','stopped') or row.get('_cleanup_complete'):return
            self.closing.add(identity)
        self.diagnostics.cancel_window(identity)
        browser=None
        try:
            session=self.sessions.get(identity)
            self.update(identity,status='closing',stage='关闭并清理',cleanup_error='',cleanup_attempt_id=uuid.uuid4().hex,cleanup_operation='close',_octo_stop_unavailable=False)
            if unresolved_creation(row) and not (session or {}).get('profile_id'):
                raise RuntimeError(UNCERTAIN_MESSAGE)
            if not confirmed_absent(row) and row.get('_owner')!=self.owner:
                raise RuntimeError('清理记录属于另一电脑、目录或软件实例，未停止或删除其他实例的资源')
            if row['kind']=='desktop':
                profile_id=row['profile_id'] or (session or {}).get('profile_id','')
                if profile_id and not row.get('profile_id'):
                    row=self.update(identity,profile_id=profile_id,creation_state='created',_start_requested=False)
                if profile_id and (failed or row.get('_start_failed')) and not row.get('_deleted'):
                    # 创建成功后启动失败：保留原环境，等待用户选择重试（换代理）或删除。
                    row=self.update(identity,keep=True)
                # 原环境已经停妥，本轮尚未发出启动请求：代理失败无需再连厂商才能解锁记录。
                unstarted_reuse=(not session and row.get('_reuse') and row.get('keep') and
                    row.get('_start_requested') is False and not row.get('pid'))
                if unstarted_reuse:self.update(identity,_closed=True)
                elif profile_id:
                    browser=(session.get('browser') if session else None) or providers.build_browser(row['provider'],row['_config'])
                    close_browser(browser,{**row,'profile_id':profile_id},lambda **v:self.update(identity,**v))
                    self.desktop.release(row['provider'],profile_id)
            elif session and session.get('cloud'):
                session['cloud'].close(force_stop=True)
                session['cloud'].provider.close()
            elif row['profile_id'] and not row.get('_cloud_resume_unstarted'):
                if row.get('_owner')!=self.owner:raise RuntimeError('旧云手机记录缺少本机归属，请在厂商页面核实；未接管未知设备')
                provider=providers.build_cloud(row['provider'],row['_config'],self.store.root)
                try:
                    cloud=recover_cloud(provider,row)
                    self.sessions[identity]={'cloud':cloud}
                    cloud.close(force_stop=True)
                finally:provider.close()
            if row['kind']!='desktop' and row.get('_viewer_owned'):
                close_owned_viewer(row)
            start_failed=failed or row.get('_start_failed')
            self.update(identity,status='failed' if start_failed else ('stopped' if row['keep'] or row['kind']!='desktop' else 'closed'),
                        stage=(str(row.get('_failed_stage') or '启动')+'失败，已收尾') if start_failed else '已收尾',pid=0,created=0,hwnd=0,debug_port=0,_cloud_started=False,_cloud_owned=False,_viewer_owned=False,viewer_status='',_cleanup_complete=True,cleanup_error='',error=row.get('error','') if start_failed else '')
            with self.lock:
                self.sessions.pop(identity,None)
                self._last_cloud_status_check.pop(identity,None)
        except Exception as exc:
            attempts=row.get('_cleanup_attempts',0)+1
            self.update(identity,status='cleanup_failed',stage='清理失败，可重试',cleanup_error=f'{type(exc).__name__}: {exc}',
                        _cleanup_attempts=attempts,_retry_at=time.time()+min(60,attempts*10))
            self.store.log(str(exc),run=identity,operation='清理',traceback=traceback.format_exc())
        finally:
            if browser:close_adapter(browser)
            with self.lock:self.closing.discard(identity)

    def _recover(self):
        """只读取本地账本；不会重新创建或重新执行旧队列。"""
        for snapshot in self.store.unfinished_runs():
            identity=snapshot['id']
            try:
                with self.lock:
                    row=self.store.find_run(identity)
                    if row is None or row.get('updated')!=snapshot.get('updated'):continue
                    if unresolved_creation(row):
                        self.update(identity,status='cleanup_failed',stage='创建结果待确认',creation_state='uncertain',cleanup_error=UNCERTAIN_MESSAGE,_cleanup_complete=False,_cleanup_attempts=3)
                        continue
                    if row['kind']=='desktop' and confirmed_absent(row):
                        self.update(identity,status='cancelled',stage='上次退出已取消，未发出创建请求',_cleanup_complete=True)
                        continue
                    if row.get('_owner')!=self.owner:
                        self.update(identity,status='cleanup_failed',stage='旧记录待确认',cleanup_error='记录来自旧版本或另一实例，未自动接管；请核实原环境后处理',_cleanup_attempts=3)
                        continue
                    if not row.get('profile_id'):
                        self.update(identity,status='cancelled',stage='上次退出已取消排队',_cleanup_complete=True)
                        continue
                    self.cancel[identity]=threading.Event()
                    if row['kind']=='desktop' and row['status']=='running' and alive(row.get('pid'),row.get('created')) and not self.stop.is_set():
                        browser=providers.build_browser(row['provider'],row['_config'])
                        self.sessions[identity]={'browser':browser,'profile_id':row['profile_id']}
                        self.update(identity,stage='已恢复进程监控',connection_phase='已断开')
                        continue
                self._close(identity,background=True)
            except Exception as exc:
                with self.lock:
                    if self.store.find_run(identity) is None:continue
                    self.update(identity,status='cleanup_failed',stage='恢复清理失败，可重试',cleanup_error=str(exc))
                self.store.log(str(exc),run=identity,operation='重启恢复')

    def preserve(self, identity, value):
        if not isinstance(value,bool):raise ValueError('保留状态需要开关值')
        row=self.store.get_run(identity)
        if row.get('account_email') and not value:raise ValueError('邮箱固定环境必须保留；如需移除，请在回收站明确彻底删除')
        if row['kind']!='desktop':raise ValueError('云手机关闭窗口后必须关机，不提供保留运行开关')
        if row['status'] in ('closing','closed'):raise ValueError('已开始清理的环境不能再保留')
        return self.update(identity,keep=value)

    def _monitor(self):
        while not self.stop.wait(MONITOR_SECONDS):
            try:
                now=time.monotonic()
                if now>=self._next_trash_purge:
                    self._next_trash_purge=now+3600
                    for expired in self.store.expired_trash(time.time()-30*86400):
                        try:self.purge_runs([expired])
                        except Exception as exc:self.store.log(str(exc),run=expired,operation='回收站到期删除')
                with self.lock:identities=list(self.sessions)
                identities=list(dict.fromkeys(identities+[r['id'] for r in self.store.unfinished_runs() if r['status']=='cleanup_failed' and r.get('_owner')==self.owner]))
                for identity in identities:
                    with self.lock:
                        if identity in getattr(self,'_actions',set()):continue
                        row=self.store.find_run(identity)
                    if row is None:continue
                    if row['status']=='running' and row['kind']=='desktop' and not alive(row['pid'],row['created']):self.close_run(identity)
                    elif row['status']=='running' and row['kind']!='desktop':
                        session=self.sessions.get(identity)
                        window=getattr((session or {}).get('cloud'),'window',None)
                        if row.get('_viewer_owned') and window and not window.is_open():
                            self.close_run(identity);continue
                        now=time.monotonic()
                        if now-self._last_cloud_status_check.get(identity,0)>=15:
                            self._last_cloud_status_check[identity]=now
                            session=self.sessions.get(identity)
                            if session and session.get('cloud') and session['cloud'].device:
                                if session['cloud'].provider.runtime_state(row['profile_id']).status=='idle':self.close_run(identity)
                    elif row['status']=='cleanup_failed' and row.get('_cleanup_attempts',0)<3 and time.time()>=row.get('_retry_at',0):self.close_run(identity)
                self.desktop.reap_retired()
            except Exception as exc:self.store.log(str(exc),operation='进程监测')

    def shutdown(self):
        with self.lock:
            if self.stop.is_set():return
            self.stop.set()
            for event in self.cancel.values():event.set()
            for row in self.store.unfinished_runs():
                if row['status']=='queued' and not row.get('profile_id'):
                    self.update(row['id'],status='cancelled',stage='退出软件，已取消排队',_cleanup_complete=True)
        self.diagnostics.close()
        for identity in list(self.sessions):self.close_run(identity)
        self.pool.shutdown(wait=False,cancel_futures=True)
        self.cleanup_pool.shutdown(wait=False)
