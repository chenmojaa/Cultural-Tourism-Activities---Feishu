# -*- coding: utf-8 -*-
"""文旅案例 webhook 路由(Flask blueprint,路径前缀 /timingTasks)。"""
import threading
import time
import uuid
from datetime import datetime

import requests
from flask import Blueprint, jsonify, request

import config
from app.timingTasks import blueprint
from app.timingTasks.tools.wenlv_case import run_to_feishu, run_to_mysql
from app.utils import dedup
from app.utils import mysqlapi as mysql_client
from app.utils.tools import setup_logger

log = setup_logger()

WEBHOOK_PATH = config.WEBHOOK_PATH
WEBHOOK_PATH_FEISHU = "/timingTasks/wenlv_feishu"

_jobs = {}
_jobs_lock = threading.Lock()

health_blueprint = Blueprint("wenlv_health", __name__)


def _notify_feishu_workflow(rec_id: str, response_data: str, job_id: str, source: str):
    """主动 POST 飞书 webhook URL,触发"接收 Webhook 时"工作流更新记录。"""
    if not config.FEISHU_WORKFLOW_WEBHOOK_URL:
        log.warning("[webhook feishu] FEISHU_WORKFLOW_WEBHOOK_URL 未配置,跳过通知 source={}".format(source))
        return
    try:
        r = requests.post(
            config.FEISHU_WORKFLOW_WEBHOOK_URL,
            headers={
                "Authorization": "Bearer " + config.FEISHU_WORKFLOW_WEBHOOK_TOKEN,
                "Content-Type": "application/json",
            },
            json={"data_id": rec_id, "response_data": response_data, "job_id": job_id, "source": source},
            timeout=5,
        )
        log.info("[webhook feishu] notify source={} status={} body={}".format(source, r.status_code, r.text[:200]))
    except Exception as e:
        log.warning("[webhook feishu] notify {} 失败: {}".format(source, e))


def _set_job(job_id: str, status: str, **extra):
    with _jobs_lock:
        _jobs[job_id] = {"status": status, "ts": time.time(), **extra}


def _get_job(job_id: str) -> dict:
    with _jobs_lock:
        return dict(_jobs.get(job_id, {}))


def _run_feishu_job_async(job_id: str, rec_id: str, trigger_time_str: str):
    """后台线程跑 run_to_feishu,记录状态。"""
    started_at = time.time()
    try:
        _set_job(job_id, "running", started_at=started_at)
        result = run_to_feishu()
        elapsed = int(time.time() - started_at)
        mins, secs = divmod(elapsed, 60)
        data_done = "触发时间:{}, 更新完成!用时:{}分{}秒".format(trigger_time_str, mins, secs)
        _notify_feishu_workflow(rec_id, data_done, job_id, "wenglu_webhook_done")
        _set_job(job_id, "done", finished_at=time.time(), result=result, elapsed=elapsed)
        log.info("[webhook feishu] job {} done in {}m{}s: {}".format(job_id, mins, secs, result))
    except Exception as e:
        log.exception("[webhook feishu] job {} 失败".format(job_id))
        elapsed = int(time.time() - started_at)
        mins, secs = divmod(elapsed, 60)
        err_msg = str(e)[:50].replace(chr(10), " ")
        data_fail = "触发时间:{}, 更新失败!用时:{}分{}秒,原因:{}".format(trigger_time_str, mins, secs, err_msg)
        _notify_feishu_workflow(rec_id, data_fail, job_id, "wenglu_webhook_failed")
        _set_job(job_id, "failed", finished_at=time.time(), error=str(e), elapsed=elapsed)


def _run_mysql_job_async(job_id: str):
    """后台线程跑 run_to_mysql,记录状态。"""
    try:
        _set_job(job_id, "running", started_at=time.time())
        result = run_to_mysql()
        _set_job(job_id, "done", finished_at=time.time(), result=result)
        log.info("[webhook] job {} done: {}".format(job_id, result))
    except Exception as e:
        log.exception("[webhook] job {} 失败".format(job_id))
        _set_job(job_id, "failed", finished_at=time.time(), error=str(e))


