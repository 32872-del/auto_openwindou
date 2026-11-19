from contextlib import asynccontextmanager
import json
from pathlib import Path
import secrets
import traceback
import uuid
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from .catalog import PROVIDERS, BY_KEY, validate
from .storage import Store, Conflict
from .providers import initialize, probe
from . import proxy


def public_row(row):
    return {k:v for k,v in row.items() if not k.startswith('_')}


def create_app(root, *, token=None, engine_factory=None):
    root=Path(root).resolve();store=Store(root);initialize(root)
    token=token or secrets.token_urlsafe(32)
    @asynccontextmanager
    async def lifespan(app):
        if engine_factory is None:
            from .engine import Engine
            app.state.engine=Engine(store)
        else:app.state.engine=engine_factory(store)
        yield
        if app.state.engine:app.state.engine.shutdown()
    app=FastAPI(title='自动创建窗口',docs_url=None,redoc_url=None,lifespan=lifespan)
    app.state.store=store;app.state.token=token

    @app.middleware('http')
    async def authenticate(request, call_next):
        if request.url.path.startswith('/api/') and not secrets.compare_digest(request.headers.get('X-Window-Token',''),token):
            return JSONResponse({'message':'此窗口的连接已失效，请重新打开软件'},status_code=401)
        response=await call_next(request)
        response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'
        return response

    @app.exception_handler(Conflict)
    async def conflict(request,exc):return JSONResponse({'message':str(exc)},status_code=409)
    @app.exception_handler(ValueError)
    async def bad(request,exc):
        from .programs import ProgramRequiredError
        return JSONResponse(exc.public() if isinstance(exc,ProgramRequiredError) else {'message':str(exc)},status_code=422)
    @app.exception_handler(Exception)
    async def error(request,exc):
        identity=uuid.uuid4().hex[:12]
        store.log(str(exc),operation=request.method+' '+request.url.path,error_id=identity,traceback=traceback.format_exc())
        return JSONResponse({'message':f'{type(exc).__name__}: {exc}','error_id':identity},status_code=500)

    @app.get('/',response_class=HTMLResponse)
    def index():
        content=(Path(__file__).resolve().parents[1]/'ui/index.html').read_text(encoding='utf-8')
        return content.replace('__API_TOKEN__',json.dumps(token))

    @app.get('/api/bootstrap')
    def bootstrap():
        from .ui_preferences import read
        return {'providers':PROVIDERS,'system':store.system(),'paths':store.paths.public(),'preferences':read(store)}

    @app.get('/api/ui/preferences')
    def get_ui_preferences():
        from .ui_preferences import read
        return read(store)

    @app.put('/api/ui/preferences')
    def save_ui_preferences(body:dict):
        from .ui_preferences import save
        return save(store,body)

    @app.get('/api/locations')
    def locations():
        return json.loads((Path(__file__).parent/'resources/美加城市目录.json').read_text(encoding='utf-8'))

    @app.get('/api/providers/{key}')
    def provider(key:str):return store.provider(key)
    @app.put('/api/providers/{key}')
    def save_provider(key:str,body:dict):return store.save_provider(key,body)
    @app.post('/api/providers/{key}/probe')
    def provider_probe(key:str,body:dict):
        config=body.get('settings',store.provider(key)['settings'])
        if BY_KEY[key]['family']=='cloud':config={**config,'device_mode':'weighted'}
        if key in ('octo','hub'):config={**config,'android_mode':'all'}
        config=validate(key,config)
        result=probe(key,config,root)
        store.log(result['message'],provider=key,operation='探测连接')
        return result

    @app.get('/api/plans/{kind}/{key}')
    def plan(kind:str,key:str):return store.plan(kind,key)
    @app.put('/api/plans/{kind}/{key}')
    def save_plan(kind:str,key:str,body:dict):return store.save_plan(kind,key,body)

    @app.post('/api/providers/{key}/models')
    def provider_models(key:str,body:dict):
        from .providers import device_models
        refresh=body.get('refresh',False)
        if not isinstance(refresh,bool):raise ValueError('刷新标记无效')
        config=store.provider(key)['settings'] if 'settings' not in body else body['settings']
        return device_models(key,config,refresh=refresh,root=root)

    @app.post('/api/providers/{key}/programs')
    def provider_programs(key:str,body:dict):
        from .programs import probe as program_probe
        config=body.get('settings',store.provider(key)['settings'])
        return program_probe(key,config,store,force=True,candidate_id=body.get('candidate_id',''))

    @app.post('/api/providers/{key}/program-binding')
    def provider_program_binding(key:str,body:dict):
        from .programs import bind_path
        return bind_path(key,body.get('path'),store)

    @app.post('/api/providers/{key}/catalog/{section}')
    def provider_catalog(key:str,section:str,body:dict):
        from .providers import browser_catalog
        if not isinstance(body.get('refresh',False),bool):raise ValueError('刷新标记无效')
        return browser_catalog(key,body.get('settings',store.provider(key)['settings']),section,
                               filters=body.get('filters'),refresh=body.get('refresh',False),root=root)

    @app.post('/api/providers/{key}/cloud/{section}')
    def cloud_catalog(key:str,section:str,body:dict):
        from .providers import cloud_catalog as call
        if section=='reset':
            identity=str(body.get('id',''))
            if any(r['provider']==key and identity in (r.get('profile_id'),r.get('planned_device')) for r in store.unfinished_runs()):
                raise ValueError('这台设备正在本软件的队列或运行记录中，请先停止并完成清理后单独换机')
        result=call(key,body.get('settings',store.provider(key)['settings']),root,section,body)
        if section=='reset':store.log(result['message'],provider=key,operation='显式换机')
        return result

    @app.get('/api/proxies')
    def proxies():return store.proxies()
    @app.post('/api/proxies/import-preview')
    def proxy_import_preview(body:dict):
        from .proxy_import import preview
        return preview(body.get('text'),store.proxies())
    @app.post('/api/proxies/parse')
    def proxy_parse(body:dict):return proxy.parse_source(body.get('value'),body.get('protocol','socks5'),preview=True)
    @app.put('/api/proxies')
    def save_proxies(body:dict):
        return store.save_proxies(body)
    @app.post('/api/proxies/{identity}/test')
    def test_proxy(identity:str,body:dict):
        from .location import normalize
        location=normalize(body.get('location',{}))
        if not isinstance(body.get('rotate',False),bool):raise ValueError('换 IP 应为开关值')
        document=store.proxies()
        source=next((item for item in document['sources'] if item['id']==identity),None)
        if source is None:raise ValueError('此代理来源已不存在，请刷新列表')
        if source['source_type']=='rotate' and not source.get('host') and not body.get('rotate'):
            raise ValueError('此来源由换 IP 链接返回代理地址，请点“请求新代理并检测”')
        _,measured=proxy.acquire(document,[identity],location,store.system(),
            ip_family=body.get('ip_family','any'),request_rotation=body.get('rotate',False),purpose='test')
        return measured

    @app.get('/api/system')
    def system():return store.system()
    @app.put('/api/system')
    def save_system(body:dict):
        store.revision(body)
        from .settings import validate_system
        value=store.save_system(validate_system(body,root),body['revision'])
        if getattr(app.state.engine,'diagnostics',None):app.state.engine.diagnostics.configure()
        return value
    @app.put('/api/operator')
    def operator(body:dict):
        alias=body.get('alias')
        if not isinstance(alias,str) or not 1<=len(alias.strip())<=40:raise ValueError('请填写 1～40 字的操作人别名')
        store.write('状态/操作人.json',{'alias':alias.strip()})
        if getattr(app.state.engine,'diagnostics',None):app.state.engine.diagnostics.configure()
        return store.system()

    @app.get('/api/runs/{identity}/history')
    def run_history(identity:str):
        diag=getattr(app.state.engine,'diagnostics',None)
        return {'events':store.history(identity),'archives':diag.for_run(identity) if diag else []}

    @app.post('/api/runs/{identity}/device-egress')
    def read_phone_chrome_egress(identity: str):
        return app.state.engine.read_chrome_egress(identity)

    @app.get('/api/features')
    def features():return {'text':(Path(__file__).resolve().parents[1]/'功能清单.md').read_text(encoding='utf-8')}
    @app.get('/api/config/export')
    def config_export():
        from .config_bundle import export_bundle
        return Response(export_bundle(store),media_type='application/json',headers={'Content-Disposition':"attachment; filename*=UTF-8''%E5%85%B1%E7%94%A8%E9%85%8D%E7%BD%AE.json"})
    @app.post('/api/config/preview')
    async def config_preview(request:Request):
        from .config_bundle import preview_bundle
        content=bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content)>20_000_000:raise ValueError('配置包超过 20 MB')
        return preview_bundle(store,bytes(content))
    @app.post('/api/config/apply')
    def config_apply(body:dict):
        from .config_bundle import apply_bundle
        result=apply_bundle(store,body.get('token'))
        if getattr(app.state.engine,'diagnostics',None):app.state.engine.diagnostics.configure()
        return result

    @app.get('/api/runs')
    def runs(offset:int=0,query:str='',category:str='all',kind:str='all',descending:bool=False):
        if offset<0:raise ValueError('页码无效')
        value=store.runs(offset=offset,query=query,category=category,kind=kind,descending=descending);value['rows']=[public_row(r) for r in value['rows']];return value
    @app.get('/api/runs/{identity}/edit-options')
    def run_edit_options(identity:str):
        from .run_edit import edit_options
        return edit_options(app.state.engine,identity)
    @app.put('/api/runs/{identity}')
    def edit_run(identity:str,body:dict):
        from .run_edit import edit_run as save_run
        return public_row(save_run(app.state.engine,identity,body))
    @app.get('/api/retained-u-browsers')
    def retained_u_browsers():return {'rows':store.kept_u_browsers()}
    @app.post('/api/runs/{identity}/trash')
    def trash(identity:str):
        result=app.state.engine.trash_runs([identity])
        row=store.find_run(identity)
        return {**(public_row(row) if row else {'id':identity,'deleted':True}),**result}
    @app.post('/api/runs/{identity}/restore')
    def restore(identity:str):return public_row(store.archive_run(identity,restore=True))
    @app.delete('/api/runs/{identity}')
    def purge(identity:str):return app.state.engine.purge_runs([identity])
    @app.post('/api/run-batch/trash')
    def trash_batch(body:dict):return app.state.engine.trash_runs(body.get('ids'))
    @app.post('/api/run-batch/open')
    def open_batch(body:dict):return app.state.engine.open_runs(body.get('ids'))
    @app.post('/api/run-batch/restore')
    def restore_batch(body:dict):return app.state.engine.restore_runs(body.get('ids'))
    @app.post('/api/run-batch/purge')
    def purge_batch(body:dict):return app.state.engine.purge_runs(body.get('ids'))
    @app.post('/api/run-batch/remove-local')
    def remove_local_batch(body:dict):return app.state.engine.remove_local_records(body.get('ids'))
    @app.get('/api/events')
    def events(after:int=0):
        result=store.events(after)
        for event in result['events']:event['row']=public_row(event['row'])
        return result
    @app.post('/api/runs')
    def start(body:dict):
        if body.get('email_mode'):
            from .table_import import fixed_item
            items=body.get('items')
            if not isinstance(items,list) or not 1<=len(items)<=100:raise ValueError('请先校验并带入 1～100 行五列或八列邮箱资料')
            for i,item in enumerate(items):
                if not isinstance(item,dict):raise ValueError('邮箱资料格式错误')
                fixed_item(item,row_number=i+1)
        return {'ids':app.state.engine.submit(body)}
    @app.post('/api/import/preview')
    def import_preview(body:dict):
        from .table_import import preview
        result=preview(body)
        if body.get('email_mode'):
            from .email_accounts import describe_accounts
            accounts=describe_accounts(store,[row['item'] for row in result['rows']])
            for row,account in zip(result['rows'],accounts):
                row['account']=account
                # 本步只校验资料格式；绑定状态在下一步选择环境类型后校验。
            result['valid']=all(not row['error'] for row in result['rows'])
        return result
    @app.post('/api/email-accounts/preview')
    def email_accounts_preview(body:dict):
        from .table_import import validate_item
        from .email_accounts import describe_accounts
        items=body.get('items')
        if not isinstance(items,list) or not 1<=len(items)<=100:raise ValueError('请带入 1～100 条邮箱资料')
        return {'rows':describe_accounts(store,[validate_item(item,row_number=i+1) for i,item in enumerate(items)],kind=body.get('kind'))}
    @app.post('/api/email-systems/preview')
    def email_systems_preview(body:dict):
        from .table_import import validate_item
        from .system_assignment import preview
        items=body.get('items')
        if not isinstance(items,list) or not 1<=len(items)<=100:raise ValueError('请带入 1～100 条邮箱资料')
        return preview(store,[validate_item(item,row_number=i+1) for i,item in enumerate(items)],
                       kind=body.get('kind'),provider=body.get('provider'),selection=body.get('system_selection'))
    @app.post('/api/runs/{identity}/recreate')
    def recreate(identity:str,body:dict):
        retry=body.get('retry',False)
        if not isinstance(retry,bool):raise ValueError('重试标记无效')
        return {'ids':app.state.engine.recreate(identity,retry=retry)}
    @app.post('/api/runs/{identity}/retry')
    def retry(identity:str):return {'ids':app.state.engine.retry_run(identity)}
    @app.post('/api/runs/{identity}/resume')
    def resume(identity:str):return app.state.engine.resume(identity)
    @app.post('/api/runs/{identity}/v2')
    def v2(identity:str):return app.state.engine.diagnostics.solve_v2(identity)
    @app.post('/api/runs/{identity}/cancel-query')
    def cancel_query(identity:str):return app.state.engine.diagnostics.cancel_window(identity)
    @app.post('/api/tls-report/open')
    def open_tls_report():
        import os
        path=app.state.engine.diagnostics.export_tls()
        os.startfile(path)
        return {'message':'已打开 TLS 检测 Excel','path':str(path)}

    @app.get('/api/archives')
    def archives(query:str=''):
        diag=getattr(app.state.engine,'diagnostics',None)
        return {'rows':diag.entries(query) if diag else []}
    @app.post('/api/archives/open')
    def open_archive(body:dict):return app.state.engine.diagnostics.open_archive(body.get('environment_id',''),body.get('site',''),body.get('mode','auto'))
    @app.post('/api/archives/retry')
    def retry_archives(body:dict):return app.state.engine.diagnostics.retry_uploads()
    @app.post('/api/relay/test')
    def relay_test(body:dict):
        from .settings import validate_system
        from vendor_runtime.legacy_core.environment_sync import test_connection
        value=validate_system({**store.system(),**body},root)
        ok,message=test_connection({'server_url':value['sync_url'],'timeout':value['sync_timeout']})
        return {'ok':ok,'message':message}
    @app.get('/api/weights')
    def weights():return store.read('配置/随机比例.json',{'revision':0,'weights':{}})
    @app.put('/api/weights')
    def save_weights(body:dict):
        store.revision(body);weights=body.get('weights')
        if not isinstance(weights,dict):raise ValueError('浏览器比例需要对象')
        for key,value in weights.items():
            store.check_kind('desktop',key)
            if isinstance(value,bool) or not isinstance(value,int) or not 0<=value<=1000:raise ValueError('权重应为 0～1000 的整数')
        if not any(weights.values()):raise ValueError('至少一个浏览器权重大于 0')
        return store.write('配置/随机比例.json',{'weights':weights},body['revision'])
    @app.post('/api/runs/{identity}/close')
    def close(identity:str):app.state.engine.close_run(identity);return {'message':'已请求关闭；清理完成后更新此行'}
    @app.put('/api/runs/{identity}/preserve')
    def preserve(identity:str,body:dict):return public_row(app.state.engine.preserve(identity,body['value']))
    @app.get('/api/runs/{identity}/screen')
    def screen(identity:str):
        from .engine import alive
        row=store.get_run(identity)
        if not alive(row['pid'],row['created']):raise ValueError('本窗口进程已退出或身份改变')
        from server.python.browser_s.browser_module_s.browser_window import capture_browser_window_by_handle
        data=capture_browser_window_by_handle(row['hwnd'],expected_process_id=row['pid'],desktop_region=True)
        return Response(data,media_type='image/png')

    @app.get('/api/logs')
    def logs():return store.logs()
    app.mount('/ui',StaticFiles(directory=Path(__file__).resolve().parents[1]/'ui'),name='ui')
    return app
