FROM python:3.13-slim
#设置容器内的工作目录为 /app
#后续的 COPY、RUN、CMD 等命令都会在这个目录下执行
WORKDIR /app
#作用：设置 Python 环境变量，禁用输出缓冲
#说明：这样可以让 Python 程序的日志实时输出到容器日志中，方便调试和监控
ENV PYTHONUNBUFFERED=1
#作用：将本地文件复制到容器中
COPY pyproject.toml uv.lock ./
COPY app ./app
COPY mcp_servers ./mcp_servers
COPY demo_service ./demo_service
COPY static ./static
COPY aiops-docs ./aiops-docs
#说明：使用 pip 安装项目依赖的 Python 包，包括项目本身和其依赖的其他包
#作用：升级 pip 到最新版本
#说明：确保使用最新的 pip 版本，避免一些已知的问题
#--no-cache-dir：不缓存安装包，减小镜像体积
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -e .
#说明：启动一个 FastAPI 应用，使用 uvicorn 服务器，监听 9900 端口
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9900"]
