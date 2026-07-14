FROM python:3.10-slim

WORKDIR /app

# 安装必要的系统工具，例如 libpq-dev (用于 psycopg2 编译，但 psycopg2-binary 通常不需要)
# RUN apt-get update && apt-get install -y gcc libpq-dev && rm -rf /var/lib/apt/lists/*

# 复制依赖列表
COPY requirements.txt .

# 安装依赖
RUN pip install --no-cache-dir -r requirements.txt

# 复制代码到容器
COPY . .

# 暴露 FastAPI 通信端口
EXPOSE 8000

# 只启动 API；数据库迁移由部署流程的一次性任务执行，避免多副本并发迁移
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
