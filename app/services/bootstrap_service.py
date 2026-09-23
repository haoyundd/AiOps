"""Seed the minimum local control-plane records without committed credentials."""

from pathlib import Path

from loguru import logger
from sqlalchemy import select

from app.config import config
from app.db import MonitoredService, User, session_scope
from app.domain import Role
from app.security import hash_password
from app.services.runbook_service import runbook_service


async def bootstrap_database() -> None:
    async with session_scope() as session:
        service = await session.scalar(
            select(MonitoredService).where(
                MonitoredService.name == "merchantflow",
                MonitoredService.environment == config.environment,
            )
        )
        if service is None:
            session.add(
                MonitoredService(
                    name="merchantflow",
                    environment=config.environment,
                    description="MerchantFlow-Pro Spring Boot business service",
                    prometheus_labels={"job": "merchantflow", "service": "merchantflow"},
                    loki_labels={"service_name": "merchantflow"},
                    tempo_service_name="merchantflow",
                    health_url=config.merchantflow_health_url,
                    dependencies=[
                        {"name": "mysql", "type": "database"},
                        {"name": "redis", "type": "cache"},
                        {"name": "rocketmq", "type": "message_queue"},
                    ],
                    runbook_tags=["merchantflow", "spring-boot", "redis", "mysql"],
                    allow_mutations=config.lab_mode and config.allow_mutations,
                )
            )

        else:
            # 服务地址会持久化在 PostgreSQL；旧电脑曾保存 localhost，进入 Docker Worker 后
            # localhost 会指向 Worker 自己而不是 MerchantFlow。每次启动都按当前环境校准，
            # 但不覆盖 Prometheus/Loki/Tempo 标签等用户配置。下一步：Worker 取证时直接使用这条可达地址。
            if service.health_url != config.merchantflow_health_url:
                service.health_url = config.merchantflow_health_url
            service.allow_mutations = config.lab_mode and config.allow_mutations

        admin = await session.scalar(select(User).where(User.username == config.admin_username))
        if admin is None and config.admin_password:
            session.add(
                User(
                    username=config.admin_username,
                    password_hash=hash_password(config.admin_password),
                    role=Role.ADMIN,
                )
            )
            logger.info("bootstrapped local admin user from environment")
        elif admin is None:
            logger.warning(
                "ADMIN_PASSWORD is empty; no default account was created. "
                "Set ADMIN_PASSWORD before using authenticated APIs."
            )

    docs_directory = Path(__file__).resolve().parents[2] / "aiops-docs"
    if docs_directory.exists():
        for path in sorted(docs_directory.glob("*.md")):
            await runbook_service.create(
                title=path.stem.replace("_", " "),
                service_name="merchantflow",
                tags=["bundled", path.stem],
                content=path.read_text(encoding="utf-8"),
                created_by="bootstrap",
            )
