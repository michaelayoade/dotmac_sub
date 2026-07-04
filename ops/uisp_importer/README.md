# UISP -> Zabbix importer (operational host cron)

`uisp_zabbix_import.py` is the **state-layer** importer: it reads the UISP
inventory (devices + sites, read-only) and reconciles it into the app Zabbix
instance that `app/services/topology/zabbix_reconcile.py` consumes — matching
infrastructure hosts (tags/inventory only) and creating customer-layer hosts
(stations, ONUs) with `parent_ap` / `parent_olt` relationship tags.

This is a verbatim copy of the deployed script, kept in-repo for review and
disaster recovery. It is **not** imported by the app; the in-app
**relationship layer** lives in `app/services/topology/uisp_sync.py`
(`topology_uisp_sync` beat task).

Deployment (since 2026-07-04):

- Runs on the app host at `/root/uisp_importer/` as a 15-minute host cron,
  feeding the co-located Zabbix.
- Dry-run by default; `--apply` requires a confirmed plan file from a prior
  dry-run (`--yes`).
- Tokens via `UISP_TOKEN`/`UISP_TOKEN_FILE` and
  `ZABBIX_API_TOKEN`/`ZABBIX_API_TOKEN_FILE`; never hardcoded.
- Devices in the UISP Archive site are ignored entirely.

Usage:

```sh
UISP_TOKEN_FILE=... ZABBIX_API_TOKEN_FILE=... \
  ./uisp_zabbix_import.py --uisp https://uisp.dotmac.ng \
    --zabbix http://127.0.0.1:8085 [--apply --yes] [--plan-out plan.json]
```

Design: `docs/superpowers/specs/2026-07-04-uisp-topology-connector-design.md`
(v3 spec layer split: Zabbix importer = state, `uisp_sync` = relationships).
