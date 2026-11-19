"""控件树不包含系统栏时，识别截图中的三键导航；不使用固定屏幕坐标。"""
import cv2
import numpy as np
from .contracts import CloudPhoneError

def navigation_point(png,operation):
    image=cv2.imdecode(np.frombuffer(png,dtype=np.uint8),cv2.IMREAD_COLOR)
    if image is None:raise CloudPhoneError('导航识别未取得有效手机画面')
    h,w=image.shape[:2];offset=int(h*.94)
    gray=cv2.cvtColor(image[offset:,:],cv2.COLOR_BGR2GRAY)
    shapes=[]
    # 深色导航栏白色图标、浅色导航栏深色图标都支持；仍须完整三键几何一致。
    for low,high in ((220,255),(0,160)):
        contours,_=cv2.findContours(cv2.inRange(gray,low,high),cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
        for c in contours:
            area=cv2.contourArea(c);x,y,bw,bh=cv2.boundingRect(c)
            if not (w*.012<=bw<=w*.08 and w*.012<=bh<=w*.08 and area>w*w*.00015):continue
            perimeter=cv2.arcLength(c,True)
            approx=cv2.approxPolyDP(c,.04*perimeter,True)
            candidate={'x':x+bw/2,'y':offset+y+bh/2,'vertices':len(approx),'round':4*np.pi*area/(perimeter*perimeter)}
            if not any(abs(s['x']-candidate['x'])<4 and abs(s['y']-candidate['y'])<4 for s in shapes):shapes.append(candidate)
    homes=[s for s in shapes if .42*w<s['x']<.58*w and s['round']>.78 and s['vertices']>=6]
    backs=[s for s in shapes if .15*w<s['x']<.46*w and s['vertices'] in (3,4) and s['round']<.72]
    recent=[s for s in shapes if .54*w<s['x']<.85*w and s['vertices']==4]
    if not (len(homes)==len(backs)==len(recent)==1) or max(s['y'] for s in homes+backs+recent)-min(s['y'] for s in homes+backs+recent)>w*.03:
        raise CloudPhoneError('无法确认手机三键导航位置，请检查系统导航栏；未盲点画面')
    selected=homes[0] if operation=='home' else backs[0]
    return round(selected['x']),round(selected['y'])
