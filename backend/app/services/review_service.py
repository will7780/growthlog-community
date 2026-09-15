"""
Review generation service placeholder.

The route contract is kept importable, but the feature is gated until its data
model, prompts and tests are completed.
"""
from app.services.feature_gate import feature_not_enabled


async def generate_review_preview(*args, **kwargs):
    feature_not_enabled("AI 复盘")
