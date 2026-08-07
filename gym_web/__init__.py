"""gymbro PWA backend (v3.x — modular architecture).

This package is a gradual refactor of the original single-file `gym_web.py`
(9200 lines, v2.7.x series frozen as v3.0.0 baseline on 2026-08-07).

Each module owns one feature area and exposes a Flask Blueprint that the
app factory registers. New code lands here; legacy code stays in
`gym_web.py` until migrated per-version.

Public entry point is `run.py` at the project root.
"""

from flask import Flask

# v3.x: app factory pattern — modules register blueprints
def create_app() -> Flask:
    """Create and configure the gymbro Flask app.

    v3.0.0 baseline: gym_web.py remains the source of truth for routes.
    v3.1.0+: new code lives in this package; legacy routes are gradually
    migrated from gym_web.py to blueprints here.
    """
    app = Flask(
        __name__,
        static_folder="/home/work/.hermes/image_cache",
        static_url_path="/img",
    )

    # v3.x: register blueprints (currently empty, will grow each release)
    # from gym_web.whoop import bp as whoop_bp
    # app.register_blueprint(whoop_bp)
    # ... more blueprints per release

    return app
