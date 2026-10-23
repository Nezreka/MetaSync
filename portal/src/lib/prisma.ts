import { PrismaClient } from '@prisma/client'

const globalForPrisma = globalThis as unknown as {
  prisma: PrismaClient | undefined
}

// Neon serverless: cap pooled connections per instance so a traffic spike
// can't exhaust the database. Honors an explicit connection_limit if the
// deploy already sets one in DATABASE_URL — no env edits required.
// (Copied from roll-call's src/lib/prisma.ts.)
function buildDatasourceUrl(): string | undefined {
  const base = process.env.DATABASE_URL
  if (!base) return undefined
  if (/[?&]connection_limit=/.test(base)) return base
  return `${base}${base.includes('?') ? '&' : '?'}connection_limit=5`
}

const datasourceUrl = buildDatasourceUrl()

export const prisma = globalForPrisma.prisma
  ?? (datasourceUrl ? new PrismaClient({ datasourceUrl }) : new PrismaClient())

if (process.env.NODE_ENV !== 'production') globalForPrisma.prisma = prisma
