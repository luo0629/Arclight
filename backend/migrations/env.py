from alembic import context
from sqlalchemy import JSON, create_engine, pool

from arclight.core.config import get_settings
from arclight.persistence import models  # noqa: F401
from arclight.persistence.base import Base

config = context.config
url = config.attributes.get("database_url") or get_settings().database_url


def include_object(obj, name, type_, reflected, compare_to):
    # LangGraph 的 checkpoint 表由其官方迁移管理，不属于业务 Alembic 元数据。
    if type_ == "table" and reflected and compare_to is None:
        return name in Base.metadata.tables
    return True


def render_item(type_, obj, autogen_context):
    # 显式渲染 JSON 变体，避免自动生成 JSONB astext_type 时出现未限定的 Text。
    if type_ == "type" and isinstance(obj, JSON):
        autogen_context.imports.add("from sqlalchemy.dialects import postgresql")
        return 'sa.JSON().with_variant(postgresql.JSONB(), "postgresql")'
    return False


options = {
    "target_metadata": Base.metadata,
    "compare_type": True,
    "render_item": render_item,
    "include_object": include_object,
}

if context.is_offline_mode():
    context.configure(url=url, literal_binds=True, dialect_opts={"paramstyle": "named"}, **options)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            render_as_batch=engine.dialect.name == "sqlite",
            **options,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()
