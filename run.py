"""容器守望者启动入口。

用法：
  python run.py                # 正常模式（自动探测 Docker，缺失时降级 Mock）
  VT_DEMO_MODE=1 python run.py # 演示模式（compose 三容器场景 + 静态 registry）
"""
import uvicorn

from backend.config import CONFIG

if __name__ == "__main__":
    print(
        f"容器守望者启动：http://{CONFIG.HOST}:{CONFIG.PORT} "
        f"（demo={CONFIG.DEMO_MODE}, db={CONFIG.DB_PATH}）"
    )
    uvicorn.run("backend.app:app", host=CONFIG.HOST, port=CONFIG.PORT, log_level="info")
