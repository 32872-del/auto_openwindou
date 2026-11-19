"""TLS 检测值持久化和单个 Excel 报表的原子更新。"""
from datetime import datetime
import json
import os
import threading
import uuid
from pathlib import Path
from .settings import local_path


COLUMNS = (
    ('email', '邮箱号'), ('captured_at', '检测时间'),
    ('ja3_hash', 'JA3 哈希'), ('ja3', 'JA3 原始串'), ('p0f', 'p0f'), ('ja4', 'JA4'),
    ('akamai_hash', 'Akamai 哈希'), ('akamai', 'Akamai 原始串'),
    ('peetprint_hash', 'PeetPrint 哈希'), ('peetprint', 'PeetPrint 原始串'),
    ('used_tls', 'Used TLS'), ('used_http', 'Used HTTP'),
    ('http2_frames', 'Total http/2 frames'), ('supported_http', 'Supported HTTP'),
    ('supported_tls', 'Supported TLS'), ('ip_ttl', 'IP TTL'),
    ('ciphers', 'TLS cipher suites'), ('extensions', 'TLS extensions'), ('curves', 'TLS curves'),
)
_export_lock = threading.RLock()


class TlsReport:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS tls_results(
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL, email TEXT NOT NULL,
                captured_at TEXT NOT NULL, data TEXT NOT NULL)''')
            columns = {row[1] for row in db.execute('PRAGMA table_info(tls_results)')}
            if 'archive_event' not in columns:
                db.execute("ALTER TABLE tls_results ADD COLUMN archive_event TEXT NOT NULL DEFAULT ''")

    @property
    def path(self):
        return local_path(self.store.root, self.store.system()['archive_dir']) / 'TLS检测结果.xlsx'

    def captures(self, run_id=None):
        with self.store.connect() as db:
            query = 'SELECT id,run_id,email,captured_at,data,archive_event FROM tls_results'
            params = ()
            if run_id is not None:
                query += ' WHERE run_id=?'
                params = (run_id,)
            rows = db.execute(query + ' ORDER BY captured_at,rowid', params).fetchall()
        return [dict(zip(('id','run_id','email','captured_at','data','archive_event'), row)) for row in rows]

    def attach_archive(self, capture_id, event):
        with self.store.connect() as db:
            db.execute("UPDATE tls_results SET archive_event=? WHERE id=? AND archive_event=''",
                       (json.dumps(event, ensure_ascii=False), capture_id))

    def record(self, capture_id, run_id, email, captured_at, values, *, archive_event=None):
        # 每次采集的 ID 固定，补写 Excel 直接使用这条记录，不再访问网站。
        with _export_lock, self.store.connect() as db:
            db.execute('INSERT OR IGNORE INTO tls_results(id,run_id,email,captured_at,data,archive_event) VALUES(?,?,?,?,?,?)',
                       (capture_id, run_id, email, captured_at, json.dumps(values, ensure_ascii=False),
                        json.dumps(archive_event, ensure_ascii=False) if archive_event else ''))

    def export(self, *, capture_id=None, path=None):
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter

        with _export_lock:
            with self.store.connect() as db:
                query = 'SELECT id,run_id,email,captured_at,data FROM tls_results'
                params = ()
                if capture_id is not None:
                    query += ' WHERE id=?'
                    params = (capture_id,)
                records = db.execute(query + ' ORDER BY captured_at,rowid', params).fetchall()
            if not records:
                raise ValueError('还没有 TLS 检测数据，请在创建窗口时勾选 TLS 检测')
            if len(records) >= 1048576:
                raise ValueError('TLS 检测记录超过单张 Excel 工作表容量')
            book = Workbook()
            sheet = book.active
            sheet.title = 'TLS检测'
            sheet.append([label for _, label in COLUMNS])
            sheet.freeze_panes = 'C2'
            sheet.sheet_view.showGridLines = False
            for cell in sheet[1]:
                cell.font = Font(name='Microsoft YaHei', bold=True, color='FFFFFF', size=11)
                cell.fill = PatternFill('solid', fgColor='1E5146')
                cell.alignment = Alignment(vertical='center', wrap_text=True)
            sheet.row_dimensions[1].height = 34
            widths = [30, 23, 35, 65, 58, 44, 35, 60, 35, 75, 15, 16, 23, 24, 27, 12, 62, 65, 38]
            for index, width in enumerate(widths, 1):
                sheet.column_dimensions[get_column_letter(index)].width = width
            for row_number, (_, _, email, captured_at, data) in enumerate(records, 2):
                values = {**json.loads(data), 'email': email, 'captured_at': captured_at}
                for column_number, (key, _) in enumerate(COLUMNS, 1):
                    value = values.get(key, '')
                    cell = sheet.cell(row_number, column_number)
                    if key == 'captured_at':
                        cell.value = datetime.fromisoformat(value).replace(tzinfo=None)
                        cell.number_format = 'yyyy-mm-dd hh:mm:ss'
                    elif key in ('http2_frames', 'ip_ttl') and str(value).isdigit():
                        cell.value = int(value)
                        cell.number_format = '0'
                    else:
                        cell.value = str(value)
                        # 原始串、邮箱和纯数字哈希必须作为文本写入，保留前导零。
                        cell.data_type = 's'
                        cell.number_format = '@'
                    cell.font = Font(name='Microsoft YaHei', size=10)
                    cell.alignment = Alignment(vertical='top', wrap_text=True)
                    if row_number % 2 == 0:
                        cell.fill = PatternFill('solid', fgColor='F0F6F3')
                line_count = max(str(values.get(key, '')).count('\n') + 1 for key in ('ciphers', 'extensions', 'curves'))
                sheet.row_dimensions[row_number].height = min(409, max(100, line_count * 15 + 12))
            sheet.auto_filter.ref = sheet.dimensions
            target = Path(path) if path is not None else self.path
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.stem + '.' + uuid.uuid4().hex + '.tmp')
            try:
                book.save(temporary)
                os.replace(temporary, target)
            except PermissionError as exc:
                raise ValueError(f'TLS 检测数据已保留。请关闭已打开的 {target.name}，再从检测记录补写') from exc
            finally:
                book.close()
                temporary.unlink(missing_ok=True)
            return target, [(capture_id, run_id) for capture_id, run_id, *_ in records]
