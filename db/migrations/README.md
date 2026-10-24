# Migrations

Plain SQL migrations, applied in filename order.

**Always use the direct/unpooled connection string** (`DATABASE_URL_UNPOOLED`,
hostname without `-pooler`) for migrations — never the pooled `DATABASE_URL`
used by the functions at request time. This is the Neon/Drizzle convention:
the pooled connection routes through PgBouncer in transaction mode, which
breaks session-level operations migrations rely on.

```bash
# after `neon deploy` / `neon env pull` has populated .env.local / .env
psql "$DATABASE_URL_UNPOOLED" -f db/migrations/0001_sin_init.sql
```

| File | Contents |
|---|---|
| `0001_sin_init.sql` | SIN: `raw_reports`, `aggregates`, `index_publications` (matches `src/sin/schema.ts`) |
| `0002_canonical_playlists.sql` | `canonical_playlists` |
| `0003_portal_auth.sql` | Portal accounts + `portal_keys` |
| `0004_metasync_node_bindings.sql` | Node binding columns, `sin_request_nonces` |
| `0005_recent_add_signal.sql` | `recent_add` |
| `0006_mb_resolutions.sql` | Composer MusicBrainz cache |
| `0007_download_signals.sql` | `download_grab`, `download_churn` |
| `0009_drop_stored_oauth_tokens.sql` | Clears stored OAuth tokens from `portal_accounts` (the portal no longer stores them) |
| `0010_report_populations.sql` | Per-week counts of reports and of reports from installs sharing drop signals, so Loved & Kept can scale drops to the whole population |
| `0008_count_on_arrival_and_listen.sql` | `report_tokens`, `aggregate_counts` (count on arrival), `listen_album`. **Apply before deploying the matching backend**: the new report path writes these tables. |

No migration has been run yet — Neon is not authenticated in the agent
environment, and running one is Boulder's step after sign-in.
