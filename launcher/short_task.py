"""独立短任务：有总期限、可取消，任何出口都回收进程和通信句柄。"""
import json,os,subprocess,sys,time
from pathlib import Path

def run_worker(module, payload, *, timeout, cancel_event, on_progress=None):
    root=Path(__file__).resolve().parents[1]
    command=[sys.executable,'--v2-worker'] if getattr(sys,'frozen',False) else [str(Path(sys.executable).with_name('python.exe')) if os.name=='nt' else sys.executable,'-X','utf8','-m',module]
    process=subprocess.Popen(command,cwd=root,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    deadline=time.monotonic()+timeout;first=True;seen=0
    def progress(output):
        nonlocal seen
        if not output:return
        if len(output)>1_000_000:raise RuntimeError('短任务输出过大，已停止')
        lines=output.decode('utf-8',errors='replace').splitlines()
        # 最后一行只在完整换行后解析。
        if not output.endswith(b'\n'):lines=lines[:-1]
        for line in lines[seen:]:
            item=json.loads(line)
            if item.get('progress') and on_progress:on_progress(item['progress'])
        seen=len(lines)
    try:
        while True:
            if cancel_event.is_set():raise InterruptedError('已取消，短时连接已结束')
            left=deadline-time.monotonic()
            if left<=0:raise TimeoutError(f'操作超过 {timeout:g} 秒，短时连接已结束')
            try:
                out,err=process.communicate(json.dumps(payload).encode() if first else None,timeout=min(.25,left))
                progress(out);break
            except subprocess.TimeoutExpired as exc:
                first=False;progress(exc.output)
        if process.returncode:raise RuntimeError('短任务进程异常退出：'+err.decode('utf-8',errors='replace')[-1500:])
        messages=[json.loads(line) for line in out.decode('utf-8').splitlines() if line]
        result=next((m['result'] for m in reversed(messages) if 'result' in m),None)
        if result is None:raise RuntimeError('短任务未返回有效结果')
        return result
    finally:
        if process.poll() is None:process.kill()
        try:process.communicate(timeout=3)
        finally:
            for stream in (process.stdin,process.stdout,process.stderr):
                if stream:stream.close()

def verify_connection(row):
    """仅查OS进程和监听端口的归属，不向CDP发送查询。"""
    from .engine import alive
    from server.python.browser_s.browser_module_s.browser_window import browser_process_ids_by_port
    if not row.get('profile_id') or not alive(row.get('pid'),row.get('created')):
        raise ValueError('本窗口进程已退出或身份改变，已拒绝使用旧连接地址')
    if row['pid'] not in browser_process_ids_by_port(row.get('debug_port',0)):
        raise ValueError('缓存端口不再属于本窗口，已拒绝连接；请重新打开该环境')

