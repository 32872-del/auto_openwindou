"""
reCAPTCHA v2 自动破解 - CDP + CapSolver
- 通过 Chrome DevTools Protocol（HTTP /json + WebSocket Runtime.evaluate）
  在浏览器里执行两段 JS：取 sitekey；注入 g-recaptcha-response token 并触发 callback
- 不依赖 DrissionPage（避免 Qt 后台线程接管浏览器导致进程崩溃）
- 打码走 CapSolver / yescaptcha / ezcaptcha 兼容的 createTask + getTaskResult 协议
"""

import json
import time
import logging
import requests

from vendor_runtime.legacy_core import local_http
from typing import Callable, Optional

try:
    from websocket import create_connection  # websocket-client
except ImportError:
    create_connection = None  # 延迟到调用时报错，让 import 链路不中断

log = logging.getLogger("CaptchaSolver")

# 本地 CDP 调用必须绕过系统代理：启动时注入的 HTTP_PROXY=7897 会把
# 127.0.0.1:<port>/json 的请求甩进外部代理 → 又慢又易失败(过人机"失败"高发根因)。
# 与 octo_api / undetectable_api / proxy_manager 的本地调用保持一致。
_LOCAL_NO_PROXY = {"http": None, "https": None}


class CaptchaException(Exception):
    pass


# ───── JS 两段（搬自 auto_drssionPage_ai/python/utils/ReCaptchaUtils.py，逻辑零改）─────

JS_GET_SITEKEY = r"""
(function(){
    // 方案 B: 认"当前可见 + 未完成(textarea 为空)"的 widget。
    // —— SPA / 多步表单同站点新旧 widget 的 sitekey 相同，无法靠 sitekey 区分；
    //    但旧 widget 已解过(textarea 有 token)，当前待解的那个 textarea 是空的。
    function isVisible(el){
        if (!el) return false;
        var r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    }
    function skFromIframe(f){
        var m = ((f && f.src) || '').match(/[?&]k=([\w-]+)/);
        return m ? m[1] : '';
    }
    // 从 anchor iframe 往上找它所在 widget 子树里的 g-recaptcha-response textarea
    function textareaFor(f){
        var node = f;
        for (var up = 0; up < 6 && node; up++){
            node = node.parentElement;
            if (!node) break;
            var ta = node.querySelector('textarea.g-recaptcha-response, textarea[name="g-recaptcha-response"]');
            if (ta) return ta;
        }
        return null;
    }
    // anchor iframe = "我不是机器人"勾选框（src 含 anchor），排除 bframe 挑战弹窗
    var anchors = [];
    try {
        anchors = Array.prototype.slice.call(
            document.querySelectorAll('iframe[src*="recaptcha"][src*="anchor"], iframe[title="reCAPTCHA"]')
        ).filter(function(v, i, a){ return a.indexOf(v) === i; });
    } catch(e) {}
    var items = anchors.map(function(f, idx){
        var ta = textareaFor(f);
        return {
            idx: idx,
            sitekey: skFromIframe(f),
            visible: isVisible(f),
            solved: !!(ta && ta.value && ta.value.length > 0)
        };
    });
    var pick = null, reason = '';
    // 倒序优先: 可见 且 未解
    for (var i = items.length - 1; i >= 0; i--){
        if (items[i].visible && !items[i].solved && items[i].sitekey){ pick = items[i]; reason = 'visible+empty'; break; }
    }
    // 退一步: 可见的最新
    if (!pick){
        for (var i2 = items.length - 1; i2 >= 0; i2--){
            if (items[i2].visible && items[i2].sitekey){ pick = items[i2]; reason = 'visible'; break; }
        }
    }
    // 兜底: 可见的 [data-sitekey] 元素
    if (!pick){
        try {
            var ws = document.querySelectorAll('[data-sitekey]');
            for (var j = ws.length - 1; j >= 0; j--){
                var r = ws[j].getBoundingClientRect();
                var sk = ws[j].getAttribute('data-sitekey');
                if (r.width > 0 && r.height > 0 && sk){ pick = {idx:-1, sitekey:sk, visible:true, solved:false}; reason='data-sitekey'; break; }
            }
        } catch(e) {}
    }
    // 兜底: ___grecaptcha_cfg 最后一个 client
    var clientCount = 0;
    try { if (typeof ___grecaptcha_cfg !== 'undefined') clientCount = Object.keys(___grecaptcha_cfg.clients || {}).length; } catch(e) {}
    if (!pick && clientCount){
        try {
            var entries = Object.entries(___grecaptcha_cfg.clients);
            for (var k = entries.length - 1; k >= 0 && !pick; k--){
                var base = entries[k][1];
                for (var k0 of Object.keys(base)){
                    if (base[k0] == null) continue;
                    for (var k1 of Object.keys(base[k0])){
                        var v = base[k0][k1];
                        if (v && v.sitekey && typeof v.sitekey === 'string'){ pick = {idx:-1, sitekey:v.sitekey, visible:true, solved:false}; reason='cfg-last'; break; }
                    }
                    if (pick) break;
                }
            }
        } catch(e) {}
    }
    return JSON.stringify({
        sitekey: pick ? pick.sitekey : '',
        reason: reason,
        anchors: items.length,
        visible: items.filter(function(x){return x.visible;}).length,
        unsolvedVisible: items.filter(function(x){return x.visible && !x.solved;}).length,
        clients: clientCount,
        pickedIdx: pick ? pick.idx : -1
    });
})()
"""

