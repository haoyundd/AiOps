"""SQLite 数据库会话管理。"""

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import config
from app.db.models import Base

_engine_cache = {}


def _ensure_sqlite_parent(database_url: str) -> None:
    """确保 SQLite 文件所在目录存在，避免首次启动时建库失败。"""
    if not database_url.startswith("sqlite:///"):
        return

    db_path = database_url.replace("sqlite:///", "", 1)
    if db_path in {":memory:", ""}:
        return

    Path(db_path).parent.mkdir(parents=True, exist_ok=True)


def get_engine(database_url: str | None = None):
    """按数据库 URL 获取可复用 engine。"""
    url = database_url or config.database_url
    if url not in _engine_cache:
        _ensure_sqlite_parent(url)
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        _engine_cache[url] = create_engine(url, connect_args=connect_args, future=True)
    return _engine_cache[url]


def init_db(database_url: str | None = None) -> None:
    """初始化数据库表结构，第一阶段使用 create_all 简化本地演示部署。"""
    engine = get_engine(database_url)
    Base.metadata.create_all(bind=engine)


def make_session_factory(database_url: str | None = None):
    """创建 Session 工厂，便于服务和测试注入独立数据库。"""
    engine = get_engine(database_url)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


@contextmanager
def session_scope(database_url: str | None = None) -> Iterator[Session]:
    """提供带提交和回滚语义的数据库会话上下文。"""
    session_factory = make_session_factory(database_url)
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

