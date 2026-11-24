import logging
from pythonjsonlogger import jsonlogger
def configure_logging(level="INFO"):
    handler=logging.StreamHandler()
    handler.setFormatter(jsonlogger.JsonFormatter("%(asctime)s %(levelname)s %(name)s %(message)s %(request_id)s %(trace_id)s"))
    logging.basicConfig(level=getattr(logging,level.upper(),logging.INFO),handlers=[handler],force=True)
