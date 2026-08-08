"""Article extraction — a three-tier cascade with a manual-paste fallback."""

from modules.scraper.extractor import ArticleExtractor
from modules.scraper.fetcher import ArticleFetcher, FetchResult

__all__ = ["ArticleExtractor", "ArticleFetcher", "FetchResult"]
