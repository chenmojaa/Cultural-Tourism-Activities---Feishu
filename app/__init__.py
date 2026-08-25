# -*- coding: utf-8 -*-
from flask import Flask

from app.timingTasks import blueprint
from app.timingTasks.routes import health_blueprint


def create_app():
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    app.register_blueprint(health_blueprint)
    return app
