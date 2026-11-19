"""所有云手机查看器共用的坐标换算、截图和Win32输入。"""
import base64,json
from io import BytesIO
from ..isolated_viewer import ViewerError

class TouchWindow:
    def __init__(self,viewer,device,name,phone_size):
        self.viewer,self.device,self.name=viewer,device,name
        self.phone_size=tuple(phone_size)
    def close(self):
        if not self.viewer.close():raise ViewerError('云手机隔离窗口尚未退出，保留记录供清理重试')
    def describe(self):
        return {**self.viewer.call('describe'),'title':self.name+' · 隔离云手机',
            'phoneWidth':self.phone_size[0],'phoneHeight':self.phone_size[1],'canNavigate':True,
            'message':'云手机隔离窗口已打开，键鼠使用Win32'}
    def _verify_picture(self,screen):
        from PIL import Image,ImageChops,ImageStat
        a=Image.open(BytesIO(base64.b64decode(screen['image'].split(',',1)[1]))).convert('RGB').resize((54,96))
        b=Image.open(BytesIO(self.capture_bytes())).convert('RGB').resize((54,96))
        score=sum(ImageStat.Stat(ImageChops.difference(a,b)).mean)/3
        if score>25:raise ViewerError(f'隔离画面与目标设备不一致（差异{score:.1f}），未绑定输入')
    def capture_bytes(self):
        g=self._geometry()
        return self.viewer.call('captureRegion',g['x'],g['y'],g['w'],g['h'],*self.phone_size)
    def screen(self):
        return {'image':'data:image/png;base64,'+base64.b64encode(self.capture_bytes()).decode(),
                'width':self.phone_size[0],'height':self.phone_size[1],'source':'isolated-window'}
    def operate(self,operation,values,screen_size):
        if tuple(screen_size)!=self.phone_size:raise ViewerError('设备方向或尺寸已变化，请重新打开')
        g=self._geometry()
        if operation in ('tap','longPress','swipe'):
            keys=('x','y') if operation!='swipe' else ('x','y','endX','endY')
            coords=[values.get(k) for k in keys];points=[]
            if any(isinstance(v,bool) or not isinstance(v,int) for v in coords):raise ViewerError('手机坐标必须是整数')
            for x,y in zip(coords[::2],coords[1::2]):
                if not 0<=x<self.phone_size[0] or not 0<=y<self.phone_size[1]:raise ViewerError('手机坐标超出画面')
                points.extend((g['x']+x*g['w']/self.phone_size[0],g['y']+y*g['h']/self.phone_size[1]))
            if not self._clear_points(points):raise ViewerError('控制页弹层挡住手机画面，未发送操作')
            self.viewer.call(operation,*points)
        elif operation in ('home','back'):self._navigate(operation)
        elif operation=='text':self.viewer.call('text',values.get('text'))
        elif operation in ('enter','clearText'):self.viewer.call(operation)
        else:raise ViewerError('不支持的手机操作：'+str(operation))
        return {'message':'Win32操作已发送','inputMode':'win32','isolated':True}
