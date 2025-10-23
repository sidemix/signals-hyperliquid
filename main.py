# main.py

import logging
import os
# main.py
from dotenv import load_dotenv
load_dotenv()  # <-- loads .env into process env

from discord_listener import start
if __name__ == "__main__":
    start()



logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
)

if __name__ == "__main__":
    start()
