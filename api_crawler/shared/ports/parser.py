from abc import ABC, abstractmethod
from typing import Iterable

class IParser(ABC):
    @abstractmethod
    def parse(self, raw: str) -> Iterable[dict]:
        """Parse raw page into canonical records."""
        pass