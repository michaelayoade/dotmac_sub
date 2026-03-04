# UI/UX Audit — Wave 1 (2026-03-04)

**Repo:** dotmac_sub
**Portals audited:** Admin, Customer, Reseller
**Scope:** Templates, CSS components, error pages, accessibility, design consistency

---

## P0 — Critical (Fix immediately)

### P0-1: Error pages are bare and brand-disconnected
- **Files:** `templates/errors/{400,403,404,409,500}.html`
- All 5 error pages are plain text + number with no illustrations, no brand personality, and no visual hierarchy beyond the error code.
- Dashboard link hardcoded to `/admin/dashboard` — wrong for customer/reseller portals.
- No animation, no contextual guidance, no warmth.
- **Impact:** Users hitting errors feel lost; brand impression is generic.

### P0-2: Toast notifications lack status icons and ARIA live region
- **File:** `templates/base.html` (toast container, lines 228-258)
- Toasts show colored boxes with plain text but no icon to reinforce meaning (success checkmark, error X, etc.).
- Missing `role="alert"` on error/warning toasts for screen readers.
- Dismiss button has `aria-label` but toast message itself has no semantic structure.
- **Impact:** Accessibility and quick visual scanning of notifications.

### P0-3: Copyright year hardcoded to 2025
- **File:** `templates/layouts/customer.html` line 278
- Footer reads `© 2025 Dotmac` — should be dynamic or 2026.
- **Impact:** Looks unmaintained to end-users.

---

## P1 — High (Fix this sprint)

### P1-1: Empty state component is minimal and generic
- **File:** `templates/components/data/empty_state.html`
- Flat circle icon container (no depth, no animation, no gradient).
- No variant system (error vs empty vs no-results vs loading).
- Title uses `font-medium` instead of design system's `font-display`.
- Missing subtle animation (float or fade-in) that other polished components have.
- **Impact:** Empty states appear throughout the app; upgrading them lifts perceived quality everywhere.

### P1-2: Animations missing `prefers-reduced-motion` in base.html inline styles
- **File:** `templates/base.html` (inline `<style>` block)
- The `status-pulse` animation (line 143-155) and `noise-overlay` do not respect `prefers-reduced-motion`.
- `src/css/utilities/_animations.css` correctly handles it for its own classes, but `base.html` inline keyframes are separate.
- **Impact:** Accessibility for users with vestibular disorders.

### P1-3: Admin sidebar user profile section has no interactive affordance
- **File:** `templates/layouts/admin.html` lines 74-85
- The user profile at the bottom of the sidebar is purely display — no link to profile page, no logout option.
- User must find the top-right dropdown to access profile/settings/logout.
- **Impact:** Discoverability of account actions.

### P1-4: Reseller portal has no footer
- **File:** `templates/layouts/reseller.html`
- Customer portal has a polished footer with legal links. Reseller portal has none.
- **Impact:** Inconsistency between portals; missing legal links.

---

## P2 — Medium (Next sprint)

### P2-1: Color system fragmentation
- Admin uses `slate-*` palette, Customer uses `stone-*`, Reseller uses `stone-*` + `indigo-*`.
- CSS variables defined in `base.html` `<style>` use `--color-brand-*` / `--color-accent-*` but these aren't used in admin templates which use Tailwind classes directly.
- No single source of truth for the full color system.

### P2-2: Search dropdown hardcoded icon matching
- **File:** `templates/layouts/admin.html` lines 186-212
- Icon rendering in search results uses `x-if` chains matching string names (`users`, `credit-card`, etc.) — fragile and hard to extend.
- Should use an icon map or SVG sprite system.

### P2-3: Font loading could use `font-display: swap` and subset
- **File:** `templates/base.html` line 29
- Google Fonts loaded without explicit `&text=` subsetting.
- Large font payloads on initial load; consider self-hosting or adding `display=swap` (already present but no subset).

### P2-4: Card component CSS lacks elevation variants
- **File:** `src/css/components/_cards.css`
- Single card style with hover lift. No `card-elevated`, `card-flat`, or `card-selected` variants.
- Templates create ad-hoc card styles with inline Tailwind classes instead of using the component.

### P2-5: Mobile touch targets on admin header
- **File:** `templates/layouts/admin.html`
- Some header buttons are 40x40px (h-10 w-10) which meets minimum, but notification badge (8x8px) is not independently tappable.

---

## Wave 1 Implementation Plan

Implementing the top 5 high-impact improvements:

1. **Error pages redesign** (P0-1) — Add illustrations, brand warmth, portal-aware links, animations
2. **Toast notification upgrade** (P0-2) — Add status icons, improve ARIA semantics
3. **Copyright year fix + footer polish** (P0-3) — Dynamic year, minor footer cleanup
4. **Empty state component polish** (P1-1) — Gradient icon bg, display font, fade animation
5. **Reduced-motion for inline animations** (P1-2) — Add `prefers-reduced-motion` to base.html styles
