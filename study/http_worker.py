"""照片专用单次请求进程；复用官方端点、无代理/重定向/重试策略。"""
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from legacy import http_worker

http_worker.MAX_REQUEST_BYTES=12*1024*1024
if __name__=='__main__':
    http_worker.main()
