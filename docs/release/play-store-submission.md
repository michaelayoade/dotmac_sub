# Google Play — Submission Pack (DotMac Self-Care)

App ID: `io.dotmac.selfcare` · Category: **Tools** (alt: Business)

> Context: the previous listing was delisted and the original publishing account is
> unrecoverable, so this is a **first-time publish on a new Play account**. A new
> *personal* developer account triggers the **closed-testing requirement: ≥12 testers
> opted-in for ≥14 continuous days** before you can apply for production. A new
> *organization* account may be exempt — confirm in Play Console.

---

## 1. Data safety form
Answer the Play Console "Data safety" questionnaire as follows (derived from the app's
actual data use — see also `privacy-policy.md`).

- **Does your app collect or share any of the required user data types?** → **Yes**
- **Is all collected data encrypted in transit?** → **Yes**
- **Do you provide a way for users to request data deletion?** → **Yes** (in-app profile + contact support)

### Data collected / shared

| Data type | Collected | Shared | Purpose | Optional? |
|---|---|---|---|---|
| Name | Yes | No | Account management | Required |
| Email address | Yes | Yes (payment provider) | Account, payments, comms | Required |
| Phone number | Yes | No | Account, comms | Required |
| Address | Yes | No | Account / service delivery | Required |
| Date of birth | Yes | No | Account management | Optional |
| **Precise location** | Yes | Yes (geocoder) | App functionality (service pin / fault dispatch) | **Optional** |
| Approximate location | Yes | Yes (geocoder) | App functionality | Optional |
| **Payment info** | Yes | Yes (Paystack/Flutterwave) | Process payments (no card numbers stored by us) | Required for paid actions |
| Purchase/transaction history | Yes | No | Billing record | Required |
| App interactions / usage | Yes | No | App functionality (data-usage display) | Required |
| Photos | Yes | No | Profile photo / support attachments | Optional |
| **Crash logs & diagnostics** | Yes | Yes (GlitchTip/Sentry) | Diagnostics | Required |
| Device IDs / push token | Yes | Yes (Firebase) | Push notifications | Required |

- **Advertising / marketing data:** None.
- **Data sold to third parties:** No.

> Note: location is marked **Optional** because the app only uses it when the user opts
> in and chooses to attach it.

---

## 2. Content rating (IARC questionnaire)
Category: **Utility / Productivity / Communication**. Expected rating: **Everyone / PEGI 3**.

- Violence / sexual / profanity / drugs / gambling: **None**
- User-to-user communication: support messaging with staff only (not open social) → answer per questionnaire (typically "users can interact" = limited/no)
- Shares user location with other users: **No** (location goes to staff/dispatch only, not other users)
- Digital purchases: **Yes** (account top-ups / bill payments via external payment gateway)

---

## 3. Store listing copy

**App name:** DotMac Self-Care
**Short description (≤80 chars):**
> Manage your DotMac internet — pay bills, track data usage, get support.

**Full description (≤4000 chars):**
> DotMac Self-Care puts your internet service in your pocket.
>
> • **Pay your way** — top up or pay bills with card (Paystack or Flutterwave), a saved card in one tap, or bank transfer with receipt upload.
> • **Track your data** — see real-time data usage, your plan's fair-use status, and when your next bill is due.
> • **Manage your service** — view your plan, update your profile and service address, and drop a map pin so our technicians find you fast.
> • **Get support** — raise and track support tickets, attach photos, and chat with our team.
> • **Resellers** — consolidated billing, bulk payments with withholding-tax handling, and allocate funds across your subscribers.
>
> Secure by design: biometric app lock, encrypted credentials, and card details handled entirely by our licensed payment providers.
>
> For DotMac internet customers and resellers.

**Keywords/tags:** internet, ISP, self-care, data usage, bill payment, fiber, broadband

---

## 4. Assets checklist (have staged under ~/Downloads/dotmac-android-release/)
- [ ] App icon 512×512 (`play_store_icon_512.png` ✓ generated)
- [ ] Feature graphic 1024×500 (`feature_graphic_1024x500.png` ✓ generated)
- [ ] Phone screenshots ×2–8 (`screenshots/` — verify current build)
- [ ] (Optional) tablet screenshots
- [ ] Privacy policy URL (publish `privacy-policy.md` first)
- [ ] Signed `.aab` (from `mobile-release.yml`, latest off main)

---

## 5. Release steps
1. Create/verify the Play developer account; confirm whether the 12-tester/14-day closed-test gate applies.
2. Create the app → fill **Store listing**, **Data safety** (§1), **Content rating** (§2), **Privacy policy URL**.
3. Set up **Play App Signing** (upload the upload-key `.aab`; keep `~/dotmac-android-signing/` backed up).
4. Create a **Closed testing** track → upload `.aab` → add ≥12 testers → run ≥14 days.
5. Promote to **Production** once eligible.
