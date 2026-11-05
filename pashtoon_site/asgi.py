"""Entry point for uvicorn: `uvicorn pashtoon_site.asgi:app`."""
import logging

from .web import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = create_app()
