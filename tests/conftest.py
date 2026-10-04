import os
import sys

# 测试不连数据库：只测纯函数、内容文件和指令解析
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
