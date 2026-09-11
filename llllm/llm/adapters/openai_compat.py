import json
import logging
from typing import Dict, Iterator, Optional

import requests

logger = logging.getLogger(__name__)
import json, time
from django.http import StreamingHttpResponse
from django.views.decorators.csrf import csrf_exempt

@csrf_exempt   # test-only
def stream_test(request):
    def gen():
        # Phase 1: Django-only streaming (no LLM)
        for i in range(5):
            yield f"event: status\ndata: {json.dumps({'msg': f'tick {i}'})}\n\n"
            time.sleep(0.5)
        # Phase 2: router stream passthrough
        from llm.router import router

        class FakeReq:
            body = json.dumps({
                "message": "Count from 1 to 50, one number per line.",
                "system_prompt": "You are a helpful assistant.",
                "task_type": "chat",
            }).encode()
            user = getattr(request, 'user', None)
            method = 'POST'
            META = request.META

        upstream = router.stream(FakeReq())
        for chunk in upstream.streaming_content:
            yield chunk.decode() if isinstance(chunk, bytes) else chunk

    resp = StreamingHttpResponse(gen(), content_type='text/event-stream')
    resp['Cache-Control'] = 'no-cache'
    resp['X-Accel-Buffering'] = 'no'
    return resp
      

class OpenAICompatAdapter:
    def __init__(self, cfg: Dict):
        self.base_url = cfg["base_url"].rstrip("/")
        self.api_key = cfg.get("api_key", "")
        self.model = cfg["model"]
        self.timeout = cfg.get("timeout", 120)
        self.max_output = cfg.get("max_output", 8192)
        self.extra_body = cfg.get("extra_body", {})   # e.g. {"reasoning_effort": "low"}
        self.verify = cfg.get("verify", True)

    def _payload(self, req, max_tokens: Optional[int], stream: bool) -> Dict:
        messages = [{"role": "system", "content": req.system_prompt}] + req.messages
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": req.temperature,
            "stream": stream,
            **self.extra_body,
        }
        if max_tokens:
            payload["max_tokens"] = min(max_tokens, self.max_output)
        if req.json_mode:
            payload["response_format"] = {"type": "json_object"}
        if req.tools:
            payload["tools"] = req.tools
        return payload

    def _headers(self) -> Dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def call(self, req, max_tokens: Optional[int] = None) -> Dict:
        r = requests.post(
            f"{self.base_url}/chat/completions",
            headers=self._headers(),
            json=self._payload(req, max_tokens, stream=False),
            timeout=self.timeout,
            verify=self.verify,
        )
        r.raise_for_status()
        try:
            return r.json()
        except json.JSONDecodeError:
            print(f"DEBUG: JSON decode failed. Status: {r.status_code}, Content: {r.text}")
            raise

    def stream(self, req, max_tokens: Optional[int] = None) -> Iterator[str]:
        """Yields text deltas as they arrive."""
        r = requests.post(
            f"{self.base_url}/chat/completions",
            headers=self._headers(),
            json=self._payload(req, max_tokens, stream=True),
            timeout=self.timeout,
            stream=True,
            verify=self.verify,
        )
        r.raise_for_status()
        for line in r.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            data = line[len("data: "):]
            if data == "[DONE]":
                break
            try:
                delta = json.loads(data)["choices"][0]["delta"].get("content")
                if delta:
                    yield delta
            except (KeyError, IndexError, json.JSONDecodeError):
                continue