from app.services.runbook_service import runbook_service, split_text


def test_split_text_preserves_long_document_as_bounded_chunks(monkeypatch):
    chunks = split_text(("证据与处理步骤。" * 100) + "\n\n" + ("恢复验证。" * 100))
    assert len(chunks) >= 2
    assert all(chunks)


async def test_runbook_create_deduplicate_and_search():
    content = "Redis 请求出现真实 timeout。\n\n检查 Loki 日志与 Tempo Redis span，再确认连接健康状态。"
    first, created = await runbook_service.create(
        title="MerchantFlow Redis 延迟",
        service_name="merchantflow",
        tags=["redis", "latency"],
        content=content,
        created_by="admin",
    )
    second, duplicate_created = await runbook_service.create(
        title="重复标题",
        service_name="merchantflow",
        tags=[],
        content=content,
        created_by="admin",
    )
    matches = await runbook_service.search("merchantflow", "Redis", limit=3)
    assert created is True
    assert duplicate_created is False
    assert first.id == second.id
    assert matches[0]["title"] == "MerchantFlow Redis 延迟"
