# -*- coding: utf-8 -*-
"""入口:python main.py 跑主流程,python main.py --serve 启动 webhook 服务。"""
import sys

from app.timingTasks.tools.wenlv_case import run


if __name__ == "__main__":
    if "--serve" in sys.argv[1:]:
        from webhook_server import serve
        serve()
    else:
        run()
