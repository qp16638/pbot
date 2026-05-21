"""Logger ghi ra console (INFO) và file (DEBUG), xoay theo ngày."""

import logging
import os
from datetime import datetime


def setup(name: str = "polybot") -> logging.Logger:
    os.makedirs("logs", exist_ok=True)

    log = logging.getLogger(name)
    if log.handlers:
        return log  # Tránh thêm handler nhiều lần
    log.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-5s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console — chỉ hiện INFO trở lên
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    log.addHandler(ch)

    # File — ghi toàn bộ DEBUG, xoay theo ngày
    date_str = datetime.now().strftime("%Y%m%d")
    fh = logging.FileHandler(f"logs/bot_{date_str}.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    log.addHandler(fh)

    return log
