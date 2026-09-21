"""FastAPI 入口：创建语义指标层服务。"""

from __future__ import annotations

from sml.registry import MetricLayer
from sml.service import create_app

layer = MetricLayer()
app = create_app(layer)