JS_GET_PAGEURL = "location.href"


def _build_js_inject(token: str, sitekey: str = "") -> str:
    """方案 B: 定位"当前可见+未解"的 widget，填它的 textarea，
    并按 DOM 关系精确触发它对应 client 的 callback（旧的已解 widget 自动排除）。
    返回 JSON 字符串: {filled, firedVia, clients}
    """
    t = json.dumps(token)
    sk = json.dumps(sitekey)
    return r"""
(function (response, sitekey) {
    function isVisible(el){ if(!el) return false; var r=el.getBoundingClientRect(); return r.width>0 && r.height>0; }
    function textareaFor(f){
        var node=f;
        for(var up=0; up<6 && node; up++){ node=node.parentElement; if(!node) break;
            var ta=node.querySelector('textarea.g-recaptcha-response, textarea[name="g-recaptcha-response"]');
            if(ta) return ta;
        }
        return null;
    }
    function fire(v){
        if(!v || !v.callback) return false;
        if(typeof v.callback==='string'){ try{ window[v.callback](response); return true; }catch(e){} }
        else if(typeof v.callback==='function'){ try{ v.callback(response); return true; }catch(e){} }
        return false;
    }
    // 1. 定位目标 widget: 可见 且 未解(textarea 空)，倒序取最新
    var anchors=[];
    try{ anchors=Array.prototype.slice.call(
        document.querySelectorAll('iframe[src*="recaptcha"][src*="anchor"], iframe[title="reCAPTCHA"]')
    ).filter(function(v,i,a){return a.indexOf(v)===i;}); }catch(e){}
    var targetIframe=null, targetTa=null;
    for(var i=anchors.length-1;i>=0;i--){
        var ta=textareaFor(anchors[i]);
        var solved=!!(ta && ta.value && ta.value.length>0);
        if(isVisible(anchors[i]) && !solved){ targetIframe=anchors[i]; targetTa=ta; break; }
    }
    if(!targetIframe){
        for(var i2=anchors.length-1;i2>=0;i2--){ if(isVisible(anchors[i2])){ targetIframe=anchors[i2]; targetTa=textareaFor(anchors[i2]); break; } }
    }
    // 2. 填 textarea: 优先目标的；其余只填仍为空的(不覆盖旧 widget 已有 token)
    var filled=false;
    if(targetTa){ targetTa.innerHTML=response; targetTa.value=response; filled=true; }
    try{
        var all=document.querySelectorAll('textarea[name="g-recaptcha-response"], #g-recaptcha-response');
        for(var a0=0;a0<all.length;a0++){ if(!all[a0].value){ all[a0].innerHTML=response; all[a0].value=response; filled=true; } }
    }catch(e){}
    try{
        var hs=document.getElementsByName('h-captcha-response');
        for(var h=0;h<hs.length;h++){ if(!hs[h].value){ hs[h].innerHTML=response; hs[h].value=response; filled=true; } }
    }catch(e){}
    // 3. 触发对应 client 的 callback
    var firedVia='';
    var clientCount=0;
    try{
        if(typeof ___grecaptcha_cfg!=='undefined'){
            var cents=Object.entries(___grecaptcha_cfg.clients);
            clientCount=cents.length;
            // 3a. DOM 精确匹配: client 里某属性是【包含目标 iframe/textarea】的 Element
            if(targetIframe){
                for(var c=0;c<cents.length && !firedVia;c++){
                    var base=cents[c][1];
                    var domHit=false;
                    for(var dk of Object.keys(base)){
                        var dv=base[dk];
                        if(dv && typeof dv==='object' && dv.nodeType===1){
                            try{ if(dv===targetIframe || dv.contains(targetIframe) || (targetTa && dv.contains(targetTa))) domHit=true; }catch(e){}
                        }
                    }
                    if(domHit){
                        for(var k0 of Object.keys(base)){
                            if(base[k0]==null || typeof base[k0]!=='object') continue;
                            for(var k1 of Object.keys(base[k0])){ if(fire(base[k0][k1])){ firedVia='dom-match'; break; } }
                            if(firedVia) break;
                            if(fire(base[k0])){ firedVia='dom-match'; break; }
                        }
                    }
                }
            }
            // 3b. sitekey 倒序匹配(最新优先)
            if(!firedVia && sitekey){
                for(var s=cents.length-1;s>=0 && !firedVia;s--){
                    var base2=cents[s][1];
                    for(var sk0 of Object.keys(base2)){
                        if(base2[sk0]==null || typeof base2[sk0]!=='object') continue;
                        for(var sk1 of Object.keys(base2[sk0])){
                            var sv=base2[sk0][sk1];
                            if(sv && sv.sitekey===sitekey && fire(sv)){ firedVia='sitekey-reverse'; break; }
                        }
                        if(firedVia) break;
                    }
                }
            }
            // 3c. 兜底: 倒序任意 callback
            if(!firedVia){
                for(var f0=cents.length-1;f0>=0 && !firedVia;f0--){
                    var base3=cents[f0][1];
                    for(var fk0 of Object.keys(base3)){
                        if(base3[fk0]==null || typeof base3[fk0]!=='object') continue;
                        for(var fk1 of Object.keys(base3[fk0])){ if(fire(base3[fk0][fk1])){ firedVia='fallback'; break; } }
                        if(firedVia) break;
                        if(fire(base3[fk0])){ firedVia='fallback'; break; }
                    }
                }
            }
        }
    }catch(e){}
    return JSON.stringify({filled:filled, firedVia:firedVia, clients:clientCount});
})(""" + t + r""", """ + sk + r""")
"""


