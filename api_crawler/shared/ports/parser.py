from abc import ABC, abstractmethod
from collections.abc import Iterable


class IParser(ABC):
    @abstractmethod
    def parse(self, raw: str) -> Iterable[dict]:
        """Parse raw page into canonical records."""
        pass
