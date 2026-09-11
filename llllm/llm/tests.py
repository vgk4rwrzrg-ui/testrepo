import unittest
from unittest.mock import patch, MagicMock
import json
from django.conf import settings
from django.test import RequestFactory
from llm.router import LLMRouter
from llm.models import RoutingRequest

class LLMRouterTests(unittest.TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        # Setup a mock LLM_CONFIG in settings
        self.test_config = {
            "logic": {
                "model": "logic-model-id",
                "enabled": True,
                "base_url": "http://logic-api",
                "api_key": "logic-key",
                "supports": {"text": True, "images": False, "tools": True, "json": True},
                "timeout": 30,
            },
            "perception": {
                "model": "perception-model-id",
                "enabled": True,
                "base_url": "http://perception-api",
                "api_key": "perception-key",
                "supports": {"text": True, "images": True, "tools": False, "json": True},
                "timeout": 30,
            },
            "router": {
                "enabled": True,
                "complex_input_threshold": 100, # Low for easy testing
                "escalate_to_logic": True,
            }
        }
        # Patch settings.LLM_CONFIG
        if not hasattr(settings, 'LLM_CONFIG'):
            setattr(settings, 'LLM_CONFIG', self.test_config)
        else:
            settings.LLM_CONFIG = self.test_config
            
        self.router = LLMRouter()

    @patch('requests.post')
    def test_route_to_perception_routine(self, mock_post):
        """Simple short query should route to perception role."""
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {
            "choices": [{"message": {"content": "Hello! I am the perception model."}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}
        }

        request = self.factory.post(
            '/chat', 
            data=json.dumps({"message": "Hi", "history": []}), 
            content_type='application/json'
        )
        
        response = self.router.chat(request)
        res_data = json.loads(response.content)
        
        self.assertEqual(res_data['status'], 'success')
        self.assertEqual(res_data['routing']['role'], 'perception')
        self.assertEqual(res_data['routing']['model'], 'perception-model-id')

    @patch('requests.post')
    def test_route_to_logic_complex(self, mock_post):
        """Long query should route to logic role."""
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {
            "choices": [{"message": {"content": "Detailed analysis complete."}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
        }

        # Create a long message to trigger complexity threshold
        long_msg = "Explain this complex system: " + ("word " * 100)
        request = self.factory.post(
            '/chat', 
            data=json.dumps({"message": long_msg, "history": []}), 
            content_type='application/json'
        )
        
        response = self.router.chat(request)
        res_data = json.loads(response.content)
        
        self.assertEqual(res_data['routing']['role'], 'logic')
        self.assertEqual(res_data['routing']['model'], 'logic-model-id')

    @patch('requests.post')
    def test_capability_filtering_tools(self, mock_post):
        """Requests with tools should exclude perception if it doesn't support tools."""
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {
            "choices": [{"message": {"content": "Tool call executed."}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}
        }

        # Payload including tools
        payload = {
            "message": "Check the weather", 
            "history": [], 
            "tools": [{"type": "function", "function": {"name": "get_weather"}}]
        }
        request = self.factory.post(
            '/chat', 
            data=json.dumps(payload), 
            content_type='application/json'
        )
        
        response = self.router.chat(request)
        res_data = json.loads(response.content)
        
        self.assertEqual(res_data['routing']['role'], 'logic')

    @patch('requests.post')
    def test_escalation_perception_to_logic(self, mock_post):
        """If perception returns empty content, it should escalate to logic."""
        # First call (perception) returns empty content, second call (logic) returns valid
        mock_post.side_effect = [
            MagicMock(status_code=200, json=lambda: {"choices": [{"message": {"content": ""}}]}),
            MagicMock(status_code=200, json=lambda: {"choices": [{"message": {"content": "Escalated answer"}}]})
        ]

        request = self.factory.post(
            '/chat', 
            data=json.dumps({"message": "Routine task", "history": []}), 
            content_type='application/json'
        )
        
        response = self.router.chat(request)
        res_data = json.loads(response.content)
        
        self.assertEqual(res_data['status'], 'success')
        self.assertEqual(res_data['answer'], 'Escalated answer')
        self.assertEqual(mock_post.call_count, 2)