from flask import Flask, jsonify
from .config import Config
from .models import db
from flask_jwt_extended import JWTManager
from flask_cors import CORS

jwt = JWTManager()

def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    CORS(app)

    db.init_app(app)
    jwt.init_app(app)

    with app.app_context():
        from . import models
        db.create_all()

    @app.route("/")
    def index():
        return jsonify({"message": "Portfolio API is running"})

    # API-Routen
    from .routes import api_bp
    app.register_blueprint(api_bp, url_prefix="/api")

    return app
