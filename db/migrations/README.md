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

No migration has been run yet — Neon is not authenticated in the agent
environment, and running one is Boulder's step after sign-in.
