"""文件位置：游戏内容（YAML）都放在 data/ 下"""
import os

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")


def data_path(name: str) -> str:
    """data/ 下某个内容文件的完整路径"""
    return os.path.join(DATA_DIR, name)
