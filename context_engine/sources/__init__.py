"""Context sources — swappable feeds behind the ContextSource ABC."""

from context_engine.sources.base import ContextItem, ContextSource
from context_engine.sources.yfinance_news import YFinanceNewsSource

__all__ = ["ContextItem", "ContextSource", "YFinanceNewsSource"]
