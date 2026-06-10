"""FastAPI 应用入口

主应用程序，配置路由、中间件、静态文件等
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from contextlib import asynccontextmanager
import os

from app.config import config
from loguru import logger
from app.api import alerts, aiops, chat, demo, file, health, model
from app.core.milvus_client import milvus_manager


@asynccontextmanager#装饰器
async def lifespan(app: FastAPI):#async表示异步 “这个函数可能会等外部事情，等的时候不堵住整个程序”
    """应用生命周期管理"""
    # 启动时执行
    logger.info("=" * 60)
    logger.info(f"🚀 {config.app_name} v{config.app_version} 启动中...")
    logger.info(f"📝 环境: {'开发' if config.debug else '生产'}")
    logger.info(f"🌐 监听地址: http://{config.host}:{config.port}")
    logger.info(f"📚 API 文档: http://{config.host}:{config.port}/docs")
    
    # 连接 Milvus
    logger.info("🔌 正在连接 Milvus...")
    milvus_manager.connect()
    logger.info("✅ Milvus 连接成功")
    
    logger.info("=" * 60)
    
    yield #启动服务 → 执行 lifespan 中 yield 之前的代码 → 服务运行 → 关闭服务 → 执行 yield 之后的代码
    
    # 关闭时执行
    logger.info("🔌 正在关闭 Milvus 连接...")
    milvus_manager.close()
    logger.info(f"👋 {config.app_name} 关闭")


# 创建 FastAPI 应用
app = FastAPI(
    title=config.app_name,
    version=config.app_version,
    description="基于 LangChain 的智能oncall运维系统",
    lifespan=lifespan
)

# 配置 CORS#Cross-Origin Resource Sharing（跨域资源共享）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 生产环境应该限制具体域名
    allow_credentials=True,
    allow_methods=["*"],# 允许哪些 HTTP 方法
    allow_headers=["*"],# 允许哪些请求头
)

# 注册路由
#给那四个api路由呢
app.include_router(health.router, tags=["健康检查"])
app.include_router(chat.router, prefix="/api", tags=["对话"])
#chat.router 中的 /chat 路由 → 实际访问路径 /api/chat。有个前缀/api
app.include_router(file.router, prefix="/api", tags=["文件管理"])
app.include_router(aiops.router, prefix="/api", tags=["AIOps智能运维"])
app.include_router(alerts.router, prefix="/api", tags=["告警与事故"])#接收 Alertmanager 告警
app.include_router(demo.router, prefix="/api", tags=["Demo故障注入"])
app.include_router(model.router, prefix="/api", tags=["模型配置"])

# 挂载静态文件
static_dir = "static"#把前端静态文件挂载在static目录下，访问路径为/static
app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.get("/")
async def root():
    """返回首页"""
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {
        "message": f"Welcome to {config.app_name} API",
        "version": config.app_version,
        "docs": "/docs"
    }


if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(  #uvicorn.run() 是启动 FastAPI 服务的命令
        "app.main:app",# 应用模块路径，指定了应用实例的位置
        host=config.host,# 监听地址
        port=config.port,# 监听端口
        reload=config.debug,# 开发模式自动重载
        log_level="info"  # 日志级别
    )
