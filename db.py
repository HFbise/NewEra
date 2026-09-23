"""
数据库连接池，server.py 和 admin.py 共用。DATABASE_URL 写在 .env 里（Render 上是后台环境变量）
"""
import os

from dotenv import load_dotenv
from psycopg_pool import ConnectionPool

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
# 回合只在读写库时借连接，等 AI 时不占（见 server.run_turn）
pool = ConnectionPool(os.environ["DATABASE_URL"], min_size=1, max_size=10, open=True)
