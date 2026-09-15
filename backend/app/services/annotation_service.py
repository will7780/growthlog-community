"""
AI annotation service boundary.
"""
from app.services.feature_gate import feature_not_enabled


async def generate_annotation_preview(*args, **kwargs):
    feature_not_enabled("AI 批注")


def confirm_annotation(*args, **kwargs):
    feature_not_enabled("AI 批注确认")