def _extract_token(headers, params) -> str:
    """从 query / Authorization Bearer / X-Webhook-Token 中提取 token。"""
    t = (params.get("token") or "").strip()
    if t:
        return t
    auth = (headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    t = (headers.get("X-Webhook-Token") or "").strip()
    return t


def _check_token(headers, params) -> bool:
    if not config.WEBHOOK_TOKEN:
        return True
    return _extract_token(headers, params) == config.WEBHOOK_TOKEN


def _parse_feishu_trigger():
    """解析飞书工作流触发的 data_id / name,兼容 query 与 body。"""
    rec_id = ""
    name = ""
    obj = request.get_json(silent=True) or {}
    if isinstance(obj, dict):
        rec_id = str(obj.get("data_id") or obj.get("id") or obj.get("record_id") or "").strip()
        name = str(obj.get("name") or obj.get("note") or obj.get("data") or "").strip()
    qp = request.args
    if not rec_id:
        rec_id = (qp.get("data_id") or qp.get("id") or qp.get("record_id") or "").strip()
    if not name:
        name = (qp.get("name") or qp.get("note") or qp.get("data") or "").strip()
    return rec_id, name


@health_blueprint.route("/health")
@health_blueprint.route("/ping")
def health():
    return jsonify({"status": "ok", "ts": time.time(), "service": "wenlv-webhook"})


@health_blueprint.route("/")
def index():
    return jsonify({
        "service": "wenlv-webhook",
        "endpoints": {
            "health": "/health (免鉴权)",
            "post_trigger_feishu": WEBHOOK_PATH_FEISHU + " (POST,需 token)",
            "post_write_mysql": WEBHOOK_PATH + " (POST,免鉴权)",
            "get_status_feishu": WEBHOOK_PATH_FEISHU + "?status=<job_id> (免鉴权,只读)",
            "get_status_db": WEBHOOK_PATH + "?status=<job_id> (免鉴权,只读)",
        },
    })


@blueprint.route("/wenlv_feishu", methods=["GET", "HEAD", "POST"])
def wenlv_feishu():
    params = request.args
    if "status" in params:
        return jsonify(_get_job(params.get("status")) or {"status": "not_found"})
    if request.method != "POST":
        return jsonify({"status": "method_not_allowed", "msg": "请用 POST 触发主流程"}), 405
    if not _check_token(request.headers, params):
        received = _extract_token(request.headers, params)
        return jsonify({
            "status": "forbidden",
            "msg": "token 不正确",
            "received_token_prefix": (received[:6] + "***") if received else "(none)",
        }), 403

    rec_id, name = _parse_feishu_trigger()
    if not name:
        name = "文旅案例"
    job_id = str(uuid.uuid4())[:8]
    now_str = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
    data_initial = "触发时间:{}, 正在更新案例，预计20分钟......".format(now_str)

    _notify_feishu_workflow(rec_id, data_initial, job_id, "wenglu_webhook")
    t = threading.Thread(target=_run_feishu_job_async, args=(job_id, rec_id, now_str), daemon=True)
    t.start()
    return jsonify({"status": "started", "job_id": job_id, "id": rec_id or job_id, "data": data_initial})


@blueprint.route("/wenlv_db", methods=["GET", "HEAD", "POST"])
def wenlv_db():
    params = request.args
    if "status" in params:
        return jsonify(_get_job(params.get("status")) or {"status": "not_found"})

    if request.method != "POST":
        if not _check_token(request.headers, params):
            return jsonify({"status": "forbidden", "msg": "token 不正确"}), 403
        job_id = str(uuid.uuid4())[:8]
        t = threading.Thread(target=_run_mysql_job_async, args=(job_id,), daemon=True)
        t.start()
        return jsonify({
            "status": "started",
            "job_id": job_id,
            "query": WEBHOOK_PATH + "?status=" + job_id,
        })

    row = request.get_json(silent=True)
    if not isinstance(row, dict) or not row:
        return jsonify({"code": -1, "status": "bad_request", "msg": "body 必须是 JSON object"}), 400

    allowed = ["title", "url", "summary", "publish_time", "author",
               "contentAddressNew", "source", "info_type"]
    row.pop("use_time", None)
    if not any(row.get(k) for k in allowed):
        return jsonify({
            "code": -1,
            "status": "bad_request",
            "msg": "body 缺少有效字段,至少需要 1 个: {}".format(allowed),
            "data": {"allowed": allowed},
        }), 400

    if config.MYSQL_DEDUP_ENABLED:
        try:
            is_dup, reason = dedup.check_duplicate(
                (row.get("url") or "").strip(),
                (row.get("summary") or "").strip(),
            )
            if is_dup:
                return jsonify({
                    "code": 1,
                    "status": "duplicate",
                    "msg": "已存在相似记录,跳过写入",
                    "data": {"reason": reason},
                    "reason": reason,
                })
        except Exception as e:
            log.warning("[webhook POST] 查重失败,降级为未命中: {}".format(e))

    try:
        new_id = mysql_client.insert_row(row)
    except Exception as e:
        log.exception("[webhook POST] 写入 MySQL 失败: {}".format(e))
        return jsonify({"code": -1, "status": "error", "msg": str(e), "data": {"error": str(e)}}), 500

    return jsonify({
        "code": 0,
        "status": "ok",
        "msg": "ok",
        "id": new_id,
        "title": row.get("title", ""),
        "data": {"id": new_id, "title": row.get("title", "")},
    })
