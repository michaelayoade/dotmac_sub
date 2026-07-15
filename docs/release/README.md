# Production Release — DotMac Self-Care

Release-prep pack for the mobile apps. Work through the checklist; the detailed
submission content lives in the linked docs.

- [`privacy-policy.md`](privacy-policy.md) — privacy policy draft (publish at a public URL; both stores require it)
- [`play-store-submission.md`](play-store-submission.md) — Data Safety, content rating, listing copy, release steps
- [`ios-appstore-submission.md`](ios-appstore-submission.md) — privacy nutrition labels, listing copy, review notes, release steps

## Go-live checklist

### App (done ✅)
- [x] Unified payment selector (Paystack / Flutterwave / bank transfer + saved cards)
- [x] Reseller bulk pay + withholding tax + receipt + allocation
- [x] Address geocode-on-save + pin auto-approval (shadow on prod)
- [x] Ticket attachment opening
- [x] Backend deployed to prod

### Security hygiene (owner: you)
- [ ] Rotate the `admin` account password (was shared in chat; weak default)
- [ ] Rotate the exposed Gemini key (OpenBao incident)
- [ ] Back up `~/dotmac-android-signing/` (upload keystore) to a password manager

### Pre-submission
- [ ] Publish `privacy-policy.md` at a stable HTTPS URL → use in both listings
- [ ] Cut fresh builds off `main` (iOS Xcode Cloud + Android `.aab`) — carry all merged work
- [ ] Prepare a **stable demo account** with representative data for App Review
- [ ] Screenshots refreshed against the current build (green theme)

### iOS → App Store
- [ ] App Privacy labels (`ios-appstore-submission.md` §1)
- [ ] Store listing + age rating + review notes with demo login
- [ ] Latest build added to TestFlight "dotmac" group (not auto-attached)
- [ ] External TestFlight beta (real-device signal) → Submit for Review

### Android → Play Store (new account, first publish)
- [ ] Confirm closed-testing gate (≥12 testers / 14 days for new personal accounts)
- [ ] Data Safety + content rating + privacy policy URL
- [ ] Play App Signing set up; `.aab` uploaded to a closed track
- [ ] Run closed test → promote to Production when eligible

### Post-launch
- [ ] Watch pin auto-approval shadow decisions in admin → flip `gis.location_auto_approve_shadow=false` when trusted
