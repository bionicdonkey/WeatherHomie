import logging

import uvicorn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# httpx logs full request URLs at INFO, and some upstream APIs take their key in the query string
for _noisy in ("httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
uvicorn.run("stormtracker.main:create_app", factory=True, host="0.0.0.0", port=8000)
