"""EXE 入口；保留原程序入口和后台检测子进程协议。"""
import multiprocessing
import runpy
import sys

if __name__ == '__main__':
    multiprocessing.freeze_support()
    if '--package-self-check' in sys.argv:
        from package_check import run
        raise SystemExit(run(sys.argv[sys.argv.index('--package-self-check') + 1]))
    runpy.run_module('main', run_name='__main__')
