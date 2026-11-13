from __future__ import annotations

import logging
import os

from .bridge import run_forever
from .config import BridgeConfig


def main() -> None:
    logging.basicConfig(level=os.getenv("FEISHU_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    run_forever(BridgeConfig.from_env())


if __name__ == "__main__":
    main()
