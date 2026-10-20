import logging
import logging.handlers

from sentinel_watchdog import paths

_configured = False


def setup(verbose: bool = False, quiet: bool = False) -> logging.Logger:
    """Configure logging once per process.

    Writes to a size-capped rotating file, and additionally to stderr unless
    --quiet. Under cron, stderr becomes mail to the crontab owner, which is why
    the cron entry passes --quiet and the file is the real log.
    """
    global _configured
    logger = logging.getLogger("sentinel")
    if _configured:
        return logger

    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    try:
        paths.LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            paths.LOG_PATH, maxBytes=2 * 1024 * 1024, backupCount=5
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError:
        # A missing or unwritable log directory must not stop a beat; the
        # stderr handler below still reports what happened.
        pass

    if not quiet:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        logger.addHandler(stream)

    _configured = True
    return logger
