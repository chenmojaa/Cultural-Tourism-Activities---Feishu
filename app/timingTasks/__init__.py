# -*- coding: utf-8 -*-
from flask import Blueprint

blueprint = Blueprint("wenlv_timing_tasks", __name__, url_prefix="/timingTasks")

from . import routes
