"""Build a reviewable page candidate; never publish or alter live configuration."""

from __future__ import annotations

import argparse
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class PrepareHandoffPage:
    source: Path
    destination: Path
    javascript: Path


def prepare(command: PrepareHandoffPage) -> str:
    original = command.source.read_bytes()
    page = original.decode("utf-8").replace("\r\n", "\n")
    form = '<form id="installation-enquiry"'
    if page.count(form) != 1 or "DotmacWebsiteHandoff" in page:
        raise ValueError("Expected the inspected WhatsApp-only coverage form")
    opening_end = page.index(">", page.index(form)) + 1
    page = (
        page[:opening_end]
        + """
              <label for="installation-name">Full name</label>
              <input id="installation-name" name="full_name" autocomplete="name" minlength="2" maxlength="200" required>
              <label for="installation-phone">WhatsApp phone number</label>
              <input id="installation-phone" name="phone" autocomplete="tel" inputmode="tel" maxlength="40" required>
              <p class="fine">Use the number you will message us from. We save your enquiry before opening WhatsApp.</p>
              <p id="installation-enquiry-status" class="fine" role="status" aria-live="polite"></p>
              <noscript>Please enable JavaScript to save an enquiry before continuing to WhatsApp.</noscript>
"""
        + page[opening_end:]
    )
    page = page.replace(
        'action="https://wa.me/2348121179536" method="get"', 'action="#" method="post"'
    )
    page = page.replace('maxlength="650"', 'maxlength="500"')

    def sales_link(match: re.Match[str]) -> str:
        anchor = match.group(0)
        if "new%20internet%20connection" in anchor or re.search(r">[^<]*Sales", anchor):
            anchor = re.sub(
                r'href="https://wa.me/2348121179536[^\"]*"',
                'href="#installation-location"',
                anchor,
            )
            anchor = anchor.replace(' target="_blank"', "")
        return anchor

    page = re.sub(
        r'<a\b[^>]*href="https://wa.me/2348121179536[^\"]*"[^>]*>.*?</a>',
        sales_link,
        page,
        flags=re.S,
    )
    start = page.index("  form.addEventListener('submit', function (event)")
    end = page.index("  pinAddress.addEventListener", start)
    handler = """  form.addEventListener('submit', async function (event) {
    event.preventDefault();
    const output = document.getElementById('installation-enquiry-status');
    const name = document.getElementById('installation-name');
    const phone = document.getElementById('installation-phone');
    const button = form.querySelector('button[type="submit"]');
    if (!output || !name || !phone || !button || !form.reportValidity()) return;
    if (pin && (!confirm.checked || !pinAddress.value.trim())) {
      output.textContent = 'Confirm the installation address and pin, or remove the pin.';
      confirm.focus(); return;
    }
    if (!window.DotmacWebsiteHandoff) { output.textContent = 'Enquiry saving is unavailable. Please reload before trying again.'; return; }
    button.disabled = true;
    output.textContent = 'Saving your enquiry…';
    try {
      const outcome = await window.DotmacWebsiteHandoff.submit({name: name.value,
        phone: phone.value, address: pin ? pinAddress.value : address.value, area: area.value,
        latitude: pin ? pin.latitude : undefined, longitude: pin ? pin.longitude : undefined});
      if (outcome.state !== 'received') { output.textContent = outcome.message; button.disabled = false; return; }
      output.textContent = 'Enquiry saved: ' + outcome.reference + '. Opening WhatsApp; press Send to reach Sales.';
      if (window.DotmacWhatsAppTracking && window.DotmacWhatsAppTracking.handoff) {
        window.DotmacWhatsAppTracking.handoff(outcome.destination, {intent: 'sales', placement: 'coverage_form'});
      } else window.location.assign(outcome.destination);
    } catch (error) { output.textContent = error.message || 'Receipt is unconfirmed. Please contact Sales.'; button.disabled = false; }
  });
"""
    page = page[:start] + handler + page[end:]
    version = hashlib.sha256(command.javascript.read_bytes()).hexdigest()[:12]
    page = page.replace(
        "</head>",
        '<script defer src="/wp-content/mu-plugins/coverage-handoff.js?ver='
        + version
        + '"></script>\n</head>',
        1,
    )
    command.destination.write_text(page, encoding="utf-8")
    return hashlib.sha256(original).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("javascript", type=Path)
    args = parser.parse_args()
    print(
        "Inspected source SHA256:",
        prepare(PrepareHandoffPage(args.source, args.destination, args.javascript)),
    )


if __name__ == "__main__":
    main()
