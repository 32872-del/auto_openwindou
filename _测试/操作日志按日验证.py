"""离线验证最近 500 条、旧日志迁移与十五日清理。"""
import json
import sqlite3
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from launcher.storage import Store


def main():
    with tempfile.TemporaryDirectory(prefix='操作日志-',dir=ROOT/'_测试') as folder:
        store=Store(folder)
        today=date.today()
        old=today-timedelta(days=20)
        recent=today-timedelta(days=2)
        logdir=store.paths.logs
        (logdir/('运行-'+old.isoformat()+'.jsonl')).write_text('旧日志\n',encoding='utf-8')
        legacy=[{'time':old.isoformat()+' 12:00:00','message':'旧版过期'},
                {'time':recent.isoformat()+' 12:00:00','message':'旧版保留'}]
        (logdir/'运行.jsonl').write_text(''.join(json.dumps(item,ensure_ascii=False)+'\n' for item in legacy),encoding='utf-8')
        with store.connect() as db:
            db.execute('INSERT INTO logs(at,data) VALUES(?,?)',(time.mktime(old.timetuple()),json.dumps(legacy[0],ensure_ascii=False)))
            for index in range(520):
                item={'time':today.isoformat()+' 12:00:00','message':f'第{index}条'}
                db.execute('INSERT INTO logs(at,data) VALUES(?,?)',(time.time(),json.dumps(item,ensure_ascii=False)))
        store=Store(folder)
        rows=store.logs()
        assert len(rows)==500 and rows[0]['message']=='第519条' and rows[-1]['message']=='第20条'
        assert not (logdir/'运行.jsonl').exists()
        assert not (logdir/('运行-'+old.isoformat()+'.jsonl')).exists()
        assert '旧版保留' in (logdir/('运行-'+recent.isoformat()+'.jsonl')).read_text(encoding='utf-8')
        assert not any(row['message']=='旧版过期' for row in rows)
        store.log('今日新增',operation='验证')
        assert '今日新增' in (logdir/('运行-'+today.isoformat()+'.jsonl')).read_text(encoding='utf-8')
        print('最近500条、每日文件、旧文件迁移与15日清理：通过')


if __name__=='__main__':main()
