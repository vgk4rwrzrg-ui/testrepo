import requests
from typing import Dict, Any
from llm.adapters.base import BaseAdapter
from llm.models import RoutingRequest

class PerceptionModelAdapter(BaseAdapter):
    """Adapter for the Perception role model (typically vision/routine tasks)."""

    def call(self, request: RoutingRequest) -> Dict[str, Any]:
        # Build OpenAI-compatible messages list
        messages = [{"role": "system", "content": request.system_prompt}]
        
        # Handle attachments for vision models
        # We assume the provider expects multi-modal messages in the 'user' role
        processed_messages = []
        for msg in request.messages:
            if msg.get('role') == 'user':
                user_content = []
                if msg.get('content'):
                    user_content.append({"type": "text", "text": msg.get('content')})
                
                # Add images from attachments if applicable
                for attachment in request.attachments:
                    if attachment.content_type.startswith('image/'):
                        # Basic implementation: assuming base64 encoding in content
                        user_content.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:{attachment.content_type};base64,{attachment.content}"}
                        })
                
                processed_messages.append({"role": "user", "content": user_content if len(user_content) > 1 else msg.get('content')})
            else:
                processed_messages.append(msg)

        messages.extend(processed_messages)

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": request.temperature,
        }

        if request.max_tokens:
            payload["max_tokens"] = request.max_tokens
        
        if request.json_mode:
            payload["response_format"] = {"type": "json_object"}

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
            raise RuntimeError(f"Perception Model API Error: {str(e)}")