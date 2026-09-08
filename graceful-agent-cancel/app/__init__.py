"""Agent 优雅中止 Demo（对应第十章：Agent 优雅中止）。

四道防线：
    1. 交互层     —— 异步提交 + SSE 长连接进度 + 红色 Stop 按钮（POST cancel）
    2. 循环层     —— Agent Loop 每次核心动作前插桩查询取消状态，撞上即跳出
    3. 底层       —— asyncio.Task.cancel() 直接掐断卡在 I/O 里的协程
    4. 兜底层     —— 捕获 CancelledError -> 事务回滚/临时文件清理 -> Partial Yield
"""
