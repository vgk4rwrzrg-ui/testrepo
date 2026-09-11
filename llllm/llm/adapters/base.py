from abc import ABC, abstractmethod
from typing import Dict, Any
from llm.models import RoutingRequest

class BaseAdapter(ABC):
    """Abstract base class for LLM provider adapters."""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.model = config.get("model")
        self.api_key = config.get("api_key")
        
        url = config.get("base_url", "").rstrip('/')
        # Ensure the base_url has a scheme to prevent 'No scheme supplied' errors
        if url and not url.startswith(('http://', 'https://')):
            url = f"https://{url}"
        self.base_url = url
        self.timeout = config.get("timeout", 60)

    @abstractmethod
    def call(self, request: RoutingRequest) -> Dict[str, Any]:
        """
        Execute the LLM call and return a normalized response.
        Should handle provider-specific payload construction and response parsing.
        """
        pass