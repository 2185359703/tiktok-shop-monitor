import uvicorn

from app.observability.logging import configure_logging
from app.settings import Settings


def main():
    configure_logging()
    settings = Settings.load()
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, access_log=False, log_config=None)


if __name__ == "__main__":
    main()
