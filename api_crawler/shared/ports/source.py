from abc import ABC, abstractmethod
from typing import Iterable

class ISource(ABC):
    @abstractmethod
    def fetch(self) -> Iterable[str]:
        """Return an iterable of raw string pages."""
        pass