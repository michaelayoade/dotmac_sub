# Weather Map NOC Separation Plan

## Problem

`/admin/network/weathermap` was a read-only rendering of the same topology graph used by `/admin/network/topology`. That made the Weather Map look like another topology editor view instead of a NOC-focused operational map.

## Scope In This Branch

- Add a persisted `network_weathermap_views` layer for weather-map-specific layout and display settings.
- Keep topology links as the inventory/connectivity source, but store Weather Map positions and NOC settings separately.
- Create a default Weather Map view automatically.
- Add saved node positions and viewport state for the Weather Map.
- Add Save Layout and Reset Layout endpoints.
- Add a Weather Map JSON refresh endpoint.
- Add an Open NOC entry point and auto-refresh control.
- Add link labels showing throughput/utilization on the operational map.
- Add focused tests for default view creation, layout persistence, reset behavior, named-view filtering, routes, and template controls.

## Follow-Up Work

- Add full CRUD for multiple named Weather Map views.
- Add a wallboard layout that removes admin navigation chrome entirely.
- Replace page reload auto-refresh with in-place graph data refresh.
- Add historical playback/time-window controls backed by VictoriaMetrics.
- Add threshold alert animation for critical links.
- Add export to PNG/SVG.
- Add richer device tooltip metrics: CPU, memory, uptime, and interface counts.
