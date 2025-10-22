# main.py
import logging
import os
from discord_listener import start

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
)

if __name__ == "__main__":
    start()
