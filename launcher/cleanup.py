"""只确认本次拥有资源的收尾；不以接口返回代替删除或关机结果。"""
import time
import re
import psutil


def process_state(pid, created):
    if not pid or not created:
        return 'exited'
    try:
        process = psutil.Process(int(pid))
        return 'running' if abs(process.create_time() - float(created)) < .01 and process.is_running() else 'exited'
    except psutil.NoSuchProcess:
        return 'exited'
    except psutil.AccessDenied:
        return 'unknown'


def profile_exists(browser, identity):
    """精确详情优先；仅明确的不支持才回退到有界分页。"""
    try:
        browser.get_browser(identity)
        return True
    except Exception as exc:
        code = getattr(exc, 'code', '')
        if code in ('not_found', 'profiles.not_found'):
            return False
        if not isinstance(exc, (NotImplementedError, AttributeError)) and code != 'unsupported_operation':
            raise
    for page in range(1, 21):
        records = browser.list_browsers(page=page, page_size=100)
        if any(str(record.browser_id) == str(identity) for record in records):
            return True
        if len(records) < 100:
            return False
    raise RuntimeError('环境列表超过2000条，未取得可确认的删除结果；请检查厂商列表后重试')


def close_browser(browser, row, update, *, timeout=8, octo_cloud_delete=False):
    identity = row.get('profile_id')
    if not identity:
        return
    # 兼容旧账本中的两种明确启动拒绝。其他 HTTP 400、超时及未绑定 PID 都需要核实。
    error = row.get('error') or ''
    legacy_auth = re.match(r'^(?:BrowserError: )?octo\.start \[(?:unauthorized|unauthenticated|login_required), HTTP 401\]',error)
    legacy_subscription = (re.match(r'^(?:BrowserError: )?octo\.start \[invalid_parameters, HTTP 400\]:',error)
                           and re.search(r'(?:厂商原文：|\]:\s*)No active subscription\.?(?=$|[；;])',error,re.I))
    rejected = row.get('_start_rejected') or (row.get('_start_failed') and (legacy_auth or legacy_subscription))
    if rejected and not row.get('pid'):
        update(_closed=True)
        if row.get('keep'):
            return
    if not profile_exists(browser, identity):
        if row.get('provider') == 'octo' and not row.get('_deleted'):
            raise ValueError('O 云端账号查不到原环境，无法确认是否已删除。请核对 API 账号，或强制移除本地记录')
        if process_state(row.get('pid'), row.get('created')) != 'exited':
            raise RuntimeError('厂商环境已不存在，但本次进程仍在运行或无法确认；未标记清理完成')
        update(_closed=True, _deleted=True)
        return
    if not octo_cloud_delete and row.get('_start_requested', True) and not row.get('_closed') and not (rejected and not row.get('pid')):
        # 监测到已绑定进程退出时，用户已经关窗；有些厂商会把重复 stop 报为失败。
        # 没绑定成功的启动失败记录仍需调用 stop，不能用 pid=0 推断已停止。
        confirmed_exit = bool(row.get('pid') and row.get('created')) and process_state(row['pid'], row['created']) == 'exited'
        if not confirmed_exit:
            try:
                browser.close_browser(identity)
            except Exception as exc:
                if getattr(exc, 'code', '') != 'already_stopped':
                    raise
        deadline = time.monotonic() + timeout
        while row.get('pid') and process_state(row['pid'], row.get('created')) != 'exited':
            if time.monotonic() >= deadline:
                raise RuntimeError('已请求关闭，但本次浏览器进程仍在运行或无权确认；保留记录供重试')
            time.sleep(.2)
        update(_closed=True)
    if row.get('keep'):
        return
    # U 的删除异步生效，后台等待确认，不阻塞界面或按接收即成功。
    deadline = time.monotonic() + (max(timeout,30) if getattr(browser,'key','')=='undetectable' else timeout)
    while True:
        try:
            browser.delete_browser(identity)
            break
        except Exception as exc:
            if row.get('provider')=='octo' and (getattr(exc,'status_code',None) in (401,403) or
                    getattr(exc,'code','') in ('missing_credentials','login_required','unauthorized','unauthenticated','forbidden')):
                raise
            # 接口超时或重复删除：只在精确确认不存在后视为成功。
            if not profile_exists(browser, identity):break
            # 管理器可能晚于本次进程退出更新状态，只重试明确“仍在关闭”。
            if getattr(exc, 'code', '') != 'browser_busy' or time.monotonic() >= deadline:raise
            time.sleep(.5)
    while profile_exists(browser, identity):
        if time.monotonic() >= deadline:
            raise RuntimeError('厂商已接收删除请求，但环境仍在列表中；未标记删除成功，请重试清理')
        time.sleep(.4)
    update(_deleted=True)


def recover_cloud(provider, row):
    """重建清理责任，不经过只接受空闲设备的新任务分配流程。"""
    from server.python.cloud_phone_s.framework.lifecycle import DeviceRun
    from server.python.cloud_phone_s.framework.device_pool import DeviceLease
    if not row.get('_cloud_owned') or not row.get('profile_id'):
        raise RuntimeError('缺少本次云手机所有权记录，未停止未知设备')
    lease = DeviceLease(provider.key, row['profile_id'], row['id'])
    if not lease.acquire():
        raise RuntimeError('该云手机正被另一软件占用，未抢占；原清理记录保留')
    try:
        device = provider.device(row['profile_id'])
        run = DeviceRun(provider, {'closeAfterRun': True})
        run.device, run.lease = device, lease
        run.started = bool(row.get('_cloud_started'))
        return run
    except BaseException:
        lease.release()
        raise


def close_owned_viewer(row, timeout=5):
    """手机控制页仅按保存的 PID/创建时间关闭，不通过浏览器调试口。"""
    state = process_state(row.get('pid'), row.get('created'))
    if state == 'exited':
        return
    if state == 'unknown' or not row.get('_viewer_owned'):
        raise RuntimeError('无法核实旧云手机控制窗口的进程归属；未关闭未知窗口')
    process = psutil.Process(int(row['pid']))
    try:
        children = process.children(recursive=True)
        process.terminate()
        for child in children:
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass
        _, remaining = psutil.wait_procs([process, *children], timeout=timeout)
        if remaining:
            raise RuntimeError('旧云手机控制窗口仍未退出；设备关机结果保留，窗口清理可重试')
    except psutil.NoSuchProcess:
        pass


def close_adapter(browser):
    """释放厂商HTTP连接；不调用 quit/stop 等浏览器控制函数。"""
    http = getattr(getattr(browser, 'api', None), 'http', None)
    session = getattr(http, '_session', None)
    if session is not None:
        session.close()