# ───── CDP 客户端（最小实现）─────

def _cdp_list_tabs(debug_port: int, timeout: int = 5) -> list:
    """GET http://127.0.0.1:<port>/json → 所有 tab 列表"""
    url = f"http://127.0.0.1:{debug_port}/json"
    try:
        resp = local_http.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        raise CaptchaException(f"CDP 列 tab 失败 (port={debug_port}): {e}")


def _cdp_eval(ws_url: str, js: str, timeout: float = 15.0) -> dict:
    """连 WebSocket 发 Runtime.evaluate，返回 {value, error}"""
    if create_connection is None:
        raise CaptchaException("缺少 websocket-client，请 pip install websocket-client")
    try:
        # suppress_origin=True: 不发送 Origin header
        # —— 绕开 Chrome 124+ 的 remote-allow-origins allowlist 检查
        # （DrissionPage 同样用这个参数，见其 _base/driver.py:159）
        ws = create_connection(ws_url, timeout=timeout, suppress_origin=True)
    except Exception as e:
        raise CaptchaException(f"CDP WebSocket 连接失败: {e}")
    try:
        msg = {
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {
                "expression": js,
                "returnByValue": True,
                "awaitPromise": False,
            },
        }
        ws.send(json.dumps(msg))
        ws.settimeout(timeout)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                raw = ws.recv()
            except Exception as e:
                # 浏览器/标签页中途关闭、连接被对端断开、recv 超时等 →
                # 返回明确错误，避免逃逸到上层变成"未知异常"难以排查
                return {"value": None,
                        "error": f"CDP 接收失败(浏览器/标签页可能已关闭): {e}"}
            try:
                data = json.loads(raw)
            except Exception:
                continue
            if data.get("id") != 1:
                continue  # 跳过服务端推送的其它事件
            if "error" in data:
                return {"value": None, "error": data["error"]}
            result = data.get("result", {}).get("result", {})
            if data.get('result',{}).get('exceptionDetails'):
                return {'value':None,'error':data['result']['exceptionDetails']}
            return {"value": result.get("value"), "error": None}
        return {"value": None, "error": "CDP 响应超时"}
    finally:
        try:
            ws.close()
        except Exception:
            pass


