# Customer technician identity

The visits list opens `TechnicianProfileScreen` on demand with the native work
order UUID. It does not fetch portraits for every visit. The new repository uses
the existing authenticated ApiClient Dio transport; credentials never enter URLs.

Identity and portrait requests set `requireFresh: true`. CacheInterceptor excludes
these requests from writes and from offline/5xx fallback even if old cache data
exists. No server rejection becomes a cached approval. Portrait bytes use the
work order, assignment generation and portrait revision, never a stored-file ID.

The screen shows only a newly fetched reviewed card after its authenticated
portrait decodes and a second card confirms the same assignment/revision. Refresh,
errors, application background and route coverage remove the previous card/photo;
foreground/current-route polling runs every 20 seconds and stops on disposal.
Image-cache entries are evicted when cleared. Unselected vendor crews and other
unavailable states never show an approval badge or organisation owner's photo.

The approval describes manual identity review, not an external KYC provider or
current authentication state. The backend rechecks customer ownership, assignment,
active staff binding and current approval on every read. A response already
received cannot be recalled; subsequent refreshes fail closed on changed state.
