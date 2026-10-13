"""路由自检：从 OpenAPI schema 读出全部接口（FastAPI 新版的惰性路由注册）。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app

schema = app.openapi()
paths = schema.get("paths", {})
count = 0
for path in sorted(paths):
    methods = sorted(m.upper() for m in paths[path])
    count += len(methods)
    print(f"  {','.join(methods).ljust(10)} {path}")
print(f"\n接口总数: {count}（业务接口）  路径数: {len(paths)}")
