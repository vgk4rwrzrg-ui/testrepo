"""Template tags: drop `{% ai_bot_widget %}` into any template."""
from django import template

from ..models import BotProfile

register = template.Library()


@register.inclusion_tag("ai_agent_core/widget.html", takes_context=True)
def ai_bot_widget(context, profile_id=None, inline=False):
    """Render the bot launcher (animated Little Larry SVG) + chat window.

    Usage::

        {% load ai_agent_tags %}

        {% ai_bot_widget %}                 {# floating launcher (default) #}
        {% ai_bot_widget inline=True %}     {# fills the enclosing div     #}
        {% ai_bot_widget 3 %}               {# specific BotProfile pk      #}
        {% ai_bot_widget 3 inline=True %}

    Inline mode: the SVG icon stretches to 100% width/height of whatever
    element you place the tag in, preserving aspect ratio via its viewBox --
    so it shrinks and grows with the container. Clicking it opens the chat
    window (docked left/right or popup, per the BotProfile).

    Include the tag ONCE per page (element ids are not namespaced).
    """
    bot = None
    if profile_id:
        bot = BotProfile.objects.filter(pk=profile_id, is_active=True).first()
    if bot is None:
        bot = BotProfile.get_default()
    return {
        "bot": bot,
        "inline": bool(inline),
        "csrf_token": context.get("csrf_token"),
        "request": context.get("request"),
    }
