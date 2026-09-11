from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Union
from django.db import models
from django.contrib.auth.models import User

@dataclass
class Attachment:
    filename: str
    content_type: str
    content: Any  # Raw bytes or extracted text
    page_number: Optional[int] = None
    slide_number: Optional[int] = None
    sheet_name: Optional[str] = None
    row_range: Optional[str] = None
    chunk_index: Optional[int] = None

@dataclass
class ProcessedAttachment:
    attachment: Attachment
    processed_text: str
    is_visual: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

@dataclass
class RoutingRequest:
    messages: List[Dict[str, str]]
    system_prompt: str
    attachments: List[Attachment] = field(default_factory=list)
    task_type: Optional[str] = None
    priority: str = "balanced"  # quality, balanced, latency, cost
    temperature: float = 0.0
    max_tokens: Optional[int] = None
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[Union[str, Dict[str, Any]]] = None
    json_mode: bool = False
    json_schema: Optional[Dict[str, Any]] = None
    streaming: bool = False
    request_id: Optional[str] = None
    user_id: Optional[str] = None
    tenant_id: Optional[str] = None
    timeout: Optional[int] = None

@dataclass
class RoutingDecision:
    selected_role: str  # 'logic' or 'perception'
    selected_model: str # Resolved model ID from env
    reason_code: str
    reasons: List[str] = field(default_factory=list)
    estimated_input_tokens: int = 0
    estimated_output_tokens: int = 0

class ChatSession(models.Model):
    """
    Stores an LLM chat session for a specific user.
    """
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='chat_sessions')
    title = models.CharField(max_length=255, default="New Chat Session")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    # saved_view = models.ForeignKey(
    #     'board_analysis.default_saved_views',
    #     on_delete=models.SET_NULL,
    #     null=True,
    #     blank=True,
    #     related_name='chat_sessions'
    # )
    initial_context = models.TextField(blank=True, null=True)

    # NEW: last generated document, kept for iterative revisions
    last_doc_spec = models.JSONField(null=True, blank=True)          # the LLM's document JSON
    last_doc_fmt = models.CharField(max_length=8, null=True, blank=True)  # 'docx'|'pdf'|'xlsx'
    last_doc_report_type = models.CharField(max_length=32, null=True, blank=True)  # 'whitepaper', '8d', ...

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return f"Session {self.id} - {self.user.username}"

class ChatMessage(models.Model):
    """
    Stores individual messages within a chat session.
    """
    ROLE_CHOICES = [
        ('system', 'System'),
        ('user', 'User'),
        ('assistant', 'Assistant'),
    ]

    session = models.ForeignKey(ChatSession, on_delete=models.CASCADE, related_name='messages')
    role = models.CharField(max_length=10, choices=ROLE_CHOICES)
    content = models.TextField()
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['timestamp']

    def __str__(self):
        return f"{self.role} at {self.timestamp}"