def _pick_recaptcha_tab(debug_port: int) -> Optional[dict]:
    """从所有 tab 里挑当前页：优先含 ___grecaptcha_cfg 的，否则返回第一个 page"""
    tabs = _cdp_list_tabs(debug_port)
    pages = [
        t for t in tabs
        if t.get("type") == "page"
        and not (t.get("url", "").startswith("chrome://"))
        and not (t.get("url", "").startswith("devtools://"))
        and t.get("webSocketDebuggerUrl")
    ]
    if not pages:
        return None
    for tab in pages:
        try:
            r = _cdp_eval(tab["webSocketDebuggerUrl"],
                          "typeof ___grecaptcha_cfg !== 'undefined'")
            if r.get("value") is True:
                return tab
        except Exception:
            continue
    return pages[0]


# ───── CapSolver API（与 yescaptcha / ezcaptcha 同协议）─────

def _capsolver_create_task(api_url: str, client_key: str,
                            website_url: str, website_key: str,
                            task_type: str = "ReCaptchaV2TaskProxyless") -> str:
    body = {
        "clientKey": client_key,
        "task": {
            "type": task_type,
            "websiteURL": website_url,
            "websiteKey": website_key,
        },
    }
    url = f"{api_url.rstrip('/')}/createTask"
    try:
        resp = requests.post(url, json=body, timeout=(5, 20))
        data = resp.json()
    except Exception as e:
        raise CaptchaException(f"createTask 网络错: {e}")
    if data.get("errorId") == 0 and data.get("taskId"):
        return data["taskId"]
    raise CaptchaException(
        f"createTask 失败: {data.get('errorCode')} {data.get('errorDescription')}"
    )


def _capsolver_get_result(api_url: str, client_key: str, task_id: str,
                           wait_count: int = 50, pause: float = 2.0,
                           progress_cb: Optional[Callable[[str], None]] = None) -> str:
    url = f"{api_url.rstrip('/')}/getTaskResult"
    body = {"clientKey": client_key, "taskId": task_id}
    for i in range(wait_count):
        try:
            resp = requests.post(url, json=body, timeout=(5, 20))
            data = resp.json()
            if data.get("errorId") == 0:
                status = (data.get("status") or "").lower()
                if status == "ready":
                    sol = data.get("solution") or {}
                    token = sol.get("gRecaptchaResponse")
                    if token:
                        return token
                    raise CaptchaException("solution 缺 gRecaptchaResponse")
                if progress_cb:
                    progress_cb(f"等待({wait_count - i})")
            else:
                err = (data.get("errorCode") or "")
                if "SOLVE_FAILED" in err or "INVALID" in err:
                    raise CaptchaException(
                        f"打码失败: {err} {data.get('errorDescription')}"
                    )
                if progress_cb:
                    progress_cb(f"API错{err}")
        except CaptchaException:
            raise
        except Exception as e:
            log.warning(f"getTaskResult 网络错: {e}")
        time.sleep(pause)
    raise CaptchaException(f"轮询 {wait_count} 次仍未拿到 token")


