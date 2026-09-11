"""Template tags: drop `{% ai_bot_widget %}` into any template."""
from django import template

from ..models import BotProfile

register = template.Library()


@register.inclusion_tag("ai_agent_core/widget.html", takes_context=True)
def ai_bot_widget(context, profile_id=None):
    """Render the floating bot launcher + chat window.

    Usage::

        {% load ai_agent_tags %}
        ...
        {% ai_bot_widget %}          {# default active profile #}
        {% ai_bot_widget 3 %}        {# a specific BotProfile pk #}
    """
    bot = None
    if profile_id:
        bot = BotProfile.objects.filter(pk=profile_id, is_active=True).first()
    if bot is None:
        bot = BotProfile.get_default()
    return {
        "bot": bot,
        "csrf_token": context.get("csrf_token"),
        "request": context.get("request"),
    }
