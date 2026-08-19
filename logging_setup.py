import logging
import sys


def setup_logging(error_log_path: str, level=logging.INFO):
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = logging.FileHandler(error_log_path)
    file_handler.setLevel(logging.WARNING)   # error/exception log per the reporting design
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    return root
