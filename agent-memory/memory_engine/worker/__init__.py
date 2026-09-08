"""后台记忆 Worker：反思蒸馏(distill) 与 遗忘代谢(decay)。

说明书里“反思与蒸馏(Reflect/Distill)”与“遗忘与衰减(Decay/Forget)”都是离线异步工作：
* ``worker.distill`` —— 从一条长任务轨迹中提炼因果事实(离线复盘)；
* ``worker.decay``  —— DecayWorker 定时跑 ``run_decay_cycle``(睡眠期新陈代谢)。
"""
from __future__ import annotations
