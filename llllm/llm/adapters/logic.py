import requests
from typing import Dict, Any
from llm.adapters.base import BaseAdapter
from llm.models import RoutingRequest

class LogicModelAdapter(BaseAdapter):
    """Adapter for the Logic role model (typically reasoning-heavy)."""

    def call(self, request: RoutingRequest) -> Dict[str, Any]:
        # Build OpenAI-compatible messages list
        messages = [{"role": "system", "content": request.system_prompt}]
        messages.extend(request.messages)

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": request.temperature,
        }

        if request.max_tokens:
            payload["max_tokens"] = request.max_tokens
        
        if request.json_mode:
            payload["response_format"] = {"type": "json_object"}
        
        if request.tools:
            payload["tools"] = request.tools
            if request.tool_choice:
                payload["tool_choice"] = request.tool_choice

        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        try:
            response = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=self.timeout,
                verify=False  # Consistent with existing app behavior
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Logic Model API Error: {str(e)}")