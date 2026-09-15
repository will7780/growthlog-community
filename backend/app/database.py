"""
数据库连接与会话管理
使用 SQLAlchemy 2.0 + PyMySQL
"""
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, declarative_base
from app.config import settings
from app.timeutil import MYSQL_TIME_ZONE

# 创建数据库引擎
# 使用 PyMySQL 驱动，连接 MySQL
engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,  # 连接前检查连接是否有效
    pool_size=10,
    max_overflow=20,
    echo=False  # 开发时可设为 True 查看 SQL 日志
)


@event.listens_for(engine, "connect")
def _set_session_time_zone(dbapi_connection, _connection_record) -> None:
    """Force Beijing offset on every pooled connection (TIMESTAMP conversion)."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(f"SET time_zone = '{MYSQL_TIME_ZONE}'")
    finally:
        cursor.close()

# 创建会话工厂
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# 声明式基类（用于定义模型）
Base = declarative_base()


def get_db():
    """
    数据库会话依赖注入
    每个请求使用独立的会话，请求结束后自动关闭
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
