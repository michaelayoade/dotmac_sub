# Fiber plant inventory UI contract

The Fiber Plant page is a read-only ledger over the canonical fiber owners. Its five tabs are FDH cabinets, splitters, strands, splice closures, and change requests. The initial FDH tab renders a bounded first page with the document. Other tabs load only when selected. HTMX requests return the same ledger fragment used by the full page, while ordinary links and forms retain a full-page fallback.

Each tab has its own asset-specific database query, case-insensitive literal search, stable ordering, matching total, and 50-row default page (100 maximum). The hub includes active and inactive assets and labels their inventory state, preserving the existing scope. A tab change starts at page one. Search starts at page one and pagination retains the active tab, search term, and page size in the URL. Empty results, loading, and request failure are explicit. The page does not request the other four inventories or the full-page activation summary to calculate a selected tab fragment.

The standalone FDH, splitter, strand, and splice closure lists use the same owner-backed readers with their existing active-only scope, search, totals, and bounded pages. The review page independently pages pending fiber requests and pending map asset proposals. Bulk approval applies only to selected requests on the current page.

The related Core Devices inventory keeps its existing filters and operational cards while paging 50 devices at a time. Its summary cards count the complete filtered database scope, so page navigation does not change the totals.

Imported topology evidence stored in legacy `notes` is presented as labelled source details in a disclosure. Human notes remain plain text. The display preserves the stored evidence and lets long identifiers wrap within the card; it never treats imported JSON as operator prose.

Verification: search a cabinet by name and code; change tabs and pages; refresh and use browser Back; try an out-of-range page; inspect no-result states; navigate without JavaScript; check long identifiers and notes at desktop and mobile widths. Confirm the selected tab returns at most the requested page size while the total reflects all matching active rows.
