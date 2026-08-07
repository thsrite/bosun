"""开发启动：python3 run.py

默认监听 0.0.0.0（同一局域网的手机等设备可直接访问 http://<本机IP>:8770）。
可用环境变量覆盖：BOSUN_HOST / BOSUN_PORT。
⚠️ 0.0.0.0 = 局域网内任何设备都能访问并驱动 cc/codex，请仅在可信网络使用。
"""
import os
import sys

import uvicorn

# Windows 控制台默认代码页可能不是 UTF-8，中文日志会抛编码错误
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

# 直接导入 app 对象而非 "app.main:app" 字符串：
# PyInstaller 靠静态导入图收集依赖，字符串导入在冻结包内会找不到模块。
from app.main import app

if __name__ == "__main__":
    host = os.environ.get("BOSUN_HOST", "0.0.0.0")
    port = int(os.environ.get("BOSUN_PORT", "8770"))
    print(f"Bosun 工作台: http://127.0.0.1:{port}")
    uvicorn.run(
        app,
        host=host,
        port=port,
        reload=False,
        ws="websockets-sansio",
    )
