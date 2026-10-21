from contextlib import contextmanager

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.sqlite import SqliteSaver

from arclight.core.config import Settings, get_settings


@contextmanager
def open_checkpointer(settings: Settings):
    url = settings.checkpoint_url
    if url.startswith("sqlite:///"):
        with SqliteSaver.from_conn_string(url.removeprefix("sqlite:///")) as saver:
            yield saver
    elif url.startswith("postgresql://"):
        with PostgresSaver.from_conn_string(url) as saver:
            yield saver
    else:
        raise ValueError("checkpoint URL 仅支持 postgresql:// 或 sqlite:///")


def setup() -> None:
    """显式初始化官方 checkpoint 表；API 和 Worker 启动时不隐式修改数据库。"""
    with open_checkpointer(get_settings()) as saver:
        saver.setup()


if __name__ == "__main__":
    setup()
