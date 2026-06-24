# Apple App Store — Submission Pack (DotMac Self-Care)

Bundle id: `io.dotmac.selfcare` · App: 6778765486 · Ships via Xcode Cloud → TestFlight.

---

## 1. Privacy "nutrition labels" (App Store Connect → App Privacy)
Set **"Data is collected"**. Configure data types below. For each, **Linked to the user
= Yes** (it's tied to their account); **Used for tracking = No** (no cross-app/advertising
tracking); **Used for third-party advertising = No**.

| Data type | Collected | Purposes | Linked | Tracking |
|---|---|---|---|---|
| Name | Yes | App Functionality | Yes | No |
| Email Address | Yes | App Functionality | Yes | No |
| Phone Number | Yes | App Functionality | Yes | No |
| Physical Address | Yes | App Functionality | Yes | No |
| **Precise Location** | Yes | App Functionality | Yes | No |
| Coarse Location | Yes | App Functionality | Yes | No |
| Payment Info | Yes | App Functionality | Yes | No |
| Purchase History | Yes | App Functionality | Yes | No |
| Photos or Videos | Yes | App Functionality | Yes | No |
| Product Interaction / Usage Data | Yes | App Functionality | Yes | No |
| Crash Data | Yes | App Functionality (Diagnostics) | No* | No |
| Other Diagnostic Data | Yes | App Functionality (Diagnostics) | No* | No |
| Device ID (push token) | Yes | App Functionality | Yes | No |

\* Crash/diagnostic data goes to self-hosted GlitchTip and isn't used to identify the user.

- **Used for tracking (App Tracking Transparency):** No → no ATT prompt required.
- Card numbers are **not** collected (tokenized by Paystack/Flutterwave).

---

## 2. Store listing copy

**App Name:** DotMac Self-Care
**Subtitle (≤30 chars):** Internet, billing & support
**Promotional text (≤170):**
> Pay bills, track your data, drop a service pin, and get support — your DotMac internet, managed from anywhere.

**Description:**
> DotMac Self-Care puts your internet service in your pocket.
>
> PAY YOUR WAY
> Top up or pay bills with card (Paystack or Flutterwave), a saved card in one tap, or bank transfer with receipt upload.
>
> TRACK YOUR DATA
> Real-time data usage, fair-use status, and your next bill date at a glance.
>
> MANAGE YOUR SERVICE
> View your plan, update your profile and service address, and drop a map pin so technicians find you fast.
>
> GET SUPPORT
> Raise and track tickets, attach photos, and chat with our team.
>
> FOR RESELLERS
> Consolidated billing, bulk payments with withholding-tax handling, and fund allocation across your subscribers.
>
> Secure by design: biometric app lock, encrypted credentials, and card details handled entirely by licensed payment providers.

**Keywords (≤100 chars, comma-sep):**
> internet,isp,broadband,fiber,data usage,bill payment,self care,dotmac,wifi,account

**Support URL:** https://[dotmac.io/support]  **Marketing URL (optional):** https://[dotmac.io]

---

## 3. App Review notes (paste into "Notes for Review")
> Demo customer login is provided below. The app is a self-care portal for DotMac
> internet subscribers (Nigeria). Payments use Paystack/Flutterwave hosted checkout;
> no card data is stored in the app. Location is foreground-only and optional (used to
> attach a service-location pin for technician dispatch). Push uses Firebase.
>
> Demo account: username **[demo subscriber id]** / password **[demo password]**

> ⚠️ Provide a **stable demo account** with representative data (balance, an invoice, a
> plan, usage) so review can exercise billing/usage/support without a live install.

---

## 4. Assets checklist
- [ ] App icon (1024×1024, no alpha) — from launcher icon
- [ ] 6.7" iPhone screenshots ×3–10
- [ ] 6.5"/5.5" screenshots if required by the screenshot set
- [ ] (Optional) iPad screenshots if iPad supported
- [ ] Privacy policy URL (publish `privacy-policy.md`)
- [ ] Age rating questionnaire (expected 4+)

---

## 5. Release steps
1. Confirm the latest Xcode Cloud build is on TestFlight and **added to the "dotmac" group**
   (builds are NOT auto-attached — see `reference_mobile_build_release`).
2. Fill **App Privacy** (§1), **App Information**, **Pricing (Free)**, **Store listing** (§2),
   age rating, and **App Review notes** (§3) with a working demo account.
3. Attach the build to the App Store version → **Submit for Review**.
4. (Recommended) Run an **external TestFlight beta** first to get real-device signal.
