"""V2只在用户触发的有界进程中运行，不缓存浏览器连接。"""
import contextlib,json,sys

def main():
    output=sys.stdout
    try:
        payload=json.loads(sys.stdin.buffer.read(65537).decode('utf-8'))
        def report(message):
            output.write(json.dumps({'progress':str(message)},ensure_ascii=False)+'\n');output.flush()
        with contextlib.redirect_stdout(sys.stderr):
            from vendor_runtime.legacy_core.captcha_solver import solve_recaptcha_v2
            result=solve_recaptcha_v2(**payload,progress_cb=report)
        # 不把令牌或密钥写入UI和本地日志。
        result={k:v for k,v in result.items() if k in ('success','error')}
    except Exception as exc:result={'success':False,'error':f'{type(exc).__name__}: {exc}'}
    output.write(json.dumps({'result':result},ensure_ascii=False)+'\n');output.flush()
    return 0

if __name__=='__main__':raise SystemExit(main())