# ───── 主入口 ─────

def solve_recaptcha_v2(debug_port: int,
                       api_url: str,
                       client_key: str,
                       wait_count: int = 50,
                       task_type: str = "ReCaptchaV2TaskProxyless",
                       progress_cb: Optional[Callable[[str], None]] = None) -> dict:
    """端到端解 reCAPTCHA v2，progress_cb 接收阶段文案用于 UI 反馈
    返回 {success, sitekey, pageurl, token, error}
    """
    result = {"success": False, "sitekey": "", "pageurl": "", "token": "", "error": ""}

    def _cb(s):
        if progress_cb:
            progress_cb(s)

    try:
        if not debug_port:
            raise CaptchaException("浏览器未提供 debug_port")
        if not client_key:
            raise CaptchaException("CapSolver client_key 未配置")

        _cb("查找Tab")
        tab = _pick_recaptcha_tab(debug_port)
        if not tab:
            raise CaptchaException("浏览器无可用页面")
        ws_url = tab["webSocketDebuggerUrl"]

        _cb("取sitekey")
        r_key = _cdp_eval(ws_url, JS_GET_SITEKEY)
        raw_key = r_key.get("value")
        info = {}
        if isinstance(raw_key, str):
            try:
                info = json.loads(raw_key)
            except Exception:
                info = {"sitekey": raw_key}
        elif isinstance(raw_key, dict):
            info = raw_key
        sitekey = (info.get("sitekey") or "") if info else ""
        _cb("诊断:widget %s个/可见%s/未解%s, client %s个, 选中#%s(%s)" % (
            info.get("anchors", "?"), info.get("visible", "?"),
            info.get("unsolvedVisible", "?"), info.get("clients", "?"),
            info.get("pickedIdx", "?"), info.get("reason", "")))
        if not sitekey:
            raise CaptchaException("当前页未发现 reCAPTCHA")
        result["sitekey"] = sitekey

        r_url = _cdp_eval(ws_url, JS_GET_PAGEURL)
        pageurl = r_url.get("value") or tab.get("url") or ""
        result["pageurl"] = pageurl
        if not pageurl:
            raise CaptchaException("无法读取页面 URL")

        _cb("提交打码")
        task_id = _capsolver_create_task(api_url, client_key, pageurl, sitekey, task_type)
        log.info(f"CapSolver task_id={task_id} sitekey={sitekey[:12]}... url={pageurl[:60]}")

        token = _capsolver_get_result(api_url, client_key, task_id,
                                       wait_count=wait_count,
                                       progress_cb=lambda s: _cb(s))
        result["token"] = token

        _cb("注入token")
        r_inj = _cdp_eval(ws_url, _build_js_inject(token, sitekey), timeout=10)
        raw_inj = r_inj.get("value")
        inj_info = {}
        if isinstance(raw_inj, str):
            try:
                inj_info = json.loads(raw_inj)
            except Exception:
                inj_info = {}
        elif isinstance(raw_inj, dict):
            inj_info = raw_inj
        _cb("诊断:注入 filled=%s 触发方式=%s client=%s" % (
            inj_info.get("filled", "?"), inj_info.get("firedVia") or "无",
            inj_info.get("clients", "?")))
        if r_inj.get("error"):
            raise CaptchaException(f"页面接收结果失败: {r_inj['error']}")
        if not inj_info.get('filled') and not inj_info.get('firedVia'):
            raise CaptchaException('服务已返回，但页面没有确认接收结果，请检查当前页面')

        result["success"] = True
        _cb("结果已传给页面，请查看网页最终状态")
        return result

    except CaptchaException as e:
        result["error"] = str(e)
        log.warning(f"reCAPTCHA 解决失败: {e}")
        return result
    except Exception as e:
        result["error"] = f"未知异常: {e}"
        log.error(f"reCAPTCHA 解决异常: {e}", exc_info=True)
        return result
