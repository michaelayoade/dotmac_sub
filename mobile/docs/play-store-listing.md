# Dotmac Selfcare — Google Play listing & Data Safety (draft)

App: `io.dotmac.selfcare` · self-hosted signing (`~/dotmac-android-signing/upload.jks`).
Privacy policy: https://selfcare.dotmac.io/legal/privacy · Terms: /legal/terms.

## Store listing
- **App name:** Dotmac Selfcare
- **Short description (≤80):** Manage your Dotmac internet — pay bills, track usage, get support.
- **Full description:**
  Dotmac Selfcare puts your internet service in your pocket.
  • See your connection status, live usage, and speed at a glance
  • Pay bills, top up your wallet, and view your full billing ledger
  • Track your technician live on a map during an install or repair
  • Open a support ticket or chat with our team in real time
  • Get notified when your visit is scheduled and when your technician is on the way
  • Manage your plan, service location, and account — all in one place
  For Dotmac fibre and wireless customers across Abuja and Lagos.
- **Category:** Tools (or Business) · **Content rating:** Everyone
- **Contact:** support email + phone `+2348169895859` · website https://selfcare.dotmac.io

## Data Safety form (answer honestly — this must match what the app actually does)
- **Location (approx/precise):** COLLECTED — for the map-pin install request + technician tracking. Shared with the CRM (service provider). Not sold.
- **Personal info (name, email, phone, address):** COLLECTED — account management. Not sold.
- **Financial info (payments):** processed via Paystack (third-party); tokens only, no raw card storage.
- **App activity / device IDs:** COLLECTED for FCM push + diagnostics (Sentry). Not sold.
- **Encryption in transit:** YES · **Data deletion:** YES — in-app "Delete account".
- **All data:** processing tied to providing the service; none sold to third parties.

## Assets needed (you provide / I can generate)
- Feature graphic 1024×500 · App icon 512×512 (have it) · phone screenshots (≥2; the
  iOS `flutter drive` harness can be pointed at Android to regenerate the 5 tabs).

## Account decision (the real blocker)
- **Recommended: Organization Play account** (needs a D-U-N-S number — free, few days) →
  correct identity for Dotmac and avoids the new-personal-account 12-tester/14-day gate.
- Alt: personal account ($25) → but Google forces 12+ testers × 14-day closed test first.
- Meanwhile the signed **APK** (`~/Downloads/dotmac-selfcare-release/`) covers direct/device installs.
