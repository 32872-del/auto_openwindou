"""按需检测/存档与逐站上传。浏览器存活仍只由 Engine 的进程监测负责。"""
import os
from pathlib import Path
import threading
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin
import requests
from .settings import local_path
from vendor_runtime.legacy_core.environment_identity import new_client_number, new_launch_id, is_launch_id
from vendor_runtime.legacy_core.environment_schema import build_event, new_environment_id
from vendor_runtime.legacy_core.environment_store import EnvironmentStore, event_page_dir, assets_dir
from vendor_runtime.legacy_core.environment_sync import EnvironmentSync, test_connection

SITE_LABELS={'browserscan':'BrowserScan','creepjs':'CreepJS','browserleaks':'BrowserLeaks','scamalytics':'Scamalytics','ip2proxy':'IP2Proxy','leakdig':'LeakDig','tls':'TLS Excel'}


class Diagnostics:
    def __init__(self,store,update):
        self.store=store;self.update=update;self.stop=threading.Event();self.lock=threading.Lock();self.active=set();self.threads=set()
        self.window_tasks={}
        self.tls_archive_lock=threading.RLock()
        from .tls_report import TlsReport
        self.tls_report=TlsReport(store)
        self.archive_store=EnvironmentStore(str(store.paths.state/'检测记录.sqlite'))
        self.identity=store.read('状态/存档身份.json',{})
        if not self.identity:
            self.identity=store.write('状态/存档身份.json',{'client_id':str(uuid.uuid4()),'client_number':new_client_number()})
        self.sync=EnvironmentSync(self.archive_store,{})
        self.configure();self.sync.start()

    def configure(self):
        s=self.store.system()
        os.environ['WINDOW_STUDIO_ARCHIVES']=str(local_path(self.store.root,s['archive_dir']))
        if self.tls_report.captures():
            try:self.export_tls()
            except Exception as exc:self.store.log(str(exc),operation='TLS 存档补存')
        if s['sync_url']:
            queued=self.archive_store.enqueue_missing_local_archives()
            if queued['events'] or queued['pages']:
                self.store.log('旧本地存档已加入待上传队列',**queued)
        self.sync.configure({'enabled':s['sync_auto'],'server_url':s['sync_url'],'timeout':s['sync_timeout']})

    def _thread(self,key,func):
        with self.lock:
            if self.stop.is_set():raise ValueError('软件正在退出，不能启动新的短任务')
            if key in self.active:raise ValueError('这个窗口的操作正在进行，请等待完成')
            self.active.add(key)
        def run():
            try:func()
            except Exception as exc:self.store.log(str(exc),operation=key,traceback=traceback.format_exc())
            finally:
                with self.lock:self.active.discard(key);self.threads.discard(threading.current_thread())
        t=threading.Thread(target=run,name=key,daemon=True)
        with self.lock:self.threads.add(t)
        t.start()

    def collect(self,identity):
        row=self.store.get_run(identity)
        if row.get('provider')=='ruyi':raise ValueError('如意使用 Firefox 通道，自动网站检测仅支持 Chromium')
        if row['kind']!='desktop' or not row.get('debug_port'):raise ValueError('此环境没有可用的网页查询通道，不能启动网站检测')
        self._window_thread(identity,'网站检测',lambda event:self._collect_selected(identity,event))

    def _collect_selected(self,identity,cancel_event):
        row=self.store.get_run(identity)
        if row.get('_tls_test'):
            self.update(identity,connection_phase='TLS 检测')
            self._collect_tls(identity,cancel_event)
        if row.get('_website_test',True):
            self.update(identity,connection_phase='网站检测')
            self._collect(identity,cancel_event)

    def _archive_event(self,identity):
        """各检测项目共用同一窗口身份、启动目录和已有检查结果。"""
        row=self.store.get_run(identity)
        environment_id=row.get('environment_id') or new_environment_id()
        launch=row.get('archive_launch') or row['launch']
        if not is_launch_id(launch):launch=new_launch_id()
        history=self.archive_store.get_environment(environment_id)
        previous=history['latest'] if history and history['latest']['launch_id']==launch else {}
        source=previous.get('source') or {**self.identity,'client_name':self.store.system()['alias']}
        self.update(identity,environment_id=environment_id,archive_launch=launch)
        return build_event(environment_id=environment_id,launch_id=launch,capture_reason='manual',source=source,
            profile={'id':row['profile_id'],'name':row['name'],'browser':row['provider'],'os_label':row.get('os',''),'proxy_type':row.get('source','')},
            page_snapshot={'page':previous.get('page',{}),'fingerprint':previous.get('fingerprint',{})},
            ip_check={'actual_ip':row.get('ip','')},checks=previous.get('checks',{}))

    def _tls_archive_event(self,capture):
        try:event=self._archive_event(capture['run_id'])
        except ValueError:
            # 原窗口记录删除后，仍能归档旧版 TLS 账本中的有效检测数据。
            event=build_event(environment_id=new_environment_id(),launch_id=new_launch_id(),capture_reason='manual',
                source={**self.identity,'client_name':self.store.system()['alias']},
                profile={'id':'','name':capture['email'] or 'TLS 历史记录','browser':''},page_snapshot={},ip_check={},checks={})
        fields={'captured_at':capture['captured_at']}
        if capture['email']:fields['email']=capture['email']
        event['checks']['tls']={'status':'complete','fields':fields,'url':'https://tls.peet.ws/',
                                'error':'','page':'tls_'+capture['id']+'.xlsx'}
        return event

    def _sync_tls_archives(self,environment=None):
        """从已保存的检测值补建文件，接入网页存档的事件和文件补传队列。"""
        import json
        saved={};errors=[]
        with self.tls_archive_lock:
            for capture in self.tls_report.captures():
                try:
                    event=json.loads(capture['archive_event']) if capture['archive_event'] else self._tls_archive_event(capture)
                    if environment and event['environment_id']!=environment:continue
                    if not capture['archive_event']:
                        self.tls_report.attach_archive(capture['id'],event)
                    # 先保存关联，文件被占用时，检测记录仍可显示并提供补存入口。
                    self.archive_store.save_event(event,queue_remote=True)
                    target=Path(event_page_dir(event))/event['checks']['tls']['page']
                    if not target.is_file():
                        self.tls_report.export(capture_id=capture['id'],path=target)
                    saved[capture['id']]=target
                except Exception as exc:
                    errors.append(str(exc))
                    self.store.log(str(exc),run=capture['run_id'],operation='TLS 存档补存')
            self.archive_store.enqueue_missing_local_archives()
            self.sync.trigger()
        if errors:raise ValueError('；'.join(errors[:3]))
        return saved

    def _collect_tls(self,identity,cancel_event):
        from .tls_check import collect_tls, close_tls_page
        from .table_import import is_email
        row=self.store.get_run(identity)
        recorded=False;target={}
        self.update(identity,tls_status='正在检测',tls_error='',tls_capture_id='')
        try:
            values=collect_tls(row['debug_port'],timeout=self.store.system()['site_timeout'],cancel_event=cancel_event,on_target=target.update)
            if cancel_event.is_set():raise InterruptedError('TLS 检测已取消')
            captured_at=datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds')
            capture_id=uuid.uuid4().hex
            email=row.get('input_data',{}).get('email','')
            if not email and is_email(row['name']):email=row['name']
            self.tls_report.record(capture_id,identity,email,captured_at,values)
            recorded=True
            self.update(identity,tls_capture_id=capture_id,tls_captured_at=captured_at,tls_status='等待写入 Excel')
            self.export_tls()
            self.store.log('TLS 检测已写入 Excel',run=identity,operation='TLS 检测')
            try:close_tls_page(row['debug_port'],target)
            except Exception as exc:
                # 已落盘的检测结果不能因收起页面失败而标成检测失败。
                self.store.log('TLS 已保存，但检测页未能自动关闭：'+str(exc),run=identity,operation='TLS 检测页关闭')
        except InterruptedError as exc:
            self.update(identity,tls_status='已取消',tls_error=str(exc))
        except Exception as exc:
            self.update(identity,tls_status='等待补写 Excel' if recorded else '检测失败',tls_error=str(exc))
            self.store.log(str(exc),run=identity,operation='TLS 检测',traceback=traceback.format_exc())

    def export_tls(self):
        saved=self._sync_tls_archives()
        path,records=self.tls_report.export()
        for capture_id,identity in records:
            if capture_id not in saved:continue
            try:row=self.store.get_run(identity)
            except ValueError:continue
            archive_path=str(saved[capture_id])
            if row.get('tls_capture_id')==capture_id and (row.get('tls_status')!='已保存 Excel' or row.get('tls_file')!=archive_path):
                self.update(identity,tls_status='已保存 Excel',tls_error='',tls_file=archive_path)
        return path

    def _window_thread(self,identity,label,func):
        from .short_task import verify_connection
        verify_connection(self.store.get_run(identity))
        with self.lock:
            if self.stop.is_set():raise ValueError('软件正在退出，不能连接浏览器')
            if identity in self.window_tasks:raise ValueError('此窗口正在'+self.window_tasks[identity][0]+'，请等待结束或取消')
            event=threading.Event();self.window_tasks[identity]=(label,event)
        def run():
            try:
                if event.is_set():raise InterruptedError('任务已取消，未连接浏览器')
                verify_connection(self.store.get_run(identity))
                self.update(identity,connection_phase=label)
                func(event)
            finally:
                try:self.update(identity,connection_phase='已断开')
                finally:
                    with self.lock:self.window_tasks.pop(identity,None)
        try:self._thread('窗口短任务:'+identity,run)
        except BaseException:
            with self.lock:self.window_tasks.pop(identity,None)
            raise

    def cancel_window(self,identity):
        with self.lock:
            task=self.window_tasks.get(identity)
            if task:task[1].set()
        return {'active':bool(task),'message':'已请求取消本窗口的短任务' if task else '本窗口没有正在进行的短任务'}

    def _collect(self,identity,cancel_event):
        from vendor_runtime.legacy_core.web_monitor import open_environment_check_pages, environment_check_urls, collect_page_snapshot
        from vendor_runtime.legacy_core.environment_check_process import collect_diagnostic_sites
        row=self.store.get_run(identity);settings=self.store.system()
        base=self._archive_event(identity)
        environment_id=base['environment_id'];launch=base['launch_id']
        # 中转站的名称/搜索归属统一使用本机操作人别名；窗口名仍在 profile.name。
        source=base['source'];profile=base['profile']
        ip={'actual_ip':row.get('ip','')};snapshot={};checks=base['checks']
        self.update(identity,environment_id=environment_id,archive_launch=launch,detection_status='正在检测')
        def save(part):
            nonlocal checks
            for site,item in part.get('checks',{}).items():
                if item.get('page') or site not in checks:checks[site]=item
            event=build_event(environment_id=environment_id,launch_id=launch,capture_reason='manual',source=source,profile=profile,page_snapshot=snapshot,ip_check=ip,checks=checks)
            self.archive_store.save_event(event,queue_remote=True);self.sync.trigger()
        try:
            if cancel_event.is_set():raise InterruptedError('网站检测已取消')
            folder=Path(event_page_dir(base));folder.mkdir(parents=True,exist_ok=True)
            open_environment_check_pages(row['debug_port'],ip_address=row.get('ip',''),focus_url=row.get('url',''))
            if cancel_event.is_set():raise InterruptedError('网站检测已取消')
            try:snapshot=collect_page_snapshot(row['debug_port'],requested_url=row.get('url',''),timeout=12)
            except Exception as exc:self.store.log(str(exc),run=identity,operation='基础环境采集')
            urls={item['name'].lower():item['url'] for item in environment_check_urls(row.get('ip',''))}
            result=collect_diagnostic_sites(environment_id=environment_id,profile_id=row['profile_id'],debug_port=row['debug_port'],urls=urls,
                timeout=settings['site_timeout'],close_completed=True,page_dir=str(folder),assets_dir=assets_dir(),cancel_event=cancel_event,on_progress=save)
            save(result)
            errors=[f'{SITE_LABELS[k]}：{checks.get(k,{}).get("error") or "尚未保存"}' for k in ('browserscan','creepjs') if not checks.get(k,{}).get('page')]
            self.update(identity,detection_status='存档未完整' if errors else '本地已保存',detection_error='；'.join(errors))
        except InterruptedError as exc:
            self.update(identity,detection_status='已取消',detection_error=str(exc))
        except Exception as exc:
            self.update(identity,detection_status='检测失败',detection_error=f'{type(exc).__name__}: {exc}')
            raise

    def entries(self,query=''):
        summaries=self.archive_store.search_environments(query,limit=500)
        outbox=self.archive_store.page_outbox_snapshot();states={}
        for item in outbox:states.setdefault((item['event_id'],item['site']),item)
        result=[]
        for summary in summaries:
            event=self.archive_store.get_event(summary['event_id'])
            if not event:continue
            folder=Path(event_page_dir(event));sites=[]
            for site,item in event.get('checks',{}).items():
                filename=item.get('page','');local=bool(filename and (folder/filename).is_file());status=states.get((event['event_id'],site),{})
                if 'tls' in event['checks'] and not any(item.get(key) for key in ('page','fields','url','error')):continue
                if not filename and site not in ('browserscan','creepjs'):continue
                sites.append({'environment_id':summary['environment_id'],'site':site,'label':SITE_LABELS.get(site,site),'local':local,'remote':status.get('status')=='sent',
                    'recoverable':site=='tls' and bool(filename),
                    'status':status.get('status','local' if local else 'missing'),'error':status.get('last_error','') or item.get('error','')})
            result.append({**summary,'sites':sites})
        return result

    def for_run(self,identity):
        environment=self.store.get_run(identity).get('environment_id')
        return next((x['sites'] for x in self.entries(environment) if x['environment_id']==environment),[]) if environment else []

    def open_archive(self,environment,site,mode='auto'):
        value=self.archive_store.get_environment(environment)
        if not value:raise ValueError('未找到检测存档')
        event=value['latest'];filename=(event.get('checks',{}).get(site) or {}).get('page','')
        if not filename:raise ValueError('这个网站尚未保存，不能打开')
        path=Path(event_page_dir(event))/filename
        if mode!='remote' and site=='tls' and not path.is_file():self._sync_tls_archives(environment)
        if mode!='remote' and path.is_file():os.startfile(path);return {'message':'已打开本地检测存档'}
        if mode=='local':raise ValueError('本地存档文件已不存在')
        s=self.store.system()
        if not s['sync_url']:raise ValueError('本地文件缺失，且尚未配置中转站地址')
        with requests.Session() as session:
            session.trust_env=False
            response=session.get(s['sync_url'].rstrip('/')+'/api/fingerprint/search',params={'q':environment},timeout=s['sync_timeout'])
            response.raise_for_status();data=response.json()
        candidates=data if isinstance(data,list) else data.get('items',data.get('rows',data.get('results',[])))
        remote=next((item.get('page_urls',{}).get(site) for item in candidates if item.get('environment_id')==environment),None)
        if not remote:raise ValueError('中转站未返回该站存档地址，请查看上传状态')
        import webbrowser
        webbrowser.open(urljoin(s['sync_url'].rstrip('/')+'/',remote));return {'message':'已打开中转站存档'}

    def retry_uploads(self):
        s=self.store.system()
        if not s['sync_url']:raise ValueError('请先在系统设置填写中转站地址')
        self._sync_tls_archives()
        queued=self.archive_store.retry_unsent_now()
        self.sync.trigger()
        return {'message':f'已开始补传 {queued["events"]} 条记录、{queued["pages"]} 份存档文件；确认结果会更新在检测记录中'}

    def solve_v2(self,identity):
        row=self.store.get_run(identity);s=self.store.system()
        if row.get('provider')=='ruyi':raise ValueError('如意使用 Firefox 通道，当前 V2 按钮仅支持 Chromium')
        if row['kind']!='desktop' or row['status']!='running' or not row.get('debug_port'):raise ValueError('V2需要运行中的兼容网页环境')
        if not s['v2_key']:raise ValueError('请先在系统设置 → reCAPTCHA V2 填写服务密钥')
        def run(cancel_event):
            from .short_task import run_worker
            context={'run':identity,'name':row['name'],'provider':row['provider'],'operation':'V2'}
            def progress(message):
                self.update(identity,v2='处理中',v2_detail=str(message))
            try:
                result=run_worker('launcher.v2_worker',{'debug_port':row['debug_port'],'api_url':s['v2_url'],'client_key':s['v2_key'],'wait_count':s['v2_wait_count']},
                    timeout=min(300,30+s['v2_wait_count']*3),cancel_event=cancel_event,on_progress=progress)
                self.update(identity,v2='已传入页面' if result.get('success') else '失败',v2_detail=result.get('error',''))
                self.store.log('已传入页面' if result.get('success') else '失败：'+str(result.get('error','未知原因')),
                    sitekey=result.get('sitekey',''),**context)
            except InterruptedError as exc:
                self.update(identity,v2='已取消',v2_detail=str(exc));self.store.log('已取消：'+str(exc),**context)
            except Exception as exc:
                self.update(identity,v2='失败',v2_detail=str(exc));self.store.log('失败：'+str(exc),**context);raise
        self._window_thread(identity,'V2',run)
        return {'message':'V2请求已开始，可在本行查看状态'}

    def close(self):
        self.stop.set()
        with self.lock:
            for _,event in self.window_tasks.values():event.set()
        self.sync.stop()
