"""EUR exchange rate handler — /change command (public, no approval required)."""
from __future__ import annotations

import logging

from jinja2 import Template
from telegram import Update
from telegram.ext import ContextTypes

from notify_bot.services.cambiocuba import get_rates

logger = logging.getLogger(__name__)

_TEMPLATE = Template(
    """<pre>
{% for item in data %}
<b>Date:</b>    {{ item._id }}
<b>Min:</b>     {{ item.min }}
<b>Max:</b>     {{ item.max }}
<b>Avg:</b>     {{ item.avg }}
<b>Count:</b>   {{ item.count_values }}
<b>Median:</b>  {{ item.median }}
<b>Compra:</b>  {{ item.first.value }}  ({{ item.first.date }})
<b>Venta:</b>   {{ item.last.value }}   ({{ item.last.date }})
{% endfor %}
</pre>"""
)


async def eur_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fetch and display EUR/CUP informal exchange rates for the last 7 days."""
    try:
        data, photo_url = await get_rates(currency="ECU", period="7D")
    except Exception as exc:
        logger.exception("CambioCuba API error")
        await update.message.reply_text(f"⚠️ Could not fetch exchange rates: {exc}")
        return

    rendered = _TEMPLATE.render(data=data)
    await update.message.reply_photo(photo=photo_url)
    await update.message.reply_html(rendered)
