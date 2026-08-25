# -*- coding: utf-8 -*-
"""兼容入口:启动 Flask webhook 服务(13001 + 80,与旧版行为一致)。"""
import threading
import time

from werkzeug.serving import make_server

import config
from app import create_app
from app.utils.tools import setup_logger

log = setup_logger()


def serve():
    app = create_app()
    ports = sorted({int(config.WEBHOOK_PORT), 80})
    servers = []
    for p in ports:
        try:
            srv = make_server(config.WEBHOOK_HOST, p, app, threaded=True)
            servers.append((p, srv))
        except BaseException as e:
            log.warning("[webhook] 端口 {} 绑定失败(可能被占用或无权限): {}".format(p, e))
    if not servers:
        raise RuntimeError("无可用端口可监听,服务启动失败")
    for p, srv in servers:
        t = threading.Thread(target=srv.serve_forever, daemon=True, name="webhook-{}".format(p))
        t.start()
    ports_str = ", ".join(str(p) for p, _ in servers)
    log.info("Webhook 服务已启动: http://{} (监听端口: {})".format(config.WEBHOOK_HOST, ports_str))
    log.info("POST /timingTasks/wenlv_db (接收多维表数据 -> 写 MySQL)")
    log.info("POST /timingTasks/wenlv_feishu (触发主流程 -> 写飞书 Bitable)")
    log.info("GET  /health")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        log.info("收到 Ctrl+C,正在停止...")
        for _, srv in servers:
            srv.shutdown()


if __name__ == "__main__":
    serve()
