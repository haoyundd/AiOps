"""向量存储管理器 - 封装 Milvus 向量存储的写入操作

所有操作统一通过 milvus_manager 的 gRPC 连接进行，避免多连接冲突。
"""

from typing import List
import time
import uuid

from langchain_core.documents import Document
from loguru import logger

from app.core.milvus_client import milvus_manager
from app.services.vector_embedding_service import vector_embedding_service


# 统一使用 biz collection
COLLECTION_NAME = "biz"
# content 字段最大长度（Milvus VARCHAR 限制）
CONTENT_MAX_LENGTH = 8000


class VectorStoreManager:
    """向量存储管理器 — 只负责写入操作

    读取操作请使用 vector_search_service（app/services/vector_search_service.py），
    它走的是同一条 milvus_manager gRPC 连接，稳定可靠。
    """

    def __init__(self):
        """初始化向量存储管理器"""
        self.collection_name = COLLECTION_NAME

    def add_documents(self, documents: List[Document]) -> List[str]:
        """
        批量添加文档到向量存储

        流程：文本 → 向量嵌入 → 直接插入 Milvus collection

        Args:
            documents: LangChain Document 列表

        Returns:
            List[str]: 文档 ID 列表
        """
        try:
            start_time = time.time()

            # 1. 为每个文档生成唯一 ID
            ids = [str(uuid.uuid4()) for _ in documents]

            # 2. 提取文本内容（截断到 Milvus VARCHAR 限制）
            contents = []
            for doc in documents:
                text = doc.page_content
                if len(text) > CONTENT_MAX_LENGTH:
                    text = text[:CONTENT_MAX_LENGTH]
                contents.append(text)

            # 3. 批量生成向量嵌入
            logger.info(f"批量嵌入 {len(documents)} 个文档...")
            #返回1024维向量
            vectors = vector_embedding_service.embed_documents(contents)

            # 4. 提取元数据
            metadatas = [doc.metadata for doc in documents]

            # 5. 构建插入数据（对齐 biz collection 的字段 schema）
            #可以批量插入milvus当成数据库，把collection当成表。要给表里面的字段对齐
            insert_data = [ids, vectors, contents, metadatas]

            # 6. 批量插入 Milvus
            collection = milvus_manager.get_collection()
            collection.insert(insert_data)#grpc调用

            # 强制刷新，确保数据立即可查
            collection.flush()

            elapsed = time.time() - start_time
            logger.info(
                f"批量添加 {len(documents)} 个文档完成, "
                f"耗时: {elapsed:.2f}秒, 平均: {elapsed/len(documents):.2f}秒/个"
            )
            return ids

        except Exception as e:
            logger.error(f"添加文档失败: {e}")
            raise

    def delete_by_source(self, file_path: str) -> int:
        """
        删除指定文件的所有文档

        Args:
            file_path: 文件路径

        Returns:
            int: 删除的文档数量
        """
        try:
            collection = milvus_manager.get_collection()

            # metadata 是 JSON 字段，_source 记录来源文件路径
            expr = f'metadata["_source"] == "{file_path}"'

            result = collection.delete(expr)
            deleted_count = result.delete_count if hasattr(result, "delete_count") else 0

            logger.info(f"删除文件旧数据: {file_path}, 删除数量: {deleted_count}")
            return deleted_count

        except Exception as e:
            logger.warning(f"删除旧数据失败 (可能是首次索引): {e}")
            return 0

    def similarity_search(self, query: str, k: int = 3) -> List[Document]:
        """
        相似度搜索（委托给 vector_search_service）

        Args:
            query: 查询文本
            k: 返回结果数量

        Returns:
            List[Document]: 相关文档列表
        """
        try:
            from app.services.vector_search_service import vector_search_service

            results = vector_search_service.search_similar_documents(query, top_k=k)
            docs = []
            for r in results:
                docs.append(Document(
                    page_content=r.content,
                    metadata={**r.metadata, "score": r.score}
                ))
            logger.debug(f"相似度搜索完成: query='{query}', 结果数={len(docs)}")
            return docs
        except Exception as e:
            logger.error(f"相似度搜索失败: {e}")
            return []


# 全局单例
vector_store_manager = VectorStoreManager()
