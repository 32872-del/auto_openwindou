"""显式制作公司内部配置快照；不输出密码，不复制运行身份、账本或浏览器资料。"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from launcher.company_preset import ALLOWED, FORMAT, SHARED, load_preset


def prepare(destination):
    files = {}
    for relative in sorted(ALLOWED):
        source = ROOT / relative
        if source.is_file():
            files[relative] = json.loads(source.read_text(encoding='utf-8-sig'))
    if SHARED not in files:
        raise ValueError('本机缺少共用配置，不能制作公司内部版')
    result = {'format': FORMAT, 'version': 1, 'files': files}
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    load_preset(destination)
    shared = files[SHARED]
    print(json.dumps({'configuration_files': len(files), 'providers': len(shared['providers']),
                      'proxy_sources': len(shared['proxies']['sources']), 'plans': len(shared['plans'])}, ensure_ascii=False))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('destination')
    prepare(parser.parse_args().destination)
