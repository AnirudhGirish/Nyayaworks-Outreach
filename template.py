"""HTML Email Template & Plain-Text Fallback Generator (Primary-First).

Renders minimalist, primary-inbox focused HTML email templates with 1:1 plain-text fallback.
Eliminates heavy wrapper tables, header banners, card borders, and styled buttons
that trigger Gmail's Promotions tab algorithms.

All dynamic text (subject, body, lead name, etc.) is strictly HTML-escaped
using html.escape() to eliminate XSS / injection risks.
"""
from __future__ import annotations

import html
import urllib.parse


def render(
    subject: str,
    body: str,
    recipient_email: str,
    recipient_name: str | None = None,
) -> tuple[str, str]:
    """Render high-deliverability Primary-First HTML email and plain-text fallback.

    Returns:
        tuple[str, str]: (html_body, plaintext_body)
    """
    safe_subject = html.escape(subject or "")
    _ = html.escape(recipient_name or "") if recipient_name else ""

    # Prepare unsubscribe URL
    encoded_email = urllib.parse.quote(recipient_email or "", safe="")
    unsub_url = f"https://nyayaworks.in/unsubscribe?email={encoded_email}"
    safe_unsub_url = html.escape(unsub_url)

    # Process body paragraphs: split by double newlines
    raw_paragraphs = [p.strip() for p in (body or "").split("\n\n") if p.strip()]
    if not raw_paragraphs:
        raw_paragraphs = [(body or "").strip()]

    html_paragraphs = []
    for p in raw_paragraphs:
        escaped_p = html.escape(p).replace("\n", "<br/>")
        html_paragraphs.append(
            f'<p style="margin: 0 0 16px 0; font-size: 15px; line-height: 1.6; color: #1e293b;">{escaped_p}</p>'
        )

    rendered_html_paragraphs = "\n    ".join(html_paragraphs)

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{safe_subject}</title>
</head>
<body style="margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #ffffff; color: #1e293b; -webkit-font-smoothing: antialiased;">
  <div style="max-width: 600px; margin: 0 auto; padding: 20px 12px;">
    {rendered_html_paragraphs}
    <div style="margin-top: 32px; padding-top: 16px; border-top: 1px solid #e2e8f0; font-size: 12px; color: #64748b; line-height: 1.5;">
      <p style="margin: 0 0 4px 0;">NyayaOS | Legal Operating Infrastructure</p>
      <p style="margin: 0;">If you prefer not to receive updates, you can <a href="{safe_unsub_url}" style="color: #64748b; text-decoration: underline;">unsubscribe here</a>.</p>
    </div>
  </div>
</body>
</html>"""

    # Build plain-text fallback version
    plaintext_body = (body or "").strip()
    if "nyayaworks.in/unsubscribe" not in plaintext_body:
        plaintext_body = (
            f"{plaintext_body}\n\n"
            f"If you prefer not to receive updates, you can unsubscribe here: {unsub_url}"
        )

    return html_content, plaintext_body
