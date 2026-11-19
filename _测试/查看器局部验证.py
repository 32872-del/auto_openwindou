from pathlib import Path
import json,sys,time
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from launcher.providers import initialize
initialize(ROOT)
from server.python.cloud_phone_s.isolated_viewer import IsolatedViewer
target=ROOT/'_测试/查看器验证';target.mkdir(exist_ok=True)
page=target/'点击.html'
page.write_text('<html><body><button style="position:absolute;left:120px;top:120px;width:200px;height:80px" onclick="this.textContent=\'clicked\'">test</button></body></html>',encoding='utf-8')
viewer=IsolatedViewer(target/'profile',page.as_uri(),device_identity='local-viewer-test')
result={}
try:
    viewer.start()
    result['description']=viewer.call('describe')
    viewer.call('tap',160,150)
    result['text']=viewer.call('query','return document.querySelector("button").textContent')
    assert result['text']=='clicked',result
    result['pass']=True
finally:
    result['closed']=viewer.close()
    (target/'结果.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(result,ensure_ascii=True))